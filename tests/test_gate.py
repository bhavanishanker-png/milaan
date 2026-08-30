"""Gate tests — P6.

The gate must block chargebacks at ANY confidence level.
This test is a hard contract: never weaken it.
"""

from __future__ import annotations

import pytest

from milaan.policy.gate import evaluate, GateDecision

# These codes must NEVER be auto-applied. Load from policy.yaml in the fixture
# so test and config stay in sync rather than duplicating the list.
import yaml
from pathlib import Path

_POLICY = yaml.safe_load(
    (Path(__file__).parents[1] / "config" / "policy.yaml").read_text()
)
_GATE_CFG = _POLICY.get("gate", {})
_NEVER_AUTO = _GATE_CFG.get("never_auto", [])
_MIN_CONF   = _GATE_CFG.get("min_confidence", 0.85)
_MAX_AMOUNT = _GATE_CFG.get("max_auto_apply_paise", 5_000_000)


@pytest.mark.parametrize("reason_code", _NEVER_AUTO)
def test_never_auto_blocked_at_any_confidence(reason_code: str):
    """Classes in gate.never_auto are ESCALATED regardless of confidence."""
    result = evaluate(
        confidence=1.0,        # maximum possible confidence
        amount_paise=1,        # minimum possible amount
        reason_code=reason_code,
        gate_config=_GATE_CFG,
    )
    assert result.decision == GateDecision.ESCALATE, (
        f"{reason_code} should always escalate but gate returned AUTO_APPLY"
    )


def test_chargeback_blocked_explicitly():
    """D04_CHARGEBACK_DEBIT is in never_auto — prove it directly."""
    assert "D04_CHARGEBACK_DEBIT" in _NEVER_AUTO, (
        "D04_CHARGEBACK_DEBIT must be in gate.never_auto in config/policy.yaml"
    )
    result = evaluate(
        confidence=1.0,
        amount_paise=100,
        reason_code="D04_CHARGEBACK_DEBIT",
        gate_config=_GATE_CFG,
    )
    assert result.decision == GateDecision.ESCALATE


def test_low_confidence_blocked():
    result = evaluate(
        confidence=_MIN_CONF - 0.01,
        amount_paise=100,
        reason_code="D01_TIMING_LAG",
        gate_config=_GATE_CFG,
    )
    assert result.decision == GateDecision.ESCALATE


def test_high_amount_blocked():
    result = evaluate(
        confidence=1.0,
        amount_paise=_MAX_AMOUNT + 1,
        reason_code="D01_TIMING_LAG",
        gate_config=_GATE_CFG,
    )
    assert result.decision == GateDecision.ESCALATE


def test_safe_class_auto_applied():
    safe = _GATE_CFG.get("auto_safe_classes", ["D01_TIMING_LAG"])[0]
    result = evaluate(
        confidence=_MIN_CONF,
        amount_paise=100,
        reason_code=safe,
        gate_config=_GATE_CFG,
    )
    assert result.decision == GateDecision.AUTO_APPLY


def test_gate_result_has_reason():
    """Every gate result carries a human-readable reason string."""
    result = evaluate(
        confidence=0.0,
        amount_paise=0,
        reason_code="D04_CHARGEBACK_DEBIT",
        gate_config=_GATE_CFG,
    )
    assert result.reason, "GateResult.reason must not be empty"
