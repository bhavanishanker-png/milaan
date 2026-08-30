"""T3 LangGraph agent graph.

Topology (matches P5 spec):
    classify → hypothesise → gather_evidence → verify_arithmetic → propose | flag

Edge rules:
  - After verify_arithmetic: if verified → propose (terminal)
  - After verify_arithmetic: if not verified and iterations < max → hypothesise
  - After verify_arithmetic: if not verified and iterations ≥ max → flag (terminal)
  - Any node may set state["error"] to jump directly to flag

One cluster = one graph invocation.  The graph is stateless between clusters.

When ANTHROPIC_API_KEY is absent the graph short-circuits to
API_UNAVAILABLE escalation without calling the model.
"""

from __future__ import annotations

import json
import os
from typing import Literal

import anthropic
from langgraph.graph import END, StateGraph

from milaan.agent.types import Cluster, Resolution, ReconState
from milaan.agent.tools import TOOL_SCHEMAS, dispatch
from milaan.agent.precedents import PrecedentRecord, get_store


# ---------------------------------------------------------------------------
# Model configuration — read from env, never hardcoded.
# ---------------------------------------------------------------------------

_MODEL = os.environ.get("MILAAN_AGENT_MODEL", "claude-haiku-4-5-20251001")


def _client() -> anthropic.Anthropic | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are Milaan's reconciliation agent (T3).  You receive one exception cluster
that Tiers 1 and 2 (deterministic matchers) could not resolve.

Your job: determine what caused the discrepancy and propose a resolution.
Python will independently verify any arithmetic you propose — if it fails,
you must revise.  You cannot auto-apply chargebacks.

Tools available:
  query_ledger          — look up orders by order_id or payment_ref
  query_settlement      — look up settlement rows by entity_id or settlement_id
  compute_expected_net  — compute expected net for a payment (deterministic)
  fetch_fee_schedule    — get current fee rates
  search_precedents     — find similar past resolutions
  propose_resolution    — submit your resolution (arithmetic is verified)
  flag_exception        — escalate to human review

Work methodically:
  1. Search precedents for the cluster fingerprint.
  2. Query the ledger and settlement data for evidence.
  3. Form a hypothesis about the cause.
  4. Call propose_resolution with the arithmetic breakdown.
     If the verify step rejects it, revise and retry.
  5. If you cannot resolve with confidence, call flag_exception.

