"""Unrelated bank statement lines — rent, GST, sweeps, direct customer NEFTs.

The spec (§3.3) requires genuinely unrelated rows because a reconciler that
assumes every credit is a PSP settlement is not a reconciler.  These are
distinct from D11 (injected as a discrepancy with a label record); noise rows
are structural background and carry no label entry.

All amounts are integer paise. No floats.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from milaan.generate.entities import BankRow

# Each entry: (narration template with {ref}, is_debit, min_paise, max_paise)
_NOISE_TEMPLATES = [
    ("HDFC DEBIT-OFFICE RENT/{ref}/LANDLORD PROP", True,  50_000_000,  200_000_000),
    ("GST/E-PAY/{ref}/NSDL PORTAL",               True,  1_000_000,    50_000_000),
    ("NEFT CR-SBIN0001234-DIRECT NEFT/{ref}",      False, 10_000,        5_000_000),
    ("IFT/SWEEP-OUT/{ref}/ACME SAVINGS",           True,  100_000_000,  500_000_000),
    ("ECS/LOAN-EMI/{ref}/HDFC BANK",               True,  5_000_000,    20_000_000),
]


def generate_noise_rows(
    rng: random.Random,
    date_range: tuple[date, date],
    n_rows: int,
) -> list[BankRow]:
    """Return n_rows noise bank rows spread uniformly across date_range.

    closing_balance_paise is set to 0 for all rows — cli.py will recompute
    all closing balances in one pass after merging noise with settlement credits.
    """
    start, end = date_range
    span = max(1, (end - start).days)
    rows: list[BankRow] = []
    for _ in range(n_rows):
        tmpl, is_debit, lo, hi = rng.choice(_NOISE_TEMPLATES)
        amount = rng.randint(lo, hi)
        offset = rng.randint(0, span)
        txn_date = (start + timedelta(days=offset)).isoformat()
        ref = str(rng.randint(100_000, 999_999))
        narration = tmpl.format(ref=ref)
        rows.append(BankRow(
            txn_date=txn_date,
            value_date=txn_date,
            narration=narration,
            ref_no=ref,
            withdrawal_paise=amount if is_debit else 0,
            deposit_paise=0 if is_debit else amount,
            closing_balance_paise=0,    # recomputed by cli.py
        ))
    return rows
