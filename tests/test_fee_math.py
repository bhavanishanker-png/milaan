"""Money type and fee arithmetic.

The drift tests are the point of this file: they encode the reason the whole
project uses integer paise, so that a future refactor cannot quietly undo it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from milaan.normalize.fees import GST_RATE_BPS, batch_total, compute_net
from milaan.normalize.money import MoneyError, Paise, rounding_tolerance


class TestConstruction:
    def test_from_int_rupees(self):
        assert Paise.from_rupees(1234).value == 123400

    def test_from_string_with_commas(self):
        assert Paise.from_rupees("4,82,331.44").value == 48233144

    def test_from_decimal(self):
        assert Paise.from_rupees(Decimal("99.99")).value == 9999

    def test_rejects_float(self):
        with pytest.raises(MoneyError):
            Paise.from_rupees(1234.56)

    def test_rejects_float_constructor(self):
        with pytest.raises(MoneyError):
            Paise(100.0)

    def test_rejects_bool(self):
        with pytest.raises(MoneyError):
            Paise(True)

    def test_rejects_sub_paise(self):
        with pytest.raises(MoneyError):
            Paise.from_rupees(Decimal("1.234"))


class TestArithmetic:
    def test_add_and_subtract(self):
        assert (Paise(500) + Paise(250)).value == 750
        assert (Paise(500) - Paise(750)).value == -250

    def test_division_is_disallowed(self):
        with pytest.raises(MoneyError):
            Paise(500) / 2

    def test_split_evenly_conserves_paise(self):
        parts = Paise(1000).split_evenly(3)
        assert [p.value for p in parts] == [334, 333, 333]
        assert Paise.sum(parts).value == 1000

    def test_multiply_rejects_float(self):
        with pytest.raises(MoneyError):
            Paise(100) * 1.5


class TestRateApplication:
    def test_two_percent(self):
        assert Paise.from_rupees(1000).apply_rate_bps(200).value == 2000

    def test_half_up_rounding(self):
        # 2% of 1.25 = 2.5 paise, rounds up to 3.
        assert Paise(125).apply_rate_bps(200).value == 3

    def test_negative_rounds_symmetrically(self):
        assert Paise(-125).apply_rate_bps(200).value == -3

    def test_zero_rate_for_upi(self):
        assert Paise.from_rupees(5000).apply_rate_bps(0).value == 0


class TestFeeBreakdown:
    def test_card_payment_net(self):
        b = compute_net(Paise.from_rupees(1000), "card")
        assert b.fee.value == 2000          # 2% of 1000.00
        assert b.gst.value == 360           # 18% of 20.00
        assert b.net.value == 100000 - 2000 - 360

    def test_net_plus_deductions_reconstructs_gross(self):
        b = compute_net(Paise.from_rupees("4,999.99"), "netbanking")
        assert (b.net + b.fee + b.gst) == b.gross

    def test_gst_rate_is_eighteen_percent(self):
        assert GST_RATE_BPS == 1800

    def test_unknown_method_raises(self):
        with pytest.raises(KeyError):
            compute_net(Paise.from_rupees(100), "barter")


class TestBatchDrift:
    """The reason this project is integer-only.

    Per-row rounding of fee and GST means a batch total is not simply a
    percentage of the batch gross. The solver must tolerate the accumulated
    drift, and the tolerance must be tight enough not to admit false matches.
    """

    def test_batch_total_deducts_refunds_and_chargebacks(self):
        nets = [compute_net(Paise.from_rupees(1000), "card").net for _ in range(3)]
        total = batch_total(
            payment_nets=nets,
            refund_grosses=[Paise.from_rupees(500)],
            chargeback_amounts=[Paise.from_rupees(200)],
            chargeback_fees=[Paise.from_rupees(75)],
        )
        expected = Paise.sum(nets) - Paise.from_rupees(500) - Paise.from_rupees(200) - Paise.from_rupees(75)
        assert total == expected

    def test_drift_stays_inside_tolerance(self):
        amounts = [Paise(v) for v in range(100_01, 100_01 + 400)]
        nets = [compute_net(a, "card").net for a in amounts]
        observed = Paise.sum(nets)

        gross = Paise.sum(amounts)
        naive_fee = gross.apply_rate_bps(200)
        naive = gross - naive_fee - naive_fee.apply_rate_bps(GST_RATE_BPS)

        assert observed.within(naive, rounding_tolerance(len(amounts)))

    def test_tolerance_scales_with_row_count(self):
        assert rounding_tolerance(0).value == 0
        assert rounding_tolerance(400).value == 1200

    def test_tolerance_rejects_bad_input(self):
        with pytest.raises(MoneyError):
            rounding_tolerance(-1)
