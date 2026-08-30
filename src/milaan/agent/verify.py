"""Deterministic arithmetic verifier — never the model.

The agent proposes a hypothesis of the form:
  "bank_credit = Σpayment_nets − Σrefund_grosses − Σchargeback_amounts − Σchargeback_fees"

This module checks whether that claim is arithmetically consistent with the
data loaded from settlement.csv, within the per-batch rounding tolerance.

Returning False loops the agent back to hypothesise a new claim.
Returning True allows the agent to propose the resolution.
"""

from __future__ import annotations

from milaan.normalize.money import rounding_tolerance, Paise


def check(
    bank_deposit_paise: int,
    payment_nets: list[int],
    refund_grosses: list[int] | None = None,
    chargeback_amounts: list[int] | None = None,
    chargeback_fees: list[int] | None = None,
) -> tuple[bool, int]:
    """Verify that the claimed components explain the bank deposit.

    Parameters
    ----------
    bank_deposit_paise   : the actual bank credit amount
    payment_nets         : net paise for each payment in the proposed set
    refund_grosses       : gross refund amounts to subtract (optional)
    chargeback_amounts   : chargeback principal amounts (optional)
    chargeback_fees      : PSP chargeback processing fees (optional)

    Returns
    -------
    (passes, delta_paise)
    passes      : True if |computed − bank_deposit| ≤ rounding_tolerance(n_payments)
    delta_paise : computed − bank_deposit (positive means computed is higher)
    """
    total = sum(payment_nets)
    for group in (refund_grosses, chargeback_amounts, chargeback_fees):
        if group:
            total -= sum(group)

    n_payments = len(payment_nets)
    tolerance = rounding_tolerance(n_payments).value
    delta = total - bank_deposit_paise

    return abs(delta) <= tolerance, delta


def check_no_bank_row(
    settlement_total_paise: int,
    bank_deposit_paise: int | None,
    n_payment_rows: int,
) -> tuple[bool, int]:
    """Simpler check for clusters where the bank row is known but amounts drift.

    Used by AMOUNT_TOLERANCE escalations that T2 decided not to accept.
    """
    if bank_deposit_paise is None:
        return False, 0
    tolerance = rounding_tolerance(n_payment_rows).value
    delta = settlement_total_paise - bank_deposit_paise
    return abs(delta) <= tolerance, delta
