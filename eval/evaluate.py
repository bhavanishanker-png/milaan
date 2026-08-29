"""Metrics harness — the ONLY file permitted to read labels.json.

Usage:
    python3 eval/evaluate.py --run out/a --labels data/a/labels.json

tests/test_no_label_leak.py enforces that nothing under src/ touches labels.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# eval/ is NOT under src/, so it may read labels freely.
# Metrics is also in eval/.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import metrics as _metrics  # noqa: E402 — relative import from eval/


def _pct(v: float) -> str:
    return f"{v * 100:6.2f}%"


def _bar(v: float, width: int = 20) -> str:
    filled = round(v * width)
    return "█" * filled + "░" * (width - filled)


def _print_table(report: _metrics.MetricsReport) -> None:
    w = 54  # table width

    def row(label: str, value: str, highlight: bool = False) -> None:
        marker = "▶" if highlight else " "
        print(f"│{marker} {label:<28} {value:>20} │")

    print("┌" + "─" * w + "┐")
    print(f"│  {'MILAAN — RECONCILIATION METRICS':^{w-2}}│")
    print("├" + "─" * w + "┤")

    row("Settlement batches",          str(report.total_settlement_batches))
    row("Labels true matches",         str(report.total_true_matches))
    print("├" + "─" * w + "┤")
    row("Pipeline matches (T1)",       str(report.pipeline_matches))
    row("Auto-resolve rate",           _pct(report.auto_resolve_rate))
    print("├" + "─" * w + "┤")

    # False-match rate gets its own highlighted block — it costs money.
    print(f"│  {'FALSE-MATCH RATE (costs money)':^{w-2}}│")
    print("├" + "─" * w + "┤")
    row("False positives (bad matches)", str(report.false_positives),    highlight=True)
    row("FALSE-MATCH RATE",              _pct(report.false_match_rate),  highlight=True)
    print("├" + "─" * w + "┤")

    row("True positives",              str(report.true_positives))
    row("False negatives (missed)",    str(report.false_negatives))
    print("├" + "─" * w + "┤")
    row("Precision",                   _pct(report.precision))
    row("Recall",                      _pct(report.recall))
    row("F1",                          _pct(report.f1))
    print("└" + "─" * w + "┘")

    if report.exc_type_counts:
        print()
        print("  Exception breakdown (T1):")
        for exc_type, cnt in sorted(report.exc_type_counts.items()):
            print(f"    {exc_type:<30} {cnt:>4}")

    if report.fp_by_disc_code:
        print()
        print("  False positives by injected D-code:")
        for code, cnt in sorted(report.fp_by_disc_code.items()):
            print(f"    {code:<12} {cnt:>4}  ← pipeline matched a discrepancy as clean")

    if report.fn_by_disc_code:
        print()
        print("  Missed true matches (FN) breakdown:")
        for code, cnt in sorted(report.fn_by_disc_code.items()):
            print(f"    {code:<12} {cnt:>4}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a Milaan pipeline run against ground truth.")
    parser.add_argument("--run",    required=True, help="Directory containing results.json")
    parser.add_argument("--labels", required=True, help="Path to labels.json")
    args = parser.parse_args()

    run_dir    = Path(args.run)
    labels_path = Path(args.labels)

    results_path = run_dir / "results.json"
    if not results_path.exists():
        print(f"ERROR: {results_path} not found. Run 'make run' first.", file=sys.stderr)
        sys.exit(1)

    results = json.loads(results_path.read_text())
    labels  = json.loads(labels_path.read_text())

    report = _metrics.compute(results, labels)
    _print_table(report)


if __name__ == "__main__":
    main()
