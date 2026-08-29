"""Razorpay PSP settlement report adapter.

Maps settlement.csv rows → CanonicalSettlementRow.

net_paise = credit_paise - debit_paise.
  payments:     credit_paise > 0, debit_paise = 0  → positive net
  refunds:      credit_paise = 0, debit_paise > 0  → negative net
  chargebacks:  credit_paise = 0, debit_paise > 0  → negative net
  adjustments:  either sign
"""

from __future__ import annotations

import csv
from pathlib import Path

from milaan.normalize.canonical import CanonicalSettlementRow, SettlementBatch, group_settlement_rows


def load(path: Path) -> list[CanonicalSettlementRow]:
    """Parse settlement.csv and return CanonicalSettlementRow objects.

    Raises ValueError on any row that fails to parse.
    """
    rows: list[CanonicalSettlementRow] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for idx, raw in enumerate(reader):
            try:
                credit = int(raw["credit_paise"])
                debit = int(raw["debit_paise"])
                rows.append(CanonicalSettlementRow(
                    entity_id=raw["entity_id"],
                    entity_type=raw["type"],
                    settlement_id=raw["settlement_id"],
                    settlement_utr=raw["settlement_utr"],
                    net_paise=credit - debit,
                    amount_paise=int(raw["amount_paise"]),
                    fee_paise=int(raw["fee_paise"]),
                    tax_paise=int(raw["tax_paise"]),
                    method=raw["method"],
                    order_receipt=raw["order_receipt"] or None,
                    created_at=raw["created_at"],
                    settled_at=raw["settled_at"],
                ))
            except Exception as exc:
                raise ValueError(f"settlement.csv row {idx}: {exc}") from exc
    return rows


def load_batches(path: Path) -> list[SettlementBatch]:
    """Parse settlement.csv and return pre-grouped SettlementBatch objects."""
    return group_settlement_rows(load(path))
