"""Discrepancy injectors — one function per D-code, D01 through D15.

Each injector mutates the entity lists produced by generate_clean() in place
and returns a label dict recording exactly what changed.  The caller (inject_all)
selects which batches/orders to target; injectors only perform the mutation.

All monetary arithmetic uses integer paise via Paise objects. No floats.

SPEC.md §4 defines all 15 classes and the tier expected to handle each.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone

from milaan.normalize.fees import compute_net
from milaan.normalize.money import Paise
from milaan.generate.entities import (
    BatchRecord,
    BankRow,
    OrderRow,
    SettlementRow,
    _OPENING_BALANCE,
    make_ist_ts,
)
from milaan.generate.narration import NarrationTemplate, make_narration

_IST = timezone(timedelta(hours=5, minutes=30))

# D-code metadata (class name, expected tier, expected auto/escalate action)
_META: dict[str, tuple[str, str, str]] = {
    "D01": ("TIMING_LAG",          "T2",  "auto_resolve"),
    "D02": ("FEE_ROUNDING_DRIFT",  "T2",  "auto_resolve"),
    "D03": ("NETTED_REFUND",       "T2",  "auto_resolve"),
    "D04": ("CHARGEBACK_DEBIT",    "T3",  "escalate"),
    "D05": ("DUPLICATE_PAYMENT",   "T3",  "escalate"),
    "D06": ("MANGLED_NARRATION",   "T1",  "auto_resolve"),
    "D07": ("MISSING_NARRATION",   "T2",  "auto_resolve"),
    "D08": ("SPLIT_PAYMENT",       "T3",  "escalate"),
    "D09": ("INTERNATIONAL_TXN",   "T3",  "escalate"),
    "D10": ("NEVER_SETTLED",       "T2",  "escalate"),
    "D11": ("UNIDENTIFIED_CREDIT", "T3",  "escalate"),
    "D12": ("PARTIAL_REFUND",      "T3",  "escalate"),
    "D13": ("ADJUSTMENT_ENTRY",    "T3",  "escalate"),
    "D14": ("LEDGER_TYPO",         "T3",  "escalate"),
    "D15": ("CURRENCY_MISMATCH",   "T3",  "escalate"),
}


def _label(code: str, disc_id: str, br: BatchRecord, entities: list[str], detail: dict, resolution: str) -> dict:
    class_name, tier, action = _META[code]
    return {
        "id": disc_id,
        "code": code,
        "class": class_name,
        "settlement_utr": br.settlement_utr if br else None,
        "settlement_id": br.settlement_id if br else None,
        "entities": entities,
        "expected_resolution": resolution,
        "expected_action": action,
        "expected_tier": tier,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Injection context
# ---------------------------------------------------------------------------

@dataclass
class InjectionCtx:
    """Mutable state shared across all injectors."""

    orders: list[OrderRow]
    settlement_rows: list[SettlementRow]
    bank_rows: list[BankRow]
    batch_records: list[BatchRecord]
    rng: random.Random
    opening_balance_paise: int = field(default_factory=lambda: _OPENING_BALANCE.value)
    _next_num: int = field(default=0, init=False)
    _disc_counter: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        nums = [
            int(r.entity_id.split("_", 1)[1])
            for r in self.settlement_rows
            if "_" in r.entity_id and r.entity_id.split("_", 1)[1].isdigit()
        ]
        self._next_num = max(nums, default=0) + 1

    def new_entity(self, prefix: str) -> str:
        eid = f"{prefix}{self._next_num:06d}"
        self._next_num += 1
        return eid

    def new_disc_id(self) -> str:
        did = f"D-{self._disc_counter:04d}"
        self._disc_counter += 1
        return did

    def rows_for(self, settlement_id: str) -> list[SettlementRow]:
        return [r for r in self.settlement_rows if r.settlement_id == settlement_id]

    def find_pay(self, entity_id: str) -> SettlementRow:
        return next(r for r in self.settlement_rows if r.entity_id == entity_id)

    def find_order(self, order_id: str) -> OrderRow | None:
        return next((o for o in self.orders if o.order_id == order_id), None)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _recompute_balances(bank_rows: list[BankRow], start: int, opening: int) -> None:
    """Recompute closing_balance_paise from start index onwards."""
    prev = bank_rows[start - 1].closing_balance_paise if start > 0 else opening
    for row in bank_rows[start:]:
        prev = prev + row.deposit_paise - row.withdrawal_paise
        row.closing_balance_paise = prev


def _bank_delta(ctx: InjectionCtx, batch_idx: int, delta: int) -> None:
    """Adjust a batch's bank deposit by delta paise and recompute downstream balances."""
    br = ctx.batch_records[batch_idx]
    bi = br.bank_row_index
    ctx.bank_rows[bi].deposit_paise += delta
    br.total_net_paise += delta
    _recompute_balances(ctx.bank_rows, bi, ctx.opening_balance_paise)


