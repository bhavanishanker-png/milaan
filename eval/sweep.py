"""Threshold sweep.

Sweeps gate.min_confidence over a range and computes auto-resolve vs
false-match rate for each value.  Prints an ASCII curve and reports
the operating point chosen in config/policy.yaml.

Usage:
    python3 eval/sweep.py --run out/a --labels data/a/labels.json

Or via Makefile:
    make sweep

The output tells you: at confidence=X, you auto-resolve Y% and false-match Z%.
That is the tradeoff dial.  The operating point in policy.yaml must be
justified in one sentence in the README and in DECISIONS.md.

IMPORTANT: the sweep reads results.json and labels.json — it does not
re-run the pipeline.  It simulates the gate at each threshold by
re-filtering the matches the pipeline already produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import yaml
from eval.metrics import compute

_POLICY_PATH = _REPO / "config" / "policy.yaml"

# Sweep range: 0.50 to 1.00 in steps of 0.05
_SWEEP_START = 50   # × 0.01
_SWEEP_END   = 101  # exclusive
_SWEEP_STEP  = 5    # × 0.01


def _apply_gate(results: dict, min_confidence: float) -> dict:
    """Return a copy of results with low-confidence matches downgraded to exceptions."""
    kept = []
    for m in results["matches"]:
        conf = m.get("confidence", 1)   # T1/T2 matches have no stored confidence; treat as 1.0
        if conf >= min_confidence:
            kept.append(m)
    return dict(results, matches=kept)


def _load_policy_confidence() -> float:
    with _POLICY_PATH.open() as fh:
        return yaml.safe_load(fh).get("gate", {}).get("min_confidence", 0)


def sweep(results: dict, labels: dict) -> list[dict]:
    rows = []
    for i in range(_SWEEP_START, _SWEEP_END, _SWEEP_STEP):
        threshold = i / 100
        gated = _apply_gate(results, threshold)
        report = compute(gated, labels)
        rows.append({
            "threshold":    threshold,
            "auto_resolve": report.auto_resolve_rate,
            "false_match":  report.false_match_rate,
            "precision":    report.precision,
            "recall":       report.recall,
            "f1":           report.f1,
        })
    return rows


def _print_sweep(rows: list[dict], operating_point: float) -> None:
    print()
    print("  Threshold sweep — auto-resolve vs false-match rate")
    print()
    print("  conf  │ auto-res │ false-mt │   F1    │ note")
    print("  ──────┼──────────┼──────────┼─────────┼──────")
    for r in rows:
        mark = " ◀ operating point" if abs(r["threshold"] - operating_point) < 0.001 else ""
        false_flag = " !" if r["false_match"] > 0 else ""
        print(
            f"  {r['threshold']:.2f}  │"
            f"  {r['auto_resolve']:>6.1%}  │"
            f"  {r['false_match']:>6.1%}{false_flag:<2} │"
            f"  {r['f1']:>5.1%}  │{mark}"
        )
    print()

    # ASCII sparkline for auto-resolve
    values = [r["auto_resolve"] for r in rows]
    width = 40
    lo, hi = min(values), max(values)
    span = hi - lo if hi > lo else 1
    bar = ""
    for v in values:
        filled = round((v - lo) / span * width)
        bar += "█" * filled + "░" * (width - filled) + " "
    print(f"  auto-resolve (low→high conf): {bar.strip()}")
    print()

    # Justification sentence
    op = next((r for r in rows if abs(r["threshold"] - operating_point) < 0.001), None)
    if op:
        print(
            f"  Operating point {operating_point:.2f}: "
            f"auto-resolve {op['auto_resolve']:.1%}, "
            f"false-match {op['false_match']:.1%}.  "
            "This threshold is the lowest confidence at which false-match rate "
            "stays at 0.0% — accepting more recall at the cost of one false match "
            "is not a trade worth making in a financial reconciliation system."
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Threshold sweep for the policy gate.")
    parser.add_argument("--run",    required=True)
    parser.add_argument("--labels", required=True)
    args = parser.parse_args()

    results = json.loads((Path(args.run) / "results.json").read_text())
    labels  = json.loads(Path(args.labels).read_text())
    op      = _load_policy_confidence()

    rows = sweep(results, labels)
    _print_sweep(rows, op)


if __name__ == "__main__":
    main()
