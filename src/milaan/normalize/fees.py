"""Gross -> fee -> GST -> net arithmetic.

This module is the single place where fee rounding happens. Every other tier
calls compute_net() rather than re-deriving the maths, so that the generator
and the solver cannot silently disagree about what a net amount should be.
"""

from __future__ import annotations

from dataclasses import dataclass

from .money import Paise

# GST on the platform fee, in basis points. 1800 bps = 18%.
GST_RATE_BPS = 1800

# Default per-method fee rates in basis points. Real values belong in
# config/fee_schedule.yaml; these are the fallback the generator uses.
DEFAULT_FEE_BPS = {
    "upi": 0,
    "card": 200,
    "netbanking": 190,
    "wallet": 200,
    "emi": 300,
    "international": 300,
}


@dataclass(frozen=True)
class FeeBreakdown:
    gross: Paise
    fee: Paise
    gst: Paise
    net: Paise

    def explains(self, observed_net: Paise, tolerance: Paise) -> bool:
        return self.net.within(observed_net, tolerance)


def compute_net(gross: Paise, method: str, fee_bps: dict[str, int] | None = None) -> FeeBreakdown:
    """Compute the settlement net for a single payment.

    Rounding is half-up at each step, applied independently to fee and GST.
    That per-step rounding is what creates the paise drift the solver has to
    tolerate -- it is deliberate, and it mirrors how PSP reports actually work.
    """
    rates = fee_bps if fee_bps is not None else DEFAULT_FEE_BPS
    if method not in rates:
        raise KeyError(f"No fee rate configured for method {method!r}")
    fee = gross.apply_rate_bps(rates[method])
    gst = fee.apply_rate_bps(GST_RATE_BPS)
    net = gross - fee - gst
    return FeeBreakdown(gross=gross, fee=fee, gst=gst, net=net)


def batch_total(
    payment_nets: list[Paise],
    refund_grosses: list[Paise] | None = None,
    chargeback_amounts: list[Paise] | None = None,
    chargeback_fees: list[Paise] | None = None,
) -> Paise:
    """The single bank credit a settlement batch should produce.

    Refunds and chargebacks are netted off inside the batch rather than
    arriving as separate debits -- this is the reason bank credits map
    many-to-one onto settlement rows.
    """
    total = Paise.sum(payment_nets)
    for group in (refund_grosses, chargeback_amounts, chargeback_fees):
        if group:
            total = total - Paise.sum(group)
    return total