def _settled_ts(ctx: InjectionCtx, batch_idx: int) -> str:
    bank = ctx.bank_rows[ctx.batch_records[batch_idx].bank_row_index]
    d = date.fromisoformat(bank.txn_date)
    return make_ist_ts(d, 6, 0)


# ---------------------------------------------------------------------------
# D01 — Timing lag: bank credit slips into the following month
# ---------------------------------------------------------------------------

def inject_D01(ctx: InjectionCtx, batch_idx: int) -> dict:
    br = ctx.batch_records[batch_idx]
    bank = ctx.bank_rows[br.bank_row_index]
    orig = date.fromisoformat(bank.txn_date)
    # Push to the first of the next month (simulates T+2 crossing month-end).
    if orig.month == 12:
        new_d = date(orig.year + 1, 1, 1)
    else:
        new_d = date(orig.year, orig.month + 1, 1)
    bank.txn_date = new_d.isoformat()
    bank.value_date = new_d.isoformat()
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D01")
    return _label("D01", disc_id, br, list(br.payment_entity_ids),
                  {"orig_date": orig.isoformat(), "shifted_to": new_d.isoformat()},
                  "TIMING_LAG_MONTH_BOUNDARY")


# ---------------------------------------------------------------------------
# D02 — Fee rounding drift: ±1-3 paise on the bank credit
# ---------------------------------------------------------------------------

def inject_D02(ctx: InjectionCtx, batch_idx: int) -> dict:
    delta = ctx.rng.choice([-3, -2, -1, 1, 2, 3])
    _bank_delta(ctx, batch_idx, delta)
    br = ctx.batch_records[batch_idx]
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D02")
    return _label("D02", disc_id, br, list(br.payment_entity_ids),
                  {"drift_paise": delta}, "WITHIN_TOLERANCE")


# ---------------------------------------------------------------------------
# D03 — Netted refund: full refund row inside the settlement batch
# ---------------------------------------------------------------------------

