"""T2 — constrained solver.

Handles the T1 residual with three strategies (in priority order):
  1. FUZZY_UTR        — rapidfuzz recovery of truncated UTR (D06)
  2. AMOUNT_TOLERANCE — exact UTR matched by T1 but deposit ± rounding drift (D02)
  3. DATE_AMOUNT_WIN  — no UTR at all; search by date window + amount (D07)

Ambiguity rule: if more than one settlement batch satisfies a bank credit within
tolerance, resolve NOTHING and pass to T3.  Picking one is how false matches
happen.

Subset-sum: in our data every bank credit maps to exactly one settlement batch
(1:1), so the "subset" is always size 1.  The full bounded search is implemented
anyway to handle real-world aggregated credits and to keep the codebase honest.

Config: all thresholds read from config/policy.yaml — never hardcoded here.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

from milaan.normalize.canonical import CanonicalBankRow, SettlementBatch
from milaan.normalize.utr import recover_utr
from milaan.match.deterministic import T1Exception, T1Result

_POLICY_PATH = Path(__file__).parents[4] / "config" / "policy.yaml"


def _load_policy() -> dict:
    with _POLICY_PATH.open() as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class T2Match:
    bank_row_index: int
    settlement_id: str
    settlement_utr: str
    bank_deposit_paise: int
    batch_total_paise: int
    resolution_method: str  # FUZZY_UTR | AMOUNT_TOLERANCE | DATE_AMOUNT_WIN
    confidence: float
    tier: str = "T2"


@dataclass(frozen=True)
class T2Exception:
    exc_type: str
    bank_row_index: int | None
    settlement_id: str | None
    settlement_utr: str | None
    detail: dict = field(default_factory=dict)
    tier_reached: str = "T2"


@dataclass
class T2Result:
    matches: list[T2Match]
    exceptions: list[T2Exception]
    timeouts: list[dict]
    tier: str = "T2"

    @property
    def n_matched(self) -> int:
        return len(self.matches)

    @property
    def n_exceptions(self) -> int:
        return len(self.exceptions)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tolerance(batch: SettlementBatch, paise_per_row: int) -> int:
    """Per-batch tolerance = n_payment_rows × paise_per_row."""
    return len(batch.payment_rows) * paise_per_row


def _subset_candidates(
    target_paise: int,
    batches: list[SettlementBatch],
    tolerance: int,
    max_size: int,
    deadline: float,
) -> tuple[list[list[SettlementBatch]], bool]:
    """Return all subsets of batches whose net sum is within tolerance of target.

    Parameters
    ----------
    target_paise : bank deposit we are trying to explain
    batches      : candidate settlement batches (already date-windowed)
    tolerance    : max abs(target - subset_sum) to accept
    max_size     : cap on subset cardinality (from policy)
    deadline     : monotonic time deadline; returns timed_out=True if exceeded

    Returns
    -------
    (matching_subsets, timed_out)
    matching_subsets : list of lists of SettlementBatch that satisfy the target
    timed_out        : True if search aborted due to time limit
    """
    matching: list[list[SettlementBatch]] = []
    timed_out = False

    for size in range(1, min(max_size, len(batches)) + 1):
        for combo in itertools.combinations(batches, size):
            if time.monotonic() > deadline:
                timed_out = True
                return matching, timed_out
            total = sum(b.total_net_paise for b in combo)
            if abs(total - target_paise) <= tolerance:
                matching.append(list(combo))

    return matching, timed_out


# ---------------------------------------------------------------------------
# Resolution strategies
# ---------------------------------------------------------------------------

def _resolve_fuzzy_utr(
    exc: T1Exception,
    bank_map: dict[int, CanonicalBankRow],
    candidate_batches: list[SettlementBatch],
    policy: dict,
) -> T2Match | T2Exception | None:
    """Recover truncated UTR via rapidfuzz, then verify amount."""
    partial = exc.detail.get("partial_utr")
    if not partial or not candidate_batches:
        return None

    candidate_utrs = [b.settlement_utr for b in candidate_batches]
    matched_utr, score = recover_utr(partial, candidate_utrs)
    if matched_utr is None:
        return None

    batch = next((b for b in candidate_batches if b.settlement_utr == matched_utr), None)
    if batch is None:
        return None

    bank_row = bank_map[exc.bank_row_index]
    tol = _tolerance(batch, policy["solver"]["tolerance_paise_per_row"])
    delta = abs(bank_row.deposit_paise - batch.total_net_paise)

    if delta > tol:
        return T2Exception(
            exc_type="FUZZY_UTR_AMOUNT_MISMATCH",
            bank_row_index=exc.bank_row_index,
            settlement_id=batch.settlement_id,
            settlement_utr=matched_utr,
            detail={
                "partial_utr": partial, "recovered_utr": matched_utr,
                "fuzzy_score": score, "delta_paise": bank_row.deposit_paise - batch.total_net_paise,
                "tolerance_paise": tol,
            },
        )

    s = policy["solver"]
    confidence = (
        s["confidence_fuzzy_utr_exact"] if delta == 0
        else s["confidence_fuzzy_utr_tolerance"]
    )
    return T2Match(
        bank_row_index=exc.bank_row_index,
        settlement_id=batch.settlement_id,
        settlement_utr=batch.settlement_utr,
        bank_deposit_paise=bank_row.deposit_paise,
        batch_total_paise=batch.total_net_paise,
        resolution_method="FUZZY_UTR",
        confidence=confidence,
    )


def _resolve_amount_tolerance(
    exc: T1Exception,
    bank_map: dict[int, CanonicalBankRow],
    batch_map: dict[str, SettlementBatch],
    policy: dict,
) -> T2Match | T2Exception | None:
    """Accept exact-UTR match where deposit differs by ≤ rounding tolerance."""
    if exc.settlement_id is None:
        return None

    batch = batch_map.get(exc.settlement_id)
    if batch is None:
        return None

    bank_row = bank_map[exc.bank_row_index]
    tol = _tolerance(batch, policy["solver"]["tolerance_paise_per_row"])
    delta = abs(bank_row.deposit_paise - batch.total_net_paise)

    if delta > tol:
        return T2Exception(
            exc_type="TOLERANCE_EXCEEDED",
            bank_row_index=exc.bank_row_index,
            settlement_id=batch.settlement_id,
            settlement_utr=batch.settlement_utr,
            detail={"delta_paise": bank_row.deposit_paise - batch.total_net_paise, "tolerance_paise": tol},
        )

    s = policy["solver"]
    slope = s["confidence_tolerance_slope"]
    confidence = s["confidence_amount_tolerance"] - (delta / (tol + 1)) * slope
    return T2Match(
        bank_row_index=exc.bank_row_index,
        settlement_id=batch.settlement_id,
        settlement_utr=batch.settlement_utr,
        bank_deposit_paise=bank_row.deposit_paise,
        batch_total_paise=batch.total_net_paise,
        resolution_method="AMOUNT_TOLERANCE",
        confidence=confidence,
    )


def _resolve_date_amount_window(
    exc: T1Exception,
    bank_map: dict[int, CanonicalBankRow],
    candidate_batches: list[SettlementBatch],
    policy: dict,
) -> T2Match | T2Exception | None:
    """No UTR available — find a settlement batch by date window + amount."""
    bank_row = bank_map[exc.bank_row_index]
    s = policy["solver"]
    window_before = timedelta(days=s["date_window_days_before"])
    window_after = timedelta(days=s["date_window_days_after"])
    paise_per_row = s["tolerance_paise_per_row"]
    max_size = s["max_subset_size"]
    timeout_s = s["per_cluster_timeout_seconds"]

    ref_date = bank_row.value_date
    window_start = ref_date - window_before
    window_end = ref_date + window_after

    # Filter candidates by date window.
    in_window = [
        b for b in candidate_batches
        if window_start <= b.settled_at.date() <= window_end
    ]
    if not in_window:
        return T2Exception(
            exc_type="NO_CANDIDATE_IN_WINDOW",
            bank_row_index=exc.bank_row_index,
            settlement_id=None,
            settlement_utr=None,
            detail={"ref_date": ref_date.isoformat(), "window": f"{window_start}–{window_end}", "candidates": 0},
        )

    # Compute a per-cluster tolerance as the max across candidates.
    max_tol = max(_tolerance(b, paise_per_row) for b in in_window)
    deadline = time.monotonic() + timeout_s

    matching, timed_out = _subset_candidates(
        target_paise=bank_row.deposit_paise,
        batches=in_window,
        tolerance=max_tol,
        max_size=max_size,
        deadline=deadline,
    )

    if timed_out:
        return T2Exception(
            exc_type="SOLVER_TIMEOUT",
            bank_row_index=exc.bank_row_index,
            settlement_id=None,
            settlement_utr=None,
            detail={"candidates_in_window": len(in_window), "timeout_s": timeout_s},
        )

    if len(matching) == 0:
        return T2Exception(
            exc_type="NO_AMOUNT_MATCH",
            bank_row_index=exc.bank_row_index,
            settlement_id=None,
            settlement_utr=None,
            detail={"candidates_in_window": len(in_window), "tolerance_paise": max_tol},
        )

    # Ambiguity: more than one subset satisfies the target.
    if len(matching) > 1:
        return T2Exception(
            exc_type="AMBIGUOUS",
            bank_row_index=exc.bank_row_index,
            settlement_id=None,
            settlement_utr=None,
            detail={
                "n_matching_subsets": len(matching),
                "subset_settlement_ids": [[b.settlement_id for b in s] for s in matching],
            },
        )

    resolved = matching[0]
    if len(resolved) != 1:
        # Multi-batch subset matched (legitimate aggregation — pass to T3 for now).
        return T2Exception(
            exc_type="MULTI_BATCH_SUBSET",
            bank_row_index=exc.bank_row_index,
            settlement_id=None,
            settlement_utr=None,
            detail={"settlement_ids": [b.settlement_id for b in resolved]},
        )

    batch = resolved[0]
    delta = abs(bank_row.deposit_paise - batch.total_net_paise)
    confidence = (
        s["confidence_date_window_exact"] if delta == 0
        else s["confidence_date_window_tolerance"]
    )
    return T2Match(
        bank_row_index=exc.bank_row_index,
        settlement_id=batch.settlement_id,
        settlement_utr=batch.settlement_utr,
        bank_deposit_paise=bank_row.deposit_paise,
        batch_total_paise=batch.total_net_paise,
        resolution_method="DATE_AMOUNT_WIN",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(
    bank_rows: list[CanonicalBankRow],
    settlement_batches: list[SettlementBatch],
    t1_result: T1Result,
    policy: dict | None = None,
) -> T2Result:
    """Run the T2 constrained solver on T1 residuals.

    Takes the T1 result and attempts to resolve each exception using one of the
    three strategies above.  Returns T2Result with new matches and remaining
    exceptions.
    """
    if policy is None:
        policy = _load_policy()

    bank_map: dict[int, CanonicalBankRow] = {r.row_index: r for r in bank_rows}
    batch_map: dict[str, SettlementBatch] = {b.settlement_id: b for b in settlement_batches}

    # Settlement batches claimed by T1 matches.
    claimed_sids: set[str] = {m.settlement_id for m in t1_result.matches}

    # Unmatched settlement batches available as T2 candidates.
    unmatched_batches: list[SettlementBatch] = [
        b for b in settlement_batches if b.settlement_id not in claimed_sids
    ]

    new_matches: list[T2Match] = []
    new_exceptions: list[T2Exception] = []
    timeouts: list[dict] = []

    # Track newly claimed sids so we don't double-assign.
    newly_claimed: set[str] = set()
    newly_claimed_bank_rows: set[int] = set()

    def _available_batches() -> list[SettlementBatch]:
        return [b for b in unmatched_batches if b.settlement_id not in newly_claimed]

    def _emit_match(m: T2Match) -> None:
        new_matches.append(m)
        newly_claimed.add(m.settlement_id)
        newly_claimed_bank_rows.add(m.bank_row_index)

    def _emit_exc(e: T2Exception) -> None:
        new_exceptions.append(e)

    for exc in t1_result.exceptions:
        # Skip exceptions for bank rows already resolved by earlier T2 steps.
        if exc.bank_row_index is not None and exc.bank_row_index in newly_claimed_bank_rows:
            continue

        if exc.exc_type == "UTR_PARTIAL":
            result = _resolve_fuzzy_utr(exc, bank_map, _available_batches(), policy)
            if isinstance(result, T2Match):
                _emit_match(result)
            elif isinstance(result, T2Exception):
                _emit_exc(result)
            else:
                _emit_exc(T2Exception(
                    exc_type="FUZZY_UTR_UNRESOLVED",
                    bank_row_index=exc.bank_row_index,
                    settlement_id=None,
                    settlement_utr=exc.settlement_utr,
                    detail=exc.detail,
                ))

        elif exc.exc_type == "AMOUNT_MISMATCH":
            result = _resolve_amount_tolerance(exc, bank_map, batch_map, policy)
            if isinstance(result, T2Match):
                _emit_match(result)
            elif isinstance(result, T2Exception):
                _emit_exc(result)
            else:
                _emit_exc(T2Exception(
                    exc_type="TOLERANCE_UNRESOLVED",
                    bank_row_index=exc.bank_row_index,
                    settlement_id=exc.settlement_id,
                    settlement_utr=exc.settlement_utr,
                    detail=exc.detail,
                ))

        elif exc.exc_type == "UTR_ABSENT":
            result = _resolve_date_amount_window(exc, bank_map, _available_batches(), policy)
            if isinstance(result, T2Match):
                _emit_match(result)
            elif isinstance(result, T2Exception):
                if exc.exc_type == "SOLVER_TIMEOUT":
                    timeouts.append(result.detail)
                _emit_exc(result)
            else:
                _emit_exc(T2Exception(
                    exc_type="DATE_WINDOW_UNRESOLVED",
                    bank_row_index=exc.bank_row_index,
                    settlement_id=None,
                    settlement_utr=None,
                    detail=exc.detail,
                ))

        elif exc.exc_type in ("SETTLEMENT_UNMATCHED", "UNIDENTIFIED_CREDIT"):
            # SETTLEMENT_UNMATCHED: consumed if its bank row was resolved above.
            # If still here, pass to T3.
            sid = exc.settlement_id
            if sid and sid in newly_claimed:
                continue  # already resolved via its bank row
            _emit_exc(T2Exception(
                exc_type=exc.exc_type,
                bank_row_index=exc.bank_row_index,
                settlement_id=exc.settlement_id,
                settlement_utr=exc.settlement_utr,
                detail=exc.detail,
            ))

        else:
            _emit_exc(T2Exception(
                exc_type=exc.exc_type,
                bank_row_index=exc.bank_row_index,
                settlement_id=exc.settlement_id,
                settlement_utr=exc.settlement_utr,
                detail=exc.detail,
            ))

    return T2Result(matches=new_matches, exceptions=new_exceptions, timeouts=timeouts)
