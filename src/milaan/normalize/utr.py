"""UTR extraction and fuzzy recovery.

A UTR (Unique Transaction Reference) is the join key between bank credits and
PSP settlement batches.  It lives inside the bank narration as free text and
is frequently truncated or absent.

Two-phase design:
  Phase 1 (T0 / adapters): extract_utr() pulls whatever is present from each
    bank row's narration and ref_no.  Returns confidence 'exact', 'partial', or
    'none'.  No knowledge of settlement UTRs is required here.

  Phase 2 (T1 / matcher): recover_utr() takes a partial extraction and a set
    of candidate settlement UTRs, and uses rapidfuzz to find the best match.
    Only called for rows where utr_confidence == 'partial'.

UTR format: N{YY}{MM}{DD}{counter:09d}  — 16 chars, always starts with 'N'.
"""

from __future__ import annotations

import re

from rapidfuzz import process, fuzz

# Matches a full 16-char UTR: N + 15 digits.
_UTR_EXACT = re.compile(r"N\d{15}")

# Matches a truncated UTR fragment: N + 8 to 14 digits.
# Lower bound 8 keeps enough date bits (YYMMDD = 6) for meaningful recovery.
_UTR_PARTIAL = re.compile(r"N\d{8,14}")

# Minimum rapidfuzz score to accept a fuzzy recovery.  Calibrated so that an
# 8-char prefix of a 16-char UTR scores > 70 while a random N-number does not.
_FUZZY_MIN_SCORE = 72


def extract_utr(narration: str, ref_no: str) -> tuple[str | None, str]:
    """Extract a UTR from a bank narration + ref_no pair.

    Search order: ref_no first (often cleaner), then narration.

    Returns
    -------
    (utr_value, confidence)
    utr_value  : the extracted string (full or partial), or None
    confidence : 'exact' | 'partial' | 'none'
    """
    for text in (ref_no, narration):
        m = _UTR_EXACT.search(text)
        if m:
            return m.group(), "exact"

    for text in (ref_no, narration):
        m = _UTR_PARTIAL.search(text)
        if m:
            return m.group(), "partial"

    return None, "none"


def recover_utr(
    partial: str,
    candidates: list[str],
    min_score: int = _FUZZY_MIN_SCORE,
) -> tuple[str | None, int]:
    """Attempt fuzzy recovery of a truncated UTR against a list of candidates.

    Uses partial_ratio so that a short prefix matches a longer full UTR.
    Returns (matched_utr, score) or (None, 0).  Score is 0-100 as int.

    Parameters
    ----------
    partial    : the partial UTR string extracted from the narration
    candidates : full UTR strings from the settlement report
    min_score  : minimum score to accept (0-100)
    """
    if not candidates:
        return None, 0

    result = process.extractOne(
        partial,
        candidates,
        scorer=fuzz.partial_ratio,
        score_cutoff=min_score,
    )
    if result is None:
        return None, 0
    matched, score, _ = result
    return matched, int(score)


def utr_stats(rows: list[tuple[str | None, str]]) -> dict[str, int]:
    """Count exact / partial / none across a list of (utr, confidence) pairs."""
    counts: dict[str, int] = {"exact": 0, "partial": 0, "none": 0}
    for _, conf in rows:
        counts[conf] = counts.get(conf, 0) + 1
    return counts