def inject_D03(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if not br.payment_entity_ids:
        return None
    pay_id = ctx.rng.choice(br.payment_entity_ids)
    pay = ctx.find_pay(pay_id)
    rfnd_id = ctx.new_entity("rfnd_")
    ctx.settlement_rows.append(SettlementRow(
        entity_id=rfnd_id,
        type="refund",
        debit_paise=pay.amount_paise,
        credit_paise=0,
        amount_paise=pay.amount_paise,
        fee_paise=0,
        tax_paise=0,
        settlement_id=br.settlement_id,
        settlement_utr=br.settlement_utr,
        created_at=pay.created_at,
        settled_at=pay.settled_at,
        method=pay.method,
        order_receipt=pay.order_receipt,
        notes="FULL_REFUND",
    ))
    order = ctx.find_order(pay.order_receipt)
    if order:
        order.status = "refunded"
    _bank_delta(ctx, batch_idx, -pay.amount_paise)
    br.refund_entity_ids.append(rfnd_id)
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D03")
    return _label("D03", disc_id, br, [rfnd_id],
                  {"refunded_payment": pay_id, "refund_gross_paise": pay.amount_paise},
                  "REFUND_NETTED_IN_BATCH")


# ---------------------------------------------------------------------------
# D04 — Chargeback debit: dispute row with amount + PSP fee
# ---------------------------------------------------------------------------

def inject_D04(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if not br.payment_entity_ids:
        return None
    pay_id = ctx.rng.choice(br.payment_entity_ids)
    pay = ctx.find_pay(pay_id)
    cb_amount = pay.amount_paise
    cb_fee = 25_000          # ₹250 flat PSP dispute fee
    disp_id = ctx.new_entity("disp_")
    ctx.settlement_rows.append(SettlementRow(
        entity_id=disp_id,
        type="chargeback",
        debit_paise=cb_amount + cb_fee,
        credit_paise=0,
        amount_paise=cb_amount,
        fee_paise=cb_fee,
        tax_paise=0,
        settlement_id=br.settlement_id,
        settlement_utr=br.settlement_utr,
        created_at=pay.created_at,
        settled_at=pay.settled_at,
        method=pay.method,
        order_receipt=pay.order_receipt,
        notes="CHARGEBACK",
    ))
    _bank_delta(ctx, batch_idx, -(cb_amount + cb_fee))
    br.chargeback_entity_ids.append(disp_id)
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D04")
    return _label("D04", disc_id, br, [disp_id],
                  {"disputed_payment": pay_id,
                   "chargeback_amount_paise": cb_amount,
                   "chargeback_fee_paise": cb_fee},
                  "CHARGEBACK_NETTED")


# ---------------------------------------------------------------------------
# D05 — Duplicate payment: identical payment row for the same order
# ---------------------------------------------------------------------------

def inject_D05(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if not br.payment_entity_ids:
        return None
    pay_id = ctx.rng.choice(br.payment_entity_ids)
    pay = ctx.find_pay(pay_id)
    dup_id = ctx.new_entity("pay_")
    ctx.settlement_rows.append(SettlementRow(
        entity_id=dup_id,
        type="payment",
        debit_paise=0,
        credit_paise=pay.credit_paise,
        amount_paise=pay.amount_paise,
        fee_paise=pay.fee_paise,
        tax_paise=pay.tax_paise,
        settlement_id=br.settlement_id,
        settlement_utr=br.settlement_utr,
        created_at=pay.created_at,
        settled_at=pay.settled_at,
        method=pay.method,
        order_receipt=pay.order_receipt,
        notes="DUPLICATE",
    ))
    _bank_delta(ctx, batch_idx, pay.credit_paise)
    br.payment_entity_ids.append(dup_id)
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D05")
    return _label("D05", disc_id, br, [dup_id],
                  {"original_payment": pay_id, "duplicate_payment": dup_id,
                   "order_id": pay.order_receipt},
                  "DUPLICATE_PAYMENT_IDENTIFIED")


# ---------------------------------------------------------------------------
# D06 — Mangled narration: UTR truncated in the bank statement
# ---------------------------------------------------------------------------

def inject_D06(ctx: InjectionCtx, batch_idx: int) -> dict:
    br = ctx.batch_records[batch_idx]
    bank = ctx.bank_rows[br.bank_row_index]
    settlement_date = date.fromisoformat(bank.txn_date)
    narr, ref = make_narration(
        NarrationTemplate.NEFT_TRUNC, br.settlement_utr, settlement_date, ctx.rng
    )
    bank.narration = narr
    bank.ref_no = ref
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D06")
    return _label("D06", disc_id, br, list(br.payment_entity_ids),
                  {"truncated_ref": ref, "full_utr": br.settlement_utr},
                  "FUZZY_UTR_MATCH")


# ---------------------------------------------------------------------------
# D07 — Missing narration: no UTR anywhere in the bank row
# ---------------------------------------------------------------------------

def inject_D07(ctx: InjectionCtx, batch_idx: int) -> dict:
    br = ctx.batch_records[batch_idx]
    bank = ctx.bank_rows[br.bank_row_index]
    settlement_date = date.fromisoformat(bank.txn_date)
    narr, ref = make_narration(
        NarrationTemplate.NEFT_ABSENT, br.settlement_utr, settlement_date, ctx.rng
    )
    bank.narration = narr
    bank.ref_no = ref
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D07")
    return _label("D07", disc_id, br, list(br.payment_entity_ids),
                  {"narration": narr, "full_utr": br.settlement_utr},
                  "AMOUNT_DATE_MATCH")


# ---------------------------------------------------------------------------
# D08 — Split payment: one order, two payment rows (60/40 gross split)
# ---------------------------------------------------------------------------

def inject_D08(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if not br.payment_entity_ids:
        return None
    pay_id = ctx.rng.choice(br.payment_entity_ids)
    pay_idx = next(i for i, r in enumerate(ctx.settlement_rows) if r.entity_id == pay_id)
    pay = ctx.settlement_rows[pay_idx]
    gross = pay.amount_paise

    # 60/40 split — integer division, no floats
    gross1 = gross * 6 // 10
    gross2 = gross - gross1
    bd1 = compute_net(Paise(gross1), pay.method if pay.method else "card")
    bd2 = compute_net(Paise(gross2), pay.method if pay.method else "card")

    sp1_id = ctx.new_entity("pay_")
    sp2_id = ctx.new_entity("pay_")
    for eid, bd in ((sp1_id, bd1), (sp2_id, bd2)):
        ctx.settlement_rows.append(SettlementRow(
            entity_id=eid,
            type="payment",
            debit_paise=0,
            credit_paise=bd.net.value,
            amount_paise=bd.gross.value,
            fee_paise=bd.fee.value,
            tax_paise=bd.gst.value,
            settlement_id=br.settlement_id,
            settlement_utr=br.settlement_utr,
            created_at=pay.created_at,
            settled_at=pay.settled_at,
            method=pay.method,
            order_receipt=pay.order_receipt,
            notes="SPLIT",
        ))

    ctx.settlement_rows.pop(pay_idx)
    br.payment_entity_ids.remove(pay_id)
    br.payment_entity_ids.extend([sp1_id, sp2_id])

    delta = bd1.net.value + bd2.net.value - pay.credit_paise
    if delta != 0:
        _bank_delta(ctx, batch_idx, delta)

    order = ctx.find_order(pay.order_receipt)
    if order:
        order.payment_ref = sp1_id

    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D08")
    return _label("D08", disc_id, br, [sp1_id, sp2_id],
                  {"original_payment": pay_id,
                   "split_payments": [sp1_id, sp2_id],
                   "gross_split_paise": [gross1, gross2],
                   "order_id": pay.order_receipt},
                  "SPLIT_PAYMENT_CONSOLIDATED")


# ---------------------------------------------------------------------------
# D09 — International transaction: higher fee slab, ledger currency changed
# ---------------------------------------------------------------------------

def inject_D09(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if not br.payment_entity_ids:
        return None
    pay_id = ctx.rng.choice(br.payment_entity_ids)
    pay = ctx.find_pay(pay_id)
    old_net = pay.credit_paise
    new_bd = compute_net(Paise(pay.amount_paise), "international")
    delta = new_bd.net.value - old_net
    pay.method = "international"
    pay.fee_paise = new_bd.fee.value
    pay.tax_paise = new_bd.gst.value
    pay.credit_paise = new_bd.net.value
    pay.notes = "INTERNATIONAL_TXN"
    if delta != 0:
        _bank_delta(ctx, batch_idx, delta)
    order = ctx.find_order(pay.order_receipt)
    if order:
        order.currency = "USD"
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D09")
    return _label("D09", disc_id, br, [pay_id],
                  {"new_method": "international", "net_delta_paise": delta,
                   "order_id": pay.order_receipt},
                  "INTERNATIONAL_FEE_RECALCULATED")


# ---------------------------------------------------------------------------
# D10 — Never settled: ledger order captured but no settlement row
# ---------------------------------------------------------------------------

def inject_D10(ctx: InjectionCtx, order_counter: list[int]) -> dict:
    order_num = order_counter[0]
    order_counter[0] += 1
    order_id = f"ORD-2026-{order_num:06d}"
    pay_id = ctx.new_entity("pay_")
    order_date = (
        ctx.rng.choice(ctx.orders).order_date if ctx.orders else "2026-08-01"
    )
    gross = ctx.rng.randint(50_000, 1_000_000)
    ctx.orders.append(OrderRow(
        order_id=order_id,
        order_date=order_date,
        customer_id=f"CUST-{ctx.rng.randint(1, 50):04d}",
        gross_amount_paise=gross,
        currency="INR",
        status="paid",
        payment_ref=pay_id,
        channel=ctx.rng.choice(["web", "app"]),
    ))
    # Deliberately no settlement row added — the payment was captured but never batched.
    disc_id = ctx.new_disc_id()
    return {
        "id": disc_id,
        "code": "D10",
        "class": _META["D10"][0],
        "settlement_utr": None,
        "settlement_id": None,
        "entities": [pay_id],
        "expected_resolution": "PAYMENT_NEVER_SETTLED",
        "expected_action": _META["D10"][2],
        "expected_tier": _META["D10"][1],
        "detail": {"order_id": order_id, "gross_paise": gross, "order_date": order_date},
    }


# ---------------------------------------------------------------------------
# D11 — Unidentified credit: bank row with no PSP settlement behind it
# ---------------------------------------------------------------------------

def inject_D11(ctx: InjectionCtx) -> dict:
    deposit = ctx.rng.randint(100_000, 50_000_000)
    txn_date = (
        ctx.rng.choice(ctx.bank_rows).txn_date if ctx.bank_rows else "2026-08-05"
    )
    narr, ref_no = make_narration(
        NarrationTemplate.UPI_NONSETTL, "", date.fromisoformat(txn_date), ctx.rng
    )
    last = (
        ctx.bank_rows[-1].closing_balance_paise if ctx.bank_rows
        else ctx.opening_balance_paise
    )
    ctx.bank_rows.append(BankRow(
        txn_date=txn_date,
        value_date=txn_date,
        narration=narr,
        ref_no=ref_no,
        withdrawal_paise=0,
        deposit_paise=deposit,
        closing_balance_paise=last + deposit,   # cli.py recomputes after sort
    ))
    disc_id = ctx.new_disc_id()
    return {
        "id": disc_id,
        "code": "D11",
        "class": _META["D11"][0],
        "settlement_utr": None,
        "settlement_id": None,
        "entities": [],
        "expected_resolution": "NON_PSP_CREDIT",
        "expected_action": _META["D11"][2],
        "expected_tier": _META["D11"][1],
        "detail": {"bank_narration": narr, "deposit_paise": deposit, "txn_date": txn_date},
    }


# ---------------------------------------------------------------------------
# D12 — Partial refund: refund amount ≠ order gross
# ---------------------------------------------------------------------------

def inject_D12(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if not br.payment_entity_ids:
        return None
    pay_id = ctx.rng.choice(br.payment_entity_ids)
    pay = ctx.find_pay(pay_id)
    pct = ctx.rng.randint(20, 80)
    refund_gross = pay.amount_paise * pct // 100   # integer division, no floats
    rfnd_id = ctx.new_entity("rfnd_")
    ctx.settlement_rows.append(SettlementRow(
        entity_id=rfnd_id,
        type="refund",
        debit_paise=refund_gross,
        credit_paise=0,
        amount_paise=refund_gross,
        fee_paise=0,
        tax_paise=0,
        settlement_id=br.settlement_id,
        settlement_utr=br.settlement_utr,
        created_at=pay.created_at,
        settled_at=pay.settled_at,
        method=pay.method,
        order_receipt=pay.order_receipt,
        notes="PARTIAL_REFUND",
    ))
    order = ctx.find_order(pay.order_receipt)
    if order:
        order.status = "partially_refunded"
    _bank_delta(ctx, batch_idx, -refund_gross)
    br.refund_entity_ids.append(rfnd_id)
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D12")
    return _label("D12", disc_id, br, [rfnd_id],
                  {"payment": pay_id, "refund_pct": pct, "refund_gross_paise": refund_gross},
                  "PARTIAL_REFUND_IDENTIFIED")


# ---------------------------------------------------------------------------
# D13 — Adjustment entry: PSP correction with no corresponding order
# ---------------------------------------------------------------------------

def inject_D13(ctx: InjectionCtx, batch_idx: int) -> dict:
    br = ctx.batch_records[batch_idx]
    amount = ctx.rng.randint(5_000, 50_000)
    is_credit = ctx.rng.choice([True, False])
    net_delta = amount if is_credit else -amount
    adj_id = ctx.new_entity("adj_")
    settled_ts = _settled_ts(ctx, batch_idx)
    ctx.settlement_rows.append(SettlementRow(
        entity_id=adj_id,
        type="adjustment",
        debit_paise=0 if is_credit else amount,
        credit_paise=amount if is_credit else 0,
        amount_paise=amount,
        fee_paise=0,
        tax_paise=0,
        settlement_id=br.settlement_id,
        settlement_utr=br.settlement_utr,
        created_at=settled_ts,
        settled_at=settled_ts,
        method="",
        order_receipt="",
        notes="PSP_ADJUSTMENT",
    ))
    _bank_delta(ctx, batch_idx, net_delta)
    br.adjustment_entity_ids.append(adj_id)
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D13")
    return _label("D13", disc_id, br, [adj_id],
                  {"adjustment_paise": amount, "is_credit": is_credit,
                   "net_delta_paise": net_delta},
                  "ADJUSTMENT_CLASSIFIED")


# ---------------------------------------------------------------------------
# D14 — Ledger typo: order's payment_ref points to a sibling entity
# ---------------------------------------------------------------------------

def inject_D14(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if len(br.payment_entity_ids) < 2:
        return None
    # Find orders whose payment_ref lands inside this batch's entity list.
    # (D08 changes payment_ref to sp1_id, so we search the full entity list.)
    eligible = [
        o for o in ctx.orders
        if o.payment_ref in br.payment_entity_ids and o.order_id in br.order_ids
    ]
    if not eligible:
        return None
    order_a = ctx.rng.choice(eligible)
    other = [e for e in br.payment_entity_ids if e != order_a.payment_ref]
    if not other:
        return None
    pay_b = ctx.rng.choice(other)
    correct_ref = order_a.payment_ref
    order_a.payment_ref = pay_b        # now points to wrong entity
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D14")
    return _label("D14", disc_id, br, [correct_ref, pay_b],
                  {"order_id": order_a.order_id,
                   "correct_payment_ref": correct_ref,
                   "wrong_payment_ref": pay_b},
                  "LEDGER_REF_CORRECTED")


# ---------------------------------------------------------------------------
# D15 — Currency mismatch: ledger says USD, settlement stays in INR
# ---------------------------------------------------------------------------

def inject_D15(ctx: InjectionCtx, batch_idx: int) -> dict | None:
    br = ctx.batch_records[batch_idx]
    if not br.payment_entity_ids:
        return None
    pay_id = ctx.rng.choice(br.payment_entity_ids)
    pay = ctx.find_pay(pay_id)
    order = next(
        (o for o in ctx.orders
         if o.order_id == pay.order_receipt and o.currency == "INR"),
        None,
    )
    if not order:
        return None
    order.currency = "USD"
    disc_id = ctx.new_disc_id()
    br.disc_codes.append("D15")
    return _label("D15", disc_id, br, [pay_id],
                  {"order_id": order.order_id,
                   "ledger_currency": "USD",
                   "settlement_currency": "INR"},
                  "CURRENCY_MISMATCH_FX_CONVERSION")


# ---------------------------------------------------------------------------
# Unresolvable case: bank credit with no data anywhere
# ---------------------------------------------------------------------------

def inject_unresolvable(ctx: InjectionCtx, idx: int) -> dict:
    """Add a bank credit that cannot be explained by any data in the system."""
    deposit = ctx.rng.randint(1_000_000, 10_000_000)
    txn_date = (
        ctx.rng.choice(ctx.bank_rows).txn_date if ctx.bank_rows else "2026-08-15"
    )
    fake_utr = f"NORESOL{idx:04d}"
    last = (
        ctx.bank_rows[-1].closing_balance_paise if ctx.bank_rows
        else ctx.opening_balance_paise
    )
    ctx.bank_rows.append(BankRow(
        txn_date=txn_date,
        value_date=txn_date,
        narration=f"NEFT CR-ICIC0000001-UNKNOWN ENTITY-{fake_utr}",
        ref_no=fake_utr,
        withdrawal_paise=0,
        deposit_paise=deposit,
        closing_balance_paise=last + deposit,
    ))
    disc_id = ctx.new_disc_id()
    return {
        "id": disc_id,
        "code": "UNRESOLVABLE",
        "class": "UNRESOLVABLE",
        "settlement_utr": fake_utr,
        "settlement_id": None,
        "entities": [],
        "expected_resolution": "UNRESOLVABLE",
        "expected_action": "escalate",
        "expected_tier": "T3",
        "detail": {"deposit_paise": deposit,
                   "reason": "No settlement rows or ledger orders match this credit"},
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def inject_all(
    orders: list[OrderRow],
    settlement_rows: list[SettlementRow],
    bank_rows: list[BankRow],
    batch_records: list[BatchRecord],
    rng: random.Random,
    n_unresolvable: int = 2,
) -> list[dict]:
    """Inject all 15 D-codes and add unresolvable cases.

    Target: ~15% of batches per code, minimum 1 per code so every batch has all
    15 D-codes represented when the batch is large enough.  For small batches
    (test runs with 20 records) some codes will share a batch; that's expected.
    """
    ctx = InjectionCtx(
        orders=orders,
        settlement_rows=settlement_rows,
        bank_rows=bank_rows,
        batch_records=batch_records,
        rng=rng,
    )
    labels: list[dict] = []
    n_batches = len(batch_records)
    # One injection per D-code keeps each code present (exit gate) while leaving
    # ~70-80% of batches clean for the T1 baseline (PHASES.md §P3 requirement).
    # Scaling: for very large batch sets (n_batches > 50) we allow up to 2 per code.
    per_code = max(1, n_batches // 25)

    def _sample(k: int) -> list[int]:
        return rng.sample(range(n_batches), min(k, n_batches))

    def _fat(k: int) -> list[int]:
        """Batches with ≥2 payments (needed for D05, D08, D14)."""
        eligible = [i for i, br in enumerate(batch_records)
                    if len(br.payment_entity_ids) >= 2]
        return rng.sample(eligible, min(k, len(eligible))) if eligible else []

    # Batch-level codes (any batch)
    for code, fn in [
        ("D01", inject_D01), ("D02", inject_D02), ("D03", inject_D03),
        ("D04", inject_D04), ("D06", inject_D06), ("D07", inject_D07),
        ("D09", inject_D09), ("D12", inject_D12), ("D13", inject_D13),
        ("D15", inject_D15),
    ]:
        for bi in _sample(per_code):
            result = fn(ctx, bi)
            if result:
                labels.append(result)

    # Codes that need ≥2 payments per batch
    for code, fn in [("D05", inject_D05), ("D08", inject_D08), ("D14", inject_D14)]:
        for bi in _fat(per_code):
            result = fn(ctx, bi)
            if result:
                labels.append(result)

    # D10 — new orders with no settlement (order_counter tracks highest order number)
    order_counter = [max(int(o.order_id.split("-")[-1]) for o in orders) + 1]
    for _ in range(per_code):
        labels.append(inject_D10(ctx, order_counter))

    # D11 — unrelated bank credits
    for _ in range(per_code):
        labels.append(inject_D11(ctx))

    # Unresolvable cases (always exactly n_unresolvable)
    for i in range(n_unresolvable):
        labels.append(inject_unresolvable(ctx, i + 1))

    # --- guarantee all 15 codes present (exit gate requirement) ---------------
    _BATCH_FNS: dict[str, object] = {
        "D01": inject_D01, "D02": inject_D02, "D03": inject_D03,
        "D04": inject_D04, "D05": inject_D05, "D06": inject_D06,
        "D07": inject_D07, "D08": inject_D08, "D09": inject_D09,
        "D12": inject_D12, "D13": inject_D13, "D14": inject_D14,
        "D15": inject_D15,
    }
    injected_codes = {d["code"] for d in labels}
    for code in sorted(set(_BATCH_FNS) - injected_codes):
        shuffled = list(range(n_batches))
        rng.shuffle(shuffled)
        for bi in shuffled:
            result = _BATCH_FNS[code](ctx, bi)  # type: ignore[operator]
            if result:
                labels.append(result)
                break

    # D10 / D11 are always injected (they add new rows, never return None).

    # --- print injection counts (P1 exit gate requirement) -------------------
    from collections import Counter
    counts = Counter(d["code"] for d in labels)
    print("Injected discrepancies:")
    for code in sorted(counts):
        print(f"  {code}: {counts[code]}")

    return labels
