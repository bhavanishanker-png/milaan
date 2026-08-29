"""Clean-path synthetic data generator: orders → payments → settlement batches → bank credits.

No discrepancies are injected here. discrepancies.py mutates the output of this module.

All monetary values are integers (paise). No floats are used or produced.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from milaan.normalize.fees import compute_net
from milaan.normalize.money import Paise
from milaan.generate.narration import clean_narration

# IST = UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))

# Payment method distribution — UPI dominates Indian checkout volumes.
_METHODS = ["upi", "card", "netbanking", "wallet", "emi"]
_METHOD_WEIGHTS = [40, 30, 15, 10, 5]

_CHANNELS = ["web", "app", "pos"]
_CHANNEL_WEIGHTS = [50, 40, 10]

# Gross amount range in paise: ₹100 – ₹50,000
_GROSS_MIN = 10_000
_GROSS_MAX = 5_000_000

_NUM_CUSTOMERS = 50

# Opening closing balance for the bank statement (₹5,00,000)
_OPENING_BALANCE = Paise(50_000_000)


# ---------------------------------------------------------------------------
# Row types — one per CSV column spec (SPEC.md §3)
# ---------------------------------------------------------------------------


@dataclass
class OrderRow:
    """One row in ledger.csv — the merchant's internal order record."""

    order_id: str
    order_date: str          # YYYY-MM-DD
    customer_id: str
    gross_amount_paise: int
    currency: str
    status: str
    payment_ref: str         # PSP entity_id; nullable in discrepancy cases
    channel: str


@dataclass
class SettlementRow:
    """One row in settlement.csv — one money-moving entity in a batch."""

    entity_id: str
    type: str                # payment | refund | chargeback | adjustment
    debit_paise: int
    credit_paise: int
    amount_paise: int        # gross
    fee_paise: int
    tax_paise: int           # GST on fee
    settlement_id: str
    settlement_utr: str
    created_at: str          # ISO-8601 with IST offset
    settled_at: str
    method: str
    order_receipt: str       # merchant's order_id, nullable in some cases
    notes: str


@dataclass
class BankRow:
    """One row in bank.csv — the merchant's bank statement line."""

    txn_date: str            # YYYY-MM-DD
    value_date: str
    narration: str           # free text; UTR is buried in here
    ref_no: str
    withdrawal_paise: int
    deposit_paise: int
    closing_balance_paise: int


@dataclass
class BatchRecord:
    """Internal record linking a settlement batch to its bank row.

    Used by labels.py to build the ground-truth file. Never written to the
    bank/settlement/ledger CSVs.
    """

    settlement_id: str
    settlement_utr: str
    bank_row_index: int              # 0-based index into bank_rows list; updated by cli.py after sort
    payment_entity_ids: list[str]
    order_ids: list[str]
    total_net_paise: int
    # Populated by discrepancy injectors:
    refund_entity_ids: list[str] = field(default_factory=list)
    chargeback_entity_ids: list[str] = field(default_factory=list)
    adjustment_entity_ids: list[str] = field(default_factory=list)
    disc_codes: list[str] = field(default_factory=list)  # e.g. ["D03", "D06"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_utr(settlement_date: date, counter: int) -> str:
    """Produce a deterministic UTR string.

    Format: N{YY}{MM}{DD}{counter:09d}  (16 chars total)
    e.g.    N260803000000001

    This mirrors NEFT UTR conventions closely enough to be convincing and is
    trivially extractable with a regex (T0 normaliser will use r'N\\d{15}').
    """
    return f"N{settlement_date.strftime('%y%m%d')}{counter:09d}"


def make_ist_ts(d: date, hour: int, minute: int) -> str:
    """Return an ISO-8601 timestamp at the given HH:MM IST on date d."""
    return datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=_IST).isoformat()


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


