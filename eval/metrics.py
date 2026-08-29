"""Metrics computation for the reconciliation pipeline.

This module is part of eval/ and MAY read labels.json (via evaluate.py passing
the already-loaded labels dict).  Nothing in src/ may call this.

Metrics produced:
  auto_resolve_rate  — pipeline matches / total settlement batches
  false_match_rate   — false positives / pipeline matches  (the costly one)
  precision          — tp / (tp + fp)
  recall             — tp / (tp + fn)
  f1                 — harmonic mean of precision and recall
  exception_breakdown — counts by exc_type and by injected D-code
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MetricsReport:
    # Totals from ground truth
    total_settlement_batches: int
    total_true_matches: int       # from labels["true_matches"]

    # Pipeline output counts
    pipeline_matches: int
    true_positives: int           # pipeline match ∩ labels true_match
    false_positives: int          # pipeline match NOT in labels true_match
    false_negatives: int          # labels true_match NOT matched by pipeline

    # Exception breakdown: exc_type → count
    exc_type_counts: dict[str, int] = field(default_factory=dict)
    # Disc-code breakdown for false positives: D-code → count
    fp_by_disc_code: dict[str, int] = field(default_factory=dict)
    # Unmatched disc codes (fn side): D-code → count
    fn_by_disc_code: dict[str, int] = field(default_factory=dict)

    @property
    def auto_resolve_rate(self) -> float:
        if self.total_settlement_batches == 0:
            return 0.0
        return self.pipeline_matches / self.total_settlement_batches

    @property
    def false_match_rate(self) -> float:
        if self.pipeline_matches == 0:
            return 0.0
        return self.false_positives / self.pipeline_matches

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def compute(results: dict, labels: dict) -> MetricsReport:
    """Compare pipeline results against ground truth labels.

    Parameters
    ----------
    results : dict  — loaded from out/<batch>/results.json
    labels  : dict  — loaded from data/<batch>/labels.json
    """
    # Ground truth: (bank_row, settlement_id) pairs that are correct matches.
    true_match_pairs: set[tuple[int, str]] = {
        (m["bank_row"], m["settlement_id"])
        for m in labels.get("true_matches", [])
    }
    total_true = len(true_match_pairs)
    total_batches = results.get("total_settlement_batches", 0)

    # Pipeline matches: (bank_row, settlement_id)
    pipeline_pairs: set[tuple[int, str]] = {
        (m["bank_row"], m["settlement_id"])
        for m in results.get("matches", [])
    }
    n_pipeline = len(pipeline_pairs)

    tp_pairs = pipeline_pairs & true_match_pairs
    fp_pairs = pipeline_pairs - true_match_pairs
    fn_pairs = true_match_pairs - pipeline_pairs

    # Build lookup: (bank_row, settlement_id) → D-code for discrepancy batches.
    disc_pair_to_code: dict[tuple[int, str], str] = {}
    for disc in labels.get("injected_discrepancies", []):
        bank_row = disc.get("bank_row")
        sid = None
        # Find the settlement_id from the disc record if available.
        sid = disc.get("settlement_id")
        code = disc.get("code", "UNKNOWN")
        if bank_row is not None and sid:
            disc_pair_to_code[(bank_row, sid)] = code

    # Break down false positives by D-code.
    fp_by_disc: dict[str, int] = {}
    for pair in fp_pairs:
        code = disc_pair_to_code.get(pair, "UNCLASSIFIED")
        fp_by_disc[code] = fp_by_disc.get(code, 0) + 1

    # Exc-type counts from pipeline exceptions.
    exc_type_counts: dict[str, int] = {}
    for exc in results.get("exceptions", []):
        t = exc.get("exc_type", "UNKNOWN")
        exc_type_counts[t] = exc_type_counts.get(t, 0) + 1

    # False negatives: true matches missed. Map to disc codes (shouldn't happen
    # for correctly matched clean batches, but useful if labels have edge cases).
    fn_by_disc: dict[str, int] = {}
    for pair in fn_pairs:
        code = disc_pair_to_code.get(pair, "CLEAN")
        fn_by_disc[code] = fn_by_disc.get(code, 0) + 1

    return MetricsReport(
        total_settlement_batches=total_batches,
        total_true_matches=total_true,
        pipeline_matches=n_pipeline,
        true_positives=len(tp_pairs),
        false_positives=len(fp_pairs),
        false_negatives=len(fn_pairs),
        exc_type_counts=exc_type_counts,
        fp_by_disc_code=fp_by_disc,
        fn_by_disc_code=fn_by_disc,
    )
