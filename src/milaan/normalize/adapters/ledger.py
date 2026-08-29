"""Internal order ledger adapter.

Maps ledger.csv rows → CanonicalOrderRow.

The ledger is the merchant's own source of truth.  It is the most suspicious
source: D14 (ledger typo) means payment_ref may point to the wrong PSP entity,
and D15 (currency mismatch) means the currency field may say USD while the
settlement is in INR.  The adapter just faithfully transcribes; T3 reconciles.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from milaan.normalize.canonical import CanonicalOrderRow


def load(path: Path) -> list[CanonicalOrderRow]:
    """Parse ledger.csv and return CanonicalOrderRow objects.

    Raises ValueError on any row that fails to parse.
    """
    rows: list[CanonicalOrderRow] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for idx, raw in enumerate(reader):
            try:
                rows.append(CanonicalOrderRow(
                    order_id=raw["order_id"],
                    order_date=date.fromisoformat(raw["order_date"]),
                    customer_id=raw["customer_id"],
                    gross_paise=int(raw["gross_amount_paise"]),
                    currency=raw["currency"],
                    status=raw["status"],
                    payment_ref=raw["payment_ref"] or None,
                    channel=raw["channel"],
                ))
            except Exception as exc:
                raise ValueError(f"ledger.csv row {idx}: {exc}") from exc
    return rows
