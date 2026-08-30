"""Policy gate (T4).

Three independent conditions must ALL pass for auto-apply:
  1. confidence  ≥ gate.min_confidence
  2. amount      ≤ gate.max_auto_apply_paise
  3. class       not in gate.never_auto

Any failure → ESCALATE.  Chargebacks are structurally blocked via never_auto;
there is a test in tests/test_gate.py proving this property holds regardless
of what confidence the agent emits.

Thresholds are read from config/policy.yaml.  Nothing is hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

_POLICY_PATH = Path(__file__).parents[4] / "config" / "policy.yaml"


class GateDecision(str, Enum):
    AUTO_APPLY = "AUTO_APPLY"
    ESCALATE   = "ESCALATE"


@dataclass(frozen=True)
class GateResult:
    decision:    GateDecision
    reason:      str            # human-readable explanation
    confidence:  float
    amount_paise: int
    reason_code: str


def _load_gate_config() -> dict:
    with _POLICY_PATH.open() as fh:
        return yaml.safe_load(fh).get("gate", {})


def evaluate(
    confidence: float,
    amount_paise: int,
    reason_code: str,
    *,
    gate_config: dict | None = None,
) -> GateResult:
    """Decide whether a proposed match may be auto-applied.

    Parameters
    ----------
    confidence    : score emitted by the tier that produced this match
    amount_paise  : the bank deposit amount (used for the size gate)
    reason_code   : D-code or custom reason string
    gate_config   : optional pre-loaded config dict (omit to read from disk)
    """
    cfg = gate_config if gate_config is not None else _load_gate_config()

    min_conf         = cfg.get("min_confidence", 0)
    max_amount       = cfg.get("max_auto_apply_paise", 0)
    never_auto       = set(cfg.get("never_auto", []))

    # Gate 1: class block (hardest rule — tested independently)
    if reason_code in never_auto:
        return GateResult(
            decision=GateDecision.ESCALATE,
            reason=f"reason_code {reason_code!r} is in gate.never_auto",
            confidence=confidence,
            amount_paise=amount_paise,
            reason_code=reason_code,
        )

    # Gate 2: confidence floor
    if confidence < min_conf:
        return GateResult(
            decision=GateDecision.ESCALATE,
            reason=f"confidence {confidence:.3f} < gate.min_confidence {min_conf}",
            confidence=confidence,
            amount_paise=amount_paise,
            reason_code=reason_code,
        )

    # Gate 3: amount ceiling
    if amount_paise > max_amount:
        return GateResult(
            decision=GateDecision.ESCALATE,
            reason=(
                f"amount {amount_paise} paise > gate.max_auto_apply_paise {max_amount}"
            ),
            confidence=confidence,
            amount_paise=amount_paise,
            reason_code=reason_code,
        )

    return GateResult(
        decision=GateDecision.AUTO_APPLY,
        reason="all gate conditions satisfied",
        confidence=confidence,
        amount_paise=amount_paise,
        reason_code=reason_code,
    )