Be concise.  Do not explain reasoning steps unless asked.
"""


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def _node_classify(state: ReconState) -> ReconState:
    """Classify the cluster type; add a brief summary to evidence."""
    cluster: Cluster = state["cluster"]
    state["evidence"].append({
        "tool": "_classify",
        "input": {"cluster_id": cluster.cluster_id},
        "result": {
            "exc_type":   cluster.exc_type,
            "fingerprint": cluster.fingerprint,
            "summary":    cluster.summary(),
        },
    })
    return state


def _node_hypothesise(state: ReconState, ledger_idx: dict, settlement_idx: dict) -> ReconState:
    """Call Claude to gather evidence and form a hypothesis via tool use."""
    client = _client()
    if client is None:
        state["error"] = "API_UNAVAILABLE"
        return state

    cluster: Cluster = state["cluster"]
    precedents = get_store().search(cluster.fingerprint, k=5)
    precedent_context = get_store().format_context(precedents)

    user_message = (
        f"Cluster: {cluster.summary()}\n\n"
        f"Exception type: {cluster.exc_type}\n"
        f"Bank rows: {json.dumps(cluster.bank_rows[:2], indent=2)}\n"
        f"Settlement batches: {json.dumps(cluster.settlement_batches[:1], indent=2)}\n"
        f"Ledger orders: {json.dumps(cluster.ledger_orders[:2], indent=2)}\n\n"
        f"{precedent_context}\n\n"
        "Begin investigation. Start with search_precedents, then gather evidence, "
        "then propose_resolution or flag_exception."
    )

    messages = [{"role": "user", "content": user_message}]

    policy = _load_policy()
    max_iter = policy.get("agent", {}).get("max_iterations", 6)
    iterations = state["iterations"]

    # Agentic loop — tool_use until the model stops or we hit the cap.
    while iterations < max_iter:
        iterations += 1
        try:
            response = client.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=_SYSTEM,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
        except Exception as exc:
            state["error"] = f"API_ERROR: {exc}"
            break

        # Accumulate assistant turn.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            break

        # Execute tool calls.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = dispatch(
                tool_name=block.name,
                tool_input=block.input,
                ledger_index=ledger_idx,
                settlement_index=settlement_idx,
                precedent_store=get_store(),
            )
            state["evidence"].append({
                "tool": block.name,
                "input": block.input,
                "result": result,
            })
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result),
            })

            # If the model called propose_resolution or flag_exception, parse result.
            if block.name in ("propose_resolution", "flag_exception"):
                action = result.get("action", "FLAG")
                verified = result.get("verified", True)
                reason_code = result.get("reason_code", block.input.get("reason_code", "UNKNOWN"))
                rationale = result.get("rationale", block.input.get("reason", ""))
                confidence_raw = block.input.get("confidence", None)
                confidence = (
                    float(confidence_raw) if confidence_raw is not None
                    else (policy.get("gate", {}).get("min_confidence", 0.85) if verified else 0.5)
                )
                state["resolution"] = Resolution(
                    action=action,
                    reason_code=reason_code,
                    rationale=rationale,
                    confidence=confidence,
                    verified=verified,
                    iterations=iterations,
                    tool_calls=list(state["evidence"]),
                )
                state["iterations"] = iterations
                messages.append({"role": "user", "content": tool_results})
                break

        else:
            messages.append({"role": "user", "content": tool_results})
            continue
        break

    state["iterations"] = iterations
    return state


def _node_verify(state: ReconState) -> ReconState:
    """Arithmetic is already verified inside propose_resolution tool.  This node
    just routes based on the result already stored in state['resolution']."""
    return state


def _route_after_verify(state: ReconState) -> Literal["propose", "hypothesise", "flag"]:
    if state.get("error"):
        return "flag"
    max_iter = _load_policy().get("agent", {}).get("max_iterations", 6)
    res = state.get("resolution")
    if res is None:
        if state["iterations"] < max_iter:
            return "hypothesise"
        return "flag"
    if not res.verified:
        if state["iterations"] < max_iter:
            return "hypothesise"
        return "flag"
    return "propose"


def _node_propose(state: ReconState) -> ReconState:
    """Accept the verified resolution and record it as a precedent."""
    res: Resolution = state["resolution"]
    cluster: Cluster = state["cluster"]
    get_store().add(PrecedentRecord(
        fingerprint=cluster.fingerprint,
        exc_type=cluster.exc_type,
        action=res.action,
        reason_code=res.reason_code,
        rationale=res.rationale,
        confidence=res.confidence,
    ))
    return state


def _node_flag(state: ReconState) -> ReconState:
    """Fallback: escalate to human.  Build a minimal resolution record."""
    if state.get("resolution") is None:
        error = state.get("error", "max_iterations_reached")
        state["resolution"] = Resolution(
            action="ESCALATE",
            reason_code="AGENT_UNRESOLVED",
            rationale=f"Agent could not resolve: {error}",
            confidence=0,
            verified=False,
            iterations=state["iterations"],
            tool_calls=list(state["evidence"]),
        )
    return state


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _load_policy() -> dict:
    from pathlib import Path
    import yaml
    p = Path(__file__).parents[4] / "config" / "policy.yaml"
    with p.open() as fh:
        return yaml.safe_load(fh)


def build_graph(ledger_idx: dict, settlement_idx: dict) -> "CompiledGraph":
    """Build and compile the LangGraph workflow.

    ledger_idx      : {order_id: order_dict} for query_ledger tool
    settlement_idx  : {entity_id: setl_row_dict} for query_settlement tool
    """
    from functools import partial

    g = StateGraph(ReconState)

    g.add_node("classify",    _node_classify)
    g.add_node("hypothesise", partial(_node_hypothesise, ledger_idx=ledger_idx, settlement_idx=settlement_idx))
    g.add_node("verify",      _node_verify)
    g.add_node("propose",     _node_propose)
    g.add_node("flag",        _node_flag)

    g.set_entry_point("classify")
    g.add_edge("classify",    "hypothesise")
    g.add_edge("hypothesise", "verify")
    g.add_conditional_edges("verify", _route_after_verify, {
        "propose":     "propose",
        "hypothesise": "hypothesise",
        "flag":        "flag",
    })
    g.add_edge("propose", END)
    g.add_edge("flag",    END)

    return g.compile()


def run_cluster(
    cluster: Cluster,
    ledger_idx: dict,
    settlement_idx: dict,
) -> Resolution:
    """Run one cluster through the graph and return its Resolution."""
    graph = build_graph(ledger_idx, settlement_idx)
    initial_state: ReconState = {
        "cluster":    cluster,
        "hypotheses": [],
        "evidence":   [],
        "resolution": None,
        "confidence": 0,
        "iterations": 0,
        "error":      None,
    }
    final = graph.invoke(initial_state)
    res: Resolution | None = final.get("resolution")
    if res is None:
        res = Resolution(
            action="ESCALATE",
            reason_code="GRAPH_NO_RESOLUTION",
            rationale="Graph completed without producing a resolution.",
            confidence=0,
            verified=False,
            iterations=final.get("iterations", 0),
            tool_calls=final.get("evidence", []),
        )
    return res
