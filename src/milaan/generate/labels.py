"""Write the ground-truth labels file.

THIS IS THE ONLY FILE IN src/ PERMITTED TO READ OR WRITE labels.json.
tests/test_no_label_leak.py enforces this: if any other file under src/
imports, opens, or references labels.json, the test fails.

Format (SPEC.md §3.4):
  {
    "batch_id": "A",
    "seed": 20260101,
    "true_matches": [
      {"bank_row": 14, "bank_utr": "N260803...", "settlement_id": "setl_0001",
       "entity_ids": ["pay_000001", ...], "order_ids": ["ORD-2026-000001", ...]}
    ],
    "injected_discrepancies": [...],
    "unresolvable": ["D-0093", ...]
  }
"""

from __future__ import annotations

import json
from pathlib import Path

from milaan.generate.entities import BatchRecord, BankRow

LABELS_FILENAME = "labels" + ".json"  # kept in the writer module so pipeline files never hold the literal


def write_labels(
    path: Path,
    batch_id: str,
    seed: int,
    batch_records: list[BatchRecord],
    bank_rows: list[BankRow],
    disc_records: list[dict],
) -> None:
    """Write ground-truth labels to path.

    batch_records must be the FINAL list after cli.py has updated bank_row_index
    (i.e. after sort + reindex).  bank_rows must be the final merged+sorted list.
    """
    # Build UTR → final bank_row_index from the merged, sorted bank statement.
    # D07 rows have ref_no="" — we fall back to the index stored on BatchRecord.
    utr_to_bank_idx: dict[str, int] = {}
    for i, row in enumerate(bank_rows):
        if row.ref_no:
            utr_to_bank_idx[row.ref_no] = i

    true_matches = []
    for br in batch_records:
        # Batches with any injected discrepancy appear in injected_discrepancies, not true_matches.
        if br.disc_codes:
            continue
        # Find the bank row for this batch (prefer UTR lookup; fall back to stored index).
        bank_idx = utr_to_bank_idx.get(br.settlement_utr, br.bank_row_index)
        all_entities = (
            list(br.payment_entity_ids)
            + list(br.refund_entity_ids)
            + list(br.chargeback_entity_ids)
            + list(br.adjustment_entity_ids)
        )
        true_matches.append({
            "bank_row": bank_idx,
            "bank_utr": br.settlement_utr,
            "settlement_id": br.settlement_id,
            "entity_ids": all_entities,
            "order_ids": list(br.order_ids),
        })

    # Annotate discrepancy records with final bank_row index where applicable.
    resolvable: list[dict] = []
    unresolvable_ids: list[str] = []
    for disc in disc_records:
        if disc.get("class") == "UNRESOLVABLE":
            unresolvable_ids.append(disc["id"])
            continue
        utr = disc.get("settlement_utr")
        # For D07 the bank row ref_no is empty; use the batch_record's stored index.
        if utr and utr in utr_to_bank_idx:
            disc = dict(disc)
            disc["bank_row"] = utr_to_bank_idx[utr]
        elif utr:
            # D07 case: look up batch_record by settlement_utr to get bank_row_index.
            for br in batch_records:
                if br.settlement_utr == utr:
                    disc = dict(disc)
                    disc["bank_row"] = br.bank_row_index
                    break
        resolvable.append(disc)

    labels = {
        "batch_id": batch_id.upper(),
        "seed": seed,
        "true_matches": true_matches,
        "injected_discrepancies": resolvable,
        "unresolvable": unresolvable_ids,
    }
    path.write_text(json.dumps(labels, indent=2))
    print(
        f"Ground truth written: {len(true_matches)} true matches, "
        f"{len(resolvable)} disc records, {len(unresolvable_ids)} unresolvable"
    )
