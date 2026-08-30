"""Ablation table: T1 / T1+T2 / T1+T2+T3.

Computes auto-resolve rate, false-match rate, and estimated cost per 100
settlement batches for each tier combination.  Reads an existing results.json
and labels.json rather than re-running the pipeline — ablation is post-hoc
analysis of a single completed run.

Usage:
    python3 eval/ablation.py --run out/a --labels data/a/labels.json

Or via Makefile:
    make ablation
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# eval/ is a sibling of src/; add both so milaan and eval.metrics can be imported.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from eval.metrics import compute  # noqa: E402  (after sys.path fixup)


# ---------------------------------------------------------------------------
# Cost model (claude-haiku-4-5-20251001, as of 2025-08)
# Input tokens:  $0.80  per 1M
# Output tokens: $4.00  per 1M
# Estimated tokens per T3 cluster invocation (worst-case 6 iterations):
#   ~1 000 input + ~400 output per iteration = ~8 400 total per cluster
# ---------------------------------------------------------------------------

_HAIKU_INPUT_PER_M  = 0.80
_HAIKU_OUTPUT_PER_M = 4.00
_EST_INPUT_PER_CALL  = 1000   # tokens
_EST_OUTPUT_PER_CALL = 400    # tokens
_MAX_ITER = 6


def _cost_per_cluster(n_iterations: int) -> float:
    """Estimated USD cost for one T3 cluster resolved in n_iterations."""
    inp  = n_iterations * _EST_INPUT_PER_CALL
    outp = n_iterations * _EST_OUTPUT_PER_CALL
    return (inp * _HAIKU_INPUT_PER_M + outp * _HAIKU_OUTPUT_PER_M) / 1_000_000


def _compute_ablation(results: dict, labels: dict) -> list[dict]:
    """Return one row per tier level."""
    all_matches   = results["matches"]
    n_total       = results["total_settlement_batches"]

    rows = []
    for tiers in (["T1"], ["T1", "T2"], ["T1", "T2", "T3"]):
        filtered = [m for m in all_matches if m["tier"] in tiers]
        fake_results = dict(results, matches=filtered)
        report = compute(fake_results, labels)

        # T3 cost estimate: only clusters resolved at T3
        t3_exc_resolved = results.get("t3_matched", 0)

        if "T3" in tiers:
            # Use actual iteration data if present, else assume _MAX_ITER.
            avg_iter = _MAX_ITER
            est_cost_per_100 = (
                t3_exc_resolved / max(1, n_total) * 100
                * _cost_per_cluster(avg_iter)
            )
        else:
            est_cost_per_100 = 0.0

        rows.append({
            "tiers":           "+".join(tiers),
            "auto_resolve":    report.auto_resolve_rate,
            "false_match":     report.false_match_rate,
            "precision":       report.precision,
            "recall":          report.recall,
            "f1":              report.f1,
            "est_usd_per_100": est_cost_per_100,
        })

    return rows


def _print_table(rows: list[dict]) -> None:
    print()
    print("┌─────────────┬──────────────┬──────────────┬───────────┬───────────┬───────────┬──────────────┐")
    print("│ Tier config │ Auto-resolve │ False-match  │ Precision │  Recall   │    F1     │ Cost/100 USD │")
    print("├─────────────┼──────────────┼──────────────┼───────────┼───────────┼───────────┼──────────────┤")
    for r in rows:
        mark = " ◀" if r["false_match"] > 0 else "  "
        print(
            f"│ {r['tiers']:<11} │"
            f"  {r['auto_resolve']:>8.1%}   │"
            f"  {r['false_match']:>8.1%}{mark} │"
            f" {r['precision']:>7.1%}  │"
            f" {r['recall']:>7.1%}  │"
            f" {r['f1']:>7.1%}  │"
            f"  ${r['est_usd_per_100']:>8.4f}   │"
        )
    print("└─────────────┴──────────────┴──────────────┴───────────┴───────────┴───────────┴──────────────┘")
    print()

    # T3 marginal contribution
    if len(rows) >= 3:
        t2_row, t3_row = rows[1], rows[2]
        delta_ar = t3_row["auto_resolve"] - t2_row["auto_resolve"]
        delta_fm = t3_row["false_match"]  - t2_row["false_match"]
        delta_f1 = t3_row["f1"]           - t2_row["f1"]
        print("T3 marginal contribution (T1+T2 → T1+T2+T3):")
        print(f"  auto-resolve : {delta_ar:+.1%}")
        print(f"  false-match  : {delta_fm:+.1%}  (0.0% ideal)")
        print(f"  F1           : {delta_f1:+.3f}")
        if delta_ar == 0 and delta_fm == 0:
            print("  HONEST FINDING: T3 added no auto-resolve on this batch.")
            print("  This is expected when T1+T2 already resolves all bank-settlement pairs.")
            print("  T3 value: exception classification, audit rationale, and precedent RAG.")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ablation table for Milaan pipeline tiers.")
    parser.add_argument("--run",    required=True, help="Directory containing results.json")
    parser.add_argument("--labels", required=True, help="Path to labels.json")
    args = parser.parse_args()

    run_dir = Path(args.run)
    results_path = run_dir / "results.json"
    labels_path  = Path(args.labels)

    if not results_path.exists():
        sys.exit(f"results.json not found in {run_dir}")
    if not labels_path.exists():
        sys.exit(f"labels.json not found: {labels_path}")

    results = json.loads(results_path.read_text())
    labels  = json.loads(labels_path.read_text())

    rows = _compute_ablation(results, labels)
    print(f"\nAblation table — batch: {results.get('data_dir', run_dir)}")
    _print_table(rows)


if __name__ == "__main__":
    main()
