"""T1 — deterministic exact match.

Strategy: for each bank row where is_settlement_credit=True AND
utr_confidence="exact", look up the settlement batch whose UTR matches and
compare the bank deposit exactly against the batch total net.

Deliberately dumb: no tolerance, no date windowing, no fuzzy UTR recovery.
Every row that T1 cannot handle lands in exceptions for T2/T3.

Outputs:
  matched    — list of T1Match (one per resolved bank credit)
  exceptions — list of T1Exception (everything T1 could not resolve)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from milaan.normalize.canonical import CanonicalBankRow, SettlementBatch


@dataclass(frozen=True)
class T1Match:
    bank_row_index: int
    settlement_id: str
    settlement_utr: str
    bank_deposit_paise: int
    batch_total_paise: int
    tier: str = "T1"


@dataclass(frozen=True)
class T1Exception:
    """A bank credit or settlement batch that T1 could not resolve."""

    exc_type: str
    # Populated for bank-side exceptions; None for settlement-only exceptions.
    bank_row_index: int | None
    # Populated for settlement-side exceptions; None for bank-only exceptions.
    settlement_id: str | None
    settlement_utr: str | None
    detail: dict = field(default_factory=dict)


@dataclass
class T1Result:
    matches: list[T1Match]
    exceptions: list[T1Exception]
    tier: str = "T1"

    @property
    def n_matched(self) -> int:
        return len(self.matches)

    @property
    def n_exceptions(self) -> int:
        return len(self.exceptions)


_EXC_TYPES = {
    "UTR_PARTIAL": "Bank narration has a truncated UTR — cannot exact-match (D06)",
    "UTR_ABSENT": "No UTR found in bank narration or ref_no (D07)",
    "NO_SETTLEMENT": "Exact UTR found in bank but no matching settlement batch",
    "AMOUNT_MISMATCH": "UTR matched but deposit ≠ batch total (fee drift, refund, chargeback, split, …)",
    "UNIDENTIFIED_CREDIT": "Bank credit classified as settlement but UTR matches no batch (D11, unresolvable)",
    "SETTLEMENT_UNMATCHED": "Settlement batch has no corresponding bank credit (T1 missed)",
}


def run(
    bank_rows: list[CanonicalBankRow],
    settlement_batches: list[SettlementBatch],
) -> T1Result:
    """Run T1 exact matching.

    Algorithm:
      1. Build UTR → batch index from settlement batches.
      2. For each settlement-credit bank row:
         a. utr_confidence == "exact": look up batch, compare amounts.
         b. utr_confidence == "partial": emit UTR_PARTIAL exception.
         c. utr_confidence == "none":   emit UTR_ABSENT exception.
      3. Any settlement batch not claimed by step 2 → SETTLEMENT_UNMATCHED.
    """
    utr_to_batch: dict[str, SettlementBatch] = {b.settlement_utr: b for b in settlement_batches}
    claimed_settlement_ids: set[str] = set()

    matches: list[T1Match] = []
    exceptions: list[T1Exception] = []

    for row in bank_rows:
        if not row.is_settlement_credit:
            continue

        if row.utr_confidence == "partial":
            exceptions.append(T1Exception(
                exc_type="UTR_PARTIAL",
                bank_row_index=row.row_index,
                settlement_id=None,
                settlement_utr=row.utr,
                detail={"partial_utr": row.utr, "narration": row.narration[:80]},
            ))
            continue

        if row.utr_confidence == "none":
            exceptions.append(T1Exception(
                exc_type="UTR_ABSENT",
                bank_row_index=row.row_index,
                settlement_id=None,
                settlement_utr=None,
                detail={"narration": row.narration[:80]},
            ))
            continue

        # Exact UTR — look up the settlement batch.
        batch = utr_to_batch.get(row.utr)
        if batch is None:
            exceptions.append(T1Exception(
                exc_type="UNIDENTIFIED_CREDIT",
                bank_row_index=row.row_index,
                settlement_id=None,
                settlement_utr=row.utr,
                detail={"utr": row.utr, "deposit_paise": row.deposit_paise},
            ))
            continue

        if row.deposit_paise != batch.total_net_paise:
            exceptions.append(T1Exception(
                exc_type="AMOUNT_MISMATCH",
                bank_row_index=row.row_index,
                settlement_id=batch.settlement_id,
                settlement_utr=row.utr,
                detail={
                    "bank_deposit_paise": row.deposit_paise,
                    "batch_total_paise": batch.total_net_paise,
                    "delta_paise": row.deposit_paise - batch.total_net_paise,
                },
            ))
            continue

        matches.append(T1Match(
            bank_row_index=row.row_index,
            settlement_id=batch.settlement_id,
            settlement_utr=row.utr,
            bank_deposit_paise=row.deposit_paise,
            batch_total_paise=batch.total_net_paise,
        ))
        claimed_settlement_ids.add(batch.settlement_id)

    # Settlement batches with no bank row matched.
    for batch in settlement_batches:
        if batch.settlement_id not in claimed_settlement_ids:
            already_excepted = any(
                e.settlement_id == batch.settlement_id for e in exceptions
            )
            if not already_excepted:
                exceptions.append(T1Exception(
                    exc_type="SETTLEMENT_UNMATCHED",
                    bank_row_index=None,
                    settlement_id=batch.settlement_id,
                    settlement_utr=batch.settlement_utr,
                    detail={"batch_total_paise": batch.total_net_paise},
                ))

    return T1Result(matches=matches, exceptions=exceptions)
