"""Bank narration templates for synthetic data generation.

Six templates from SPEC.md §3.3, covering the full-UTR / absent-UTR /
truncated-UTR spectrum plus one non-settlement credit.

Template identity:
  NEFT_FULL    — HDFC NEFT credit line with the complete UTR (happy path)
  RTGS_FULL    — RTGS transfer, UTR embedded in slash-path notation
  IMPS_FULL    — IMPS transfer, numeric UTR in slash-path notation
  NEFT_ABSENT  — NEFT with no UTR anywhere; only date clue available
  NEFT_TRUNC   — NEFT with UTR cut off mid-string by the bank's system
  UPI_NONSETTL — UPI collect credit unrelated to a Razorpay settlement

T0 normalise uses these templates to build its UTR-extraction regex family.
The template chosen for each bank row is recorded in BatchRecord so that
evaluate.py can stratify UTR-recovery stats by template type.
"""

from __future__ import annotations

import random
from datetime import date
from enum import IntEnum

# Bank IFSC and counterparty strings that appear in real HDFC NEFT lines.
_IFSC = "HDFC0000060"
_PSP_LONG = "RAZORPAY SOFTWARE PVT LTD"
_PSP_SHORT = "RZPY SOFTWARE"
_MERCHANT = "ACME RETAIL"


class NarrationTemplate(IntEnum):
    """Stable integer identity for each narration variant."""
    NEFT_FULL   = 1
    RTGS_FULL   = 2
    IMPS_FULL   = 3
    NEFT_ABSENT = 4
    NEFT_TRUNC  = 5
    UPI_NONSETTL = 6


# ---------------------------------------------------------------------------
# Per-template builders
# Each function returns a (narration, ref_no) pair.
# ref_no is the bank's own reference field, which may or may not contain the
# UTR — this mirrors real bank-statement behaviour.
# ---------------------------------------------------------------------------


def _neft_full(utr: str, _settlement_date: date, _rng: random.Random) -> tuple[str, str]:
    """Standard HDFC NEFT credit line — complete UTR at the tail."""
    narration = (
        f"NEFT CR-{_IFSC}-{_PSP_LONG}-{_MERCHANT}-{utr}"
    )
    return narration, utr


def _rtgs_full(utr: str, _settlement_date: date, _rng: random.Random) -> tuple[str, str]:
    """RTGS transfer — UTR embedded in the slash-delimited path.

    RTGS UTRs conventionally start with a two-letter bank code rather than 'N',
    so we strip the leading 'N' and prepend 'UTR' to match observed patterns.
    """
    numeric = utr.lstrip("N")
    rtgs_ref = f"UTR{numeric}"
    narration = f"RTGS/{rtgs_ref}/{_PSP_LONG}/SETTLEMENT"
    return narration, rtgs_ref


def _imps_full(utr: str, settlement_date: date, _rng: random.Random) -> tuple[str, str]:
    """IMPS transfer — numeric UTR in path, month appended as short name."""
    numeric = utr.lstrip("N")
    month_str = settlement_date.strftime("%b").upper()
    narration = f"IMPS/{numeric}/{_PSP_SHORT}/SETTLEMENT {month_str}"
    return narration, numeric


def _neft_absent(utr: str, settlement_date: date, _rng: random.Random) -> tuple[str, str]:
    """NEFT with no UTR — only the month+day is recoverable.

    The bank truncated or omitted the UTR entirely. T0 must fall back to
    amount + date search; this is what D07 (missing narration) injects.
    """
    month_day = settlement_date.strftime("%b%d").upper()   # e.g. AUG03
    narration = f"NEFT-{_PSP_LONG}-SETTLEMENT-{month_day}"
    # ref_no is also absent — left blank as the bank often does.
    return narration, ""


def _neft_trunc(utr: str, _settlement_date: date, rng: random.Random) -> tuple[str, str]:
    """NEFT line where the UTR has been clipped mid-string.

    Truncation length is random in [8, len(utr)-2] so it always drops at
    least the last two characters.  T0 must handle a partial match.
    This is what D06 (mangled narration) injects.
    """
    trunc_len = rng.randint(8, max(8, len(utr) - 2))
    truncated = utr[:trunc_len]
    narration = f"NEFT CR-{_IFSC}-{_PSP_LONG}-{_MERCHANT}-{truncated}"
    return narration, truncated


def _upi_nonsettl(_utr: str, settlement_date: date, rng: random.Random) -> tuple[str, str]:
    """UPI collect credit that has nothing to do with a Razorpay settlement.

    The reference number looks plausible (12 digits) but is not a settlement
    UTR.  T0 should classify this row as non-settlement and exclude it.
    """
    # 12-digit pseudo-UPI reference — seeded, so deterministic.
    ref_digits = "".join(str(rng.randint(0, 9)) for _ in range(12))
    narration = f"UPI/{ref_digits}/COLLECT/CUSTOMER PAYMENT"
    return narration, ref_digits


# Map for dispatch — keeps make_narration() O(1) and avoids a long if-chain.
_BUILDERS = {
    NarrationTemplate.NEFT_FULL:    _neft_full,
    NarrationTemplate.RTGS_FULL:    _rtgs_full,
    NarrationTemplate.IMPS_FULL:    _imps_full,
    NarrationTemplate.NEFT_ABSENT:  _neft_absent,
    NarrationTemplate.NEFT_TRUNC:   _neft_trunc,
    NarrationTemplate.UPI_NONSETTL: _upi_nonsettl,
}

# Templates that are genuine settlement credits (UTR present or recoverable).
SETTLEMENT_TEMPLATES = frozenset({
    NarrationTemplate.NEFT_FULL,
    NarrationTemplate.RTGS_FULL,
    NarrationTemplate.IMPS_FULL,
    NarrationTemplate.NEFT_ABSENT,
    NarrationTemplate.NEFT_TRUNC,
})

# Templates where the UTR is fully intact in the narration string.
FULL_UTR_TEMPLATES = frozenset({
    NarrationTemplate.NEFT_FULL,
    NarrationTemplate.RTGS_FULL,
    NarrationTemplate.IMPS_FULL,
})


def make_narration(
    template: NarrationTemplate,
    utr: str,
    settlement_date: date,
    rng: random.Random,
) -> tuple[str, str]:
    """Render one narration string for the given template.

    Parameters
    ----------
    template        : which of the six variants to produce
    utr             : the settlement UTR (may be unused by some templates)
    settlement_date : the date the bank credit hits
    rng             : seeded RNG — callers must pass their own; never create one here

    Returns
    -------
    (narration_string, ref_no_string)
    """
    return _BUILDERS[template](utr, settlement_date, rng)


# ---------------------------------------------------------------------------
# Clean-path default
# ---------------------------------------------------------------------------

def clean_narration(utr: str, settlement_date: date, rng: random.Random) -> tuple[str, str]:
    """Convenience wrapper: always returns the NEFT_FULL template.

    Used by generate_clean() so that all clean-path rows produce a complete,
    easily-extractable UTR.  discrepancies.py overrides this for D06/D07 rows.
    """
    return _neft_full(utr, settlement_date, rng)
