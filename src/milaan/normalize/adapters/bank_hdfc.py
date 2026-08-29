"""HDFC bank statement adapter.

Maps bank.csv rows → CanonicalBankRow.  One bank, one adapter; adding SBI or
Axis is a new file, not a change to this one.

Heuristic for is_settlement_credit:
  1. "RAZORPAY" appears in the narration (case-insensitive), OR
  2. The extracted UTR confidence is 'exact' (a valid 16-char UTR was found).
Any row matching either condition is treated as a PSP settlement credit.  This
catches D07 (narration says RAZORPAY but UTR is absent) and D06 (UTR truncated).
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from milaan.normalize.canonical import CanonicalBankRow
from milaan.normalize.utr import extract_utr

_RAZORPAY_TOKEN = "RAZORPAY"


def _parse_date(s: str) -> date:
    return date.fromisoformat(s.strip())


def _is_settlement(narration: str, utr_confidence: str) -> bool:
    return _RAZORPAY_TOKEN in narration.upper() or utr_confidence == "exact"


def load(path: Path) -> list[CanonicalBankRow]:
    """Parse bank.csv and return a list of CanonicalBankRow, one per CSV row.

    Raises ValueError on any row that fails to parse rather than silently
    dropping it — the exit gate requires 100% parse rate.
    """
    rows: list[CanonicalBankRow] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for idx, raw in enumerate(reader):
            try:
                utr, confidence = extract_utr(
                    narration=raw["narration"],
                    ref_no=raw["ref_no"],
                )
                rows.append(CanonicalBankRow(
                    row_index=idx,
                    txn_date=_parse_date(raw["txn_date"]),
                    value_date=_parse_date(raw["value_date"]),
                    narration=raw["narration"],
                    deposit_paise=int(raw["deposit_paise"]),
                    withdrawal_paise=int(raw["withdrawal_paise"]),
                    closing_balance_paise=int(raw["closing_balance_paise"]),
                    utr=utr,
                    utr_confidence=confidence,
                    is_settlement_credit=_is_settlement(raw["narration"], confidence),
                ))
            except Exception as exc:
                raise ValueError(f"bank.csv row {idx}: {exc}") from exc
    return rows
