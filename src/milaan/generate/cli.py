"""Entry point for synthetic data generation.

Usage:
    python -m milaan.generate.cli --batch a --seed 20260101 --records 1000 --out data/a

Writes:
    <out>/ledger.csv
    <out>/settlement.csv
    <out>/bank.csv
    <out>/<labels-file>   (ground truth — written by labels.py, never read by pipeline)

Pipeline:
    1. generate_clean()   — base entities, no discrepancies
    2. inject_all()       — mutate in place, collect label records
    3. generate_noise()   — unrelated bank rows
    4. merge + sort       — bank_rows by txn_date, reindex batch_records
    5. recompute closing balances
    6. write CSVs + ground-truth file
"""

from __future__ import annotations

import argparse
import csv
import random
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path

from milaan.generate.discrepancies import inject_all
from milaan.generate.entities import (
    _OPENING_BALANCE,
    generate_clean,
)
from milaan.generate.labels import LABELS_FILENAME, write_labels
from milaan.generate.noise import generate_noise_rows

# Noise rows per 1 000 records (scales linearly)
_NOISE_ROWS_PER_1K = 20


def _write_csv(path: Path, rows: list) -> None:
    if not rows:
        return
    with path.open("w", newline="") as fh:
        field_names = [f.name for f in fields(rows[0])]
        writer = csv.DictWriter(fh, fieldnames=field_names)
        writer.writeheader()
        writer.writerows(asdict(r) for r in rows)


def _recompute_closing_balances(bank_rows: list, opening: int) -> None:
    prev = opening
    for row in bank_rows:
        prev = prev + row.deposit_paise - row.withdrawal_paise
        row.closing_balance_paise = prev


def generate_batch(
    batch_id: str,
    seed: int,
    n_records: int,
    out_dir: Path,
) -> None:
    """Generate one complete batch and write it to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- clean path ----------------------------------------------------------
    orders, settlement_rows, bank_rows, batch_records = generate_clean(
        seed=seed,
        n_orders=n_records,
        start_date=date(2026, 8, 1),
        spread_days=30,
    )

    # --- discrepancy injection -----------------------------------------------
    # Use a fresh seeded RNG derived from the batch seed so injections are
    # independent of the clean-path generation but still reproducible.
    inj_rng = random.Random(seed + 1)
    label_records = inject_all(
        orders, settlement_rows, bank_rows, batch_records, inj_rng
    )

    # --- noise ---------------------------------------------------------------
    noise_rng = random.Random(seed + 2)
    n_noise = max(5, n_records * _NOISE_ROWS_PER_1K // 1000)
    all_dates = [date.fromisoformat(r.txn_date) for r in bank_rows]
    date_range = (min(all_dates), max(all_dates)) if all_dates else (date(2026, 8, 1), date(2026, 8, 31))
    noise_rows = generate_noise_rows(noise_rng, date_range, n_noise)

    # --- merge bank rows and reindex -----------------------------------------
    # We use object identity to re-map batch_records after sorting.
    br_bank_row_objs = {id(bank_rows[br.bank_row_index]): i for i, br in enumerate(batch_records)}
    all_bank_rows = bank_rows + noise_rows
    # Stable sort by date; settlement credits come before noise on the same day
    # (noise rows have no settlement_utr, so they sort after on equal dates).
    all_bank_rows.sort(key=lambda r: r.txn_date)

    for new_idx, row in enumerate(all_bank_rows):
        obj_id = id(row)
        if obj_id in br_bank_row_objs:
            br_idx = br_bank_row_objs[obj_id]
            batch_records[br_idx].bank_row_index = new_idx

    _recompute_closing_balances(all_bank_rows, _OPENING_BALANCE.value)

    # --- write CSVs ----------------------------------------------------------
    _write_csv(out_dir / "ledger.csv", orders)
    _write_csv(out_dir / "settlement.csv", settlement_rows)
    _write_csv(out_dir / "bank.csv", all_bank_rows)

    # --- write labels --------------------------------------------------------
    write_labels(
        path=out_dir / LABELS_FILENAME,
        batch_id=batch_id,
        seed=seed,
        batch_records=batch_records,
        bank_rows=all_bank_rows,
        disc_records=label_records,
    )

    # --- summary -------------------------------------------------------------
    print(
        f"Batch {batch_id.upper()} (seed={seed}): "
        f"{len(orders)} orders, "
        f"{len(settlement_rows)} settlement rows, "
        f"{len(all_bank_rows)} bank rows "
        f"({len(bank_rows)} settlement credits + {len(noise_rows)} noise)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a synthetic reconciliation batch.")
    parser.add_argument("--batch",   required=True,        help="Batch ID (a, b, or c)")
    parser.add_argument("--seed",    required=True, type=int)
    parser.add_argument("--records", required=True, type=int, help="Number of payment orders")
    parser.add_argument("--out",     required=True,        help="Output directory")
    args = parser.parse_args()

    generate_batch(
        batch_id=args.batch,
        seed=args.seed,
        n_records=args.records,
        out_dir=Path(args.out),
    )


if __name__ == "__main__":
    main()