def generate_clean(
    seed: int,
    n_orders: int,
    start_date: date,
    spread_days: int = 7,
) -> tuple[list[OrderRow], list[SettlementRow], list[BankRow], list[BatchRecord]]:
    """Generate a fully clean (no discrepancies) synthetic batch.

    Parameters
    ----------
    seed        : random seed — same seed → byte-identical output
    n_orders    : number of payment orders to generate
    start_date  : first possible order_date
    spread_days : orders are distributed uniformly over this many days

    Returns
    -------
    (orders, settlement_rows, bank_rows, batch_records)

    All monetary values are integer paise. No floats.
    """
    rng = random.Random(seed)
    customers = [f"CUST-{i:04d}" for i in range(1, _NUM_CUSTOMERS + 1)]

    # ------------------------------------------------------------------
    # Step 1: Create orders (no settlement info yet — that depends on grouping)
    # ------------------------------------------------------------------
    # We record the payment method alongside each order so the fee calculation
    # is consistent when we build the settlement rows.

    orders: list[OrderRow] = []
    _order_methods: list[str] = []     # parallel list — same index as orders

    for i in range(n_orders):
        day_offset = rng.randint(0, spread_days - 1)
        order_date = start_date + timedelta(days=day_offset)
        gross_paise = rng.randint(_GROSS_MIN, _GROSS_MAX)
        method = rng.choices(_METHODS, weights=_METHOD_WEIGHTS, k=1)[0]
        channel = rng.choices(_CHANNELS, weights=_CHANNEL_WEIGHTS, k=1)[0]
        customer = rng.choice(customers)

        order_id = f"ORD-2026-{i + 1:06d}"
        pay_id = f"pay_{i + 1:06d}"

        orders.append(OrderRow(
            order_id=order_id,
            order_date=order_date.isoformat(),
            customer_id=customer,
            gross_amount_paise=gross_paise,
            currency="INR",
            status="paid",
            payment_ref=pay_id,
            channel=channel,
        ))
        _order_methods.append(method)

    # ------------------------------------------------------------------
    # Step 2: Group orders by order_date → settlement batches
    # ------------------------------------------------------------------
    date_groups: dict[date, list[int]] = {}
    for idx, order in enumerate(orders):
        d = date.fromisoformat(order.order_date)
        date_groups.setdefault(d, []).append(idx)

    # ------------------------------------------------------------------
    # Step 3: Build settlement rows and bank rows for each batch
    # ------------------------------------------------------------------
    settlement_rows: list[SettlementRow] = []
    bank_rows: list[BankRow] = []
    batch_records: list[BatchRecord] = []

    closing_balance = _OPENING_BALANCE
    utr_counter = 1
    setl_counter = 1

    for ord_date in sorted(date_groups.keys()):
        indices = date_groups[ord_date]
        settlement_date = ord_date + timedelta(days=2)

        setl_id = f"setl_{setl_counter:04d}"
        utr = _make_utr(settlement_date, utr_counter)
        setl_counter += 1
        utr_counter += 1

        batch_nets: list[Paise] = []
        batch_entity_ids: list[str] = []
        batch_order_ids: list[str] = []

        for oi in indices:
            order = orders[oi]
            method = _order_methods[oi]
            gross = Paise(order.gross_amount_paise)
            bd = compute_net(gross, method)

            # Randomise the transaction time within business hours on order_date
            hour = rng.randint(8, 22)
            minute = rng.randint(0, 59)

            settlement_rows.append(SettlementRow(
                entity_id=order.payment_ref,
                type="payment",
                debit_paise=0,
                credit_paise=bd.net.value,
                amount_paise=bd.gross.value,
                fee_paise=bd.fee.value,
                tax_paise=bd.gst.value,
                settlement_id=setl_id,
                settlement_utr=utr,
                created_at=make_ist_ts(ord_date, hour, minute),
                settled_at=make_ist_ts(settlement_date, 6, 0),
                method=method,
                order_receipt=order.order_id,
                notes="",
            ))

            batch_nets.append(bd.net)
            batch_entity_ids.append(order.payment_ref)
            batch_order_ids.append(order.order_id)

        total_net = Paise.sum(batch_nets)
        closing_balance = closing_balance + total_net

        narration_str, ref_no = clean_narration(utr, settlement_date, rng)
        bank_rows.append(BankRow(
            txn_date=settlement_date.isoformat(),
            value_date=settlement_date.isoformat(),
            narration=narration_str,
            ref_no=ref_no,
            withdrawal_paise=0,
            deposit_paise=total_net.value,
            closing_balance_paise=closing_balance.value,
        ))

        batch_records.append(BatchRecord(
            settlement_id=setl_id,
            settlement_utr=utr,
            bank_row_index=len(bank_rows) - 1,
            payment_entity_ids=batch_entity_ids,
            order_ids=batch_order_ids,
            total_net_paise=total_net.value,
        ))

    return orders, settlement_rows, bank_rows, batch_records
