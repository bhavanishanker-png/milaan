"""Per-class D-code recovery table.

For each of the 15 D-codes: how many were injected, resolved by which tier,
escalated (in exception queue), or missed entirely.

Usage (standalone):
    python3 eval/perclass.py --run out/a --labels data/a/labels.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

ALL_DCODES = [f"D{i:02d}" for i in range(1, 16)]

_DCODE_NAMES = {
    "D01": "TIMING_LAG",
    "D02": "FEE_ROUNDING",
    "D03": "NETTED_REFUND",
    "D04": "CHARGEBACK_DEBIT",
    "D05": "PARTIAL_SETTLEMENT",
    "D06": "MANGLED_NARRATION",
    "D07": "MISSING_NARRATION",
    "D08": "DUPLICATE_PAYMENT",
    "D09": "INTERNATIONAL_FX",
    "D10": "NEVER_SETTLED",
    "D11": "UNIDENTIFIED_CREDIT",
    "D12": "SPLIT_SETTLEMENT",
    "D13": "ADJUSTMENT_CREDIT",
    "D14": "REVERSED_PAYMENT",
    "D15": "CURRENCY_MISMATCH",
}


def compute_perclass(results: dict, labels: dict) -> list[dict]:
    """Return one row per D-code with injected/resolved/escalated/missed counts."""
    # Build sets from pipeline results
    matched_pairs = {
        (m["bank_row"], m["settlement_id"]): m
        for m in results.get("matches", [])
    }
    exc_pairs = {
        (e.get("bank_row"), e.get("settlement_id")): e
        for e in results.get("exceptions", [])
    }

    rows = []
    for code in ALL_DCODES:
        # Find all injected discrepancies with this code
        injected = [
            d for d in labels.get("injected_discrepancies", [])
            if d.get("code") == code
        ]
        n_injected = len(injected)

        resolved_t1 = resolved_t2 = resolved_t3 = 0
        escalated = missed = 0

        for disc in injected:
            bank_row = disc.get("bank_row")
            sid = disc.get("settlement_id")
            key = (bank_row, sid)

            if key in matched_pairs:
                tier = matched_pairs[key].get("tier", "?")
                if tier == "T1":
                    resolved_t1 += 1
                elif tier == "T2":
                    resolved_t2 += 1
                elif tier == "T3":
                    resolved_t3 += 1
                else:
                    resolved_t2 += 1
            elif key in exc_pairs:
                escalated += 1
            else:
                missed += 1

        rows.append({
            "code":        code,
            "name":        _DCODE_NAMES.get(code, ""),
            "injected":    n_injected,
            "resolved_t1": resolved_t1,
            "resolved_t2": resolved_t2,
            "resolved_t3": resolved_t3,
            "escalated":   escalated,
            "missed":      missed,
        })

    return rows


def print_perclass(rows: list[dict]) -> None:
    print()
    print("  Per-class D-code recovery table")
    print()
    print(f"  {'Code':<5} {'Class':<22} {'Inj':>3} {'T1':>3} {'T2':>3} {'T3':>3} {'Esc':>3} {'Miss':>4}")
    print("  " + "─" * 55)
    for r in rows:
        miss_flag = " !" if r["missed"] > 0 else "  "
        print(
            f"  {r['code']:<5} {r['name']:<22}"
            f" {r['injected']:>3}"
            f" {r['resolved_t1']:>3}"
            f" {r['resolved_t2']:>3}"
            f" {r['resolved_t3']:>3}"
            f" {r['escalated']:>3}"
            f" {r['missed']:>3}{miss_flag}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run",    required=True)
    parser.add_argument("--labels", required=True)
    args = parser.parse_args()

    results = json.loads((Path(args.run) / "results.json").read_text())
    labels  = json.loads(Path(args.labels).read_text())
    rows = compute_perclass(results, labels)
    print_perclass(rows)


if __name__ == "__main__":
    main()
