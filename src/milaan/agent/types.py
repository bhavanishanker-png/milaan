"""Shared types for the agent tier (T3).

A Cluster is one unit of work for the agent — a bundle of unresolved rows
from bank, settlement, and ledger that T1+T2 could not fully explain.

ReconState is the LangGraph state dict that flows through the graph nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict


@dataclass
class Cluster:
    """One reconciliation exception cluster handed to T3."""

    cluster_id: str
    exc_type: str               # e.g. "NEVER_SETTLED", "UNIDENTIFIED_CREDIT", "T2_RESIDUAL"
    bank_rows: list[dict]       # serialised CanonicalBankRow dicts, may be empty
    settlement_batches: list[dict]  # serialised SettlementBatch summaries, may be empty
    ledger_orders: list[dict]   # serialised CanonicalOrderRow dicts, may be empty
    fingerprint: str            # short string for precedent lookup, e.g. "NEVER_SETTLED:pay_000123"

    def summary(self) -> str:
        """One-line cluster description for the agent prompt."""
        bank_part = (
            f"bank_row={self.bank_rows[0]['row_index']} deposit={self.bank_rows[0]['deposit_paise']}p"
            if self.bank_rows else "no bank row"
        )
        setl_part = (
            f"setl={self.settlement_batches[0]['settlement_id']}"
            if self.settlement_batches else "no settlement batch"
        )
        ledger_part = (
            f"order={self.ledger_orders[0]['order_id']} payment_ref={self.ledger_orders[0]['payment_ref']}"
            if self.ledger_orders else "no ledger order"
        )
        return f"[{self.cluster_id}] {self.exc_type}: {bank_part} | {setl_part} | {ledger_part}"


@dataclass
class Hypothesis:
    """A candidate explanation the agent is testing."""

    text: str                   # human-readable hypothesis
    proposed_action: str        # "MATCH" | "FLAG" | "ESCALATE"
    proposed_reason_code: str   # D-code or custom reason
    arithmetic_claim: dict      # {bank_paise, payment_ids, refund_ids, chargeback_ids, ...}


@dataclass
class Resolution:
    """Final output of the agent for one cluster."""

    action: str                 # "MATCH" | "FLAG" | "ESCALATE"
    reason_code: str
    rationale: str
    confidence: float
    verified: bool              # True iff verify.py confirmed the arithmetic
    iterations: int
    tool_calls: list[dict]      # audit log of every tool call + result


class ReconState(TypedDict):
    """LangGraph state that flows through the agent graph."""

    cluster: Cluster
    hypotheses: list[Hypothesis]
    evidence: list[dict]        # each entry: {"tool": str, "input": dict, "result": Any}
    resolution: Resolution | None
    confidence: float
    iterations: int
    error: str | None           # set if any node raises; triggers FLAG escalation
