"""Full pipeline entry point.

Usage (via Makefile):
    python3 -m milaan.pipeline --data data/a --out out/a

Writes out/<batch>/results.json — the single artifact that evaluate.py reads.

Tier progression in this version: T1 only.  T2 and T3 slots are stubs that
will be filled in subsequent phases.  Everything not resolved by T1 lands in
results["exceptions"] for downstream processing.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from milaan.normalize.adapters import bank_hdfc, psp_razorpay, ledger
from milaan.normalize.canonical import IST
from milaan.match import deterministic


def _run(data_dir: Path, out_dir: Path) -> dict:
    """Load three sources, run T1, return serialisable results dict."""
    bank_rows     = bank_hdfc.load(data_dir / "bank.csv")
    setl_rows     = psp_razorpay.load_batches(data_dir / "settlement.csv")
    order_rows    = ledger.load(data_dir / "ledger.csv")  # noqa: F841 — used by T3

    t1 = deterministic.run(bank_rows, setl_rows)

    now = datetime.now(tz=IST).isoformat()

    matches = [
        {
            "bank_row":       m.bank_row_index,
            "settlement_id":  m.settlement_id,
            "settlement_utr": m.settlement_utr,
            "deposit_paise":  m.bank_deposit_paise,
            "tier":           m.tier,
        }
        for m in t1.matches
    ]

    exceptions = [
        {
            "exc_type":       e.exc_type,
            "bank_row":       e.bank_row_index,
            "settlement_id":  e.settlement_id,
            "settlement_utr": e.settlement_utr,
            "detail":         e.detail,
            "tier_reached":   "T1",
        }
        for e in t1.exceptions
    ]

    n_settlement_credits = sum(1 for r in bank_rows if r.is_settlement_credit)

    return {
        "run_at":                 now,
        "pipeline_tiers":         ["T1"],
        "data_dir":               str(data_dir),
        "total_bank_rows":        len(bank_rows),
        "total_order_rows":       len(order_rows),
        "total_settlement_rows":  sum(len(b.rows) for b in setl_rows),
        "total_settlement_batches": len(setl_rows),
        "settlement_credits_in_bank": n_settlement_credits,
        "t1_matched":             t1.n_matched,
        "t1_exceptions":          t1.n_exceptions,
        "matches":                matches,
        "exceptions":             exceptions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Milaan reconciliation pipeline.")
    parser.add_argument("--data", required=True, help="Directory with bank.csv, settlement.csv, ledger.csv")
    parser.add_argument("--out",  required=True, help="Output directory for results.json")
    args = parser.parse_args()

    data_dir = Path(args.data)
    out_dir  = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = _run(data_dir, out_dir)

    out_path = out_dir / "results.json"
    out_path.write_text(json.dumps(results, indent=2))

    print(f"Pipeline complete → {out_path}")
    print(f"  T1 matched  : {results['t1_matched']}/{results['total_settlement_batches']} batches")
    print(f"  Exceptions  : {results['t1_exceptions']}")


if __name__ == "__main__":
    main()
