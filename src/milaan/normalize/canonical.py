"""Canonical (normalised) internal representations.

Every adapter maps its source schema to one of these models.  Nothing above T0
touches raw CSV columns — this is the single source of truth for field names
and types inside the pipeline.

All monetary values are integer paise.  Timestamps are always datetime objects
with IST offset (+05:30) attached.  Dates are plain date objects (no tz needed).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

IST = timezone(timedelta(hours=5, minutes=30))

UTRConfidence = Literal["exact", "partial", "none"]


def _to_ist(dt: datetime) -> datetime:
    """Attach or convert to IST.  Naive datetimes are assumed to be IST already."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


class CanonicalBankRow(BaseModel, frozen=True):
    """One row from bank.csv after normalisation."""

    row_index: int
    txn_date: date
    value_date: date
    narration: str
    deposit_paise: int
    withdrawal_paise: int
    closing_balance_paise: int
    # UTR extraction results
    utr: str | None
    utr_confidence: UTRConfidence
    # Classification
    is_settlement_credit: bool

    @field_validator("deposit_paise", "withdrawal_paise")
    @classmethod
    def _non_negative_amounts(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"deposit/withdrawal must be >= 0, got {v}")
        return v


class CanonicalSettlementRow(BaseModel, frozen=True):
    """One row from settlement.csv after normalisation."""

    entity_id: str
    entity_type: Literal["payment", "refund", "chargeback", "adjustment"]
    settlement_id: str
    settlement_utr: str
    # credit_paise - debit_paise; negative for refunds/chargebacks
    net_paise: int
    amount_paise: int
    fee_paise: int
    tax_paise: int
    method: str
    order_receipt: str | None
    created_at: datetime   # IST
    settled_at: datetime   # IST

    @model_validator(mode="before")
    @classmethod
    def _canonicalise_timestamps(cls, data: dict) -> dict:
        for field in ("created_at", "settled_at"):
            v = data.get(field)
            if isinstance(v, datetime):
                data[field] = _to_ist(v)
            elif isinstance(v, str):
                data[field] = _to_ist(datetime.fromisoformat(v))
        return data


class CanonicalOrderRow(BaseModel, frozen=True):
    """One row from ledger.csv after normalisation."""

    order_id: str
    order_date: date
    customer_id: str
    gross_paise: int
    currency: str
    status: str
    payment_ref: str | None
    channel: str


class SettlementBatch(BaseModel, frozen=True):
    """A group of settlement rows sharing the same settlement_id.

    Pre-computed for T1: total_net_paise is what the bank credit should equal.
    """

    settlement_id: str
    settlement_utr: str
    total_net_paise: int
    rows: tuple[CanonicalSettlementRow, ...]

    @property
    def entity_ids(self) -> list[str]:
        return [r.entity_id for r in self.rows]

    @property
    def payment_rows(self) -> list[CanonicalSettlementRow]:
        return [r for r in self.rows if r.entity_type == "payment"]

    @property
    def settled_at(self) -> datetime:
        return self.rows[0].settled_at


def group_settlement_rows(rows: list[CanonicalSettlementRow]) -> list[SettlementBatch]:
    """Group individual settlement rows into SettlementBatch objects.

    The total_net_paise = Σ net_paise across all rows in the batch.
    Negative rows (refunds, chargebacks) naturally reduce the sum.
    """
    batches: dict[str, list[CanonicalSettlementRow]] = {}
    for row in rows:
        batches.setdefault(row.settlement_id, []).append(row)

    result: list[SettlementBatch] = []
    for sid, batch_rows in batches.items():
        utr = batch_rows[0].settlement_utr
        total = sum(r.net_paise for r in batch_rows)
        result.append(SettlementBatch(
            settlement_id=sid,
            settlement_utr=utr,
            total_net_paise=total,
            rows=tuple(batch_rows),
        ))
    return result
