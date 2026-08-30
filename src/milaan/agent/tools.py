"""Agent tool definitions for T3.

Seven tools matching the P5 spec.  Each tool is a plain Python function
that takes a dict of arguments (matching what the LLM emits in tool_use)
and returns a JSON-serialisable dict.

The agent calls these; the verify node checks the arithmetic separately.
Tools never auto-apply anything — they only gather evidence.
"""

from __future__ import annotations

from milaan.normalize.fees import compute_net, DEFAULT_FEE_BPS
from milaan.normalize.money import Paise
from milaan.agent.verify import check


# ---------------------------------------------------------------------------
# Tool registry — the LLM sees these as JSON schemas.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "query_ledger",
        "description": "Look up one or more orders from the ledger by order_id or payment_ref.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_ids":    {"type": "array", "items": {"type": "string"}},
                "payment_refs": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "query_settlement",
        "description": "Look up settlement rows by entity_id or settlement_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_ids":     {"type": "array", "items": {"type": "string"}},
                "settlement_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "compute_expected_net",
        "description": (
            "Compute the expected net settlement for a payment: "
            "fee = gross × rate_bps / 10000 (half-up), gst = fee × 18%, net = gross − fee − gst. "
            "All amounts in integer paise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gross_paise": {"type": "integer"},
                "method":      {"type": "string"},
            },
            "required": ["gross_paise", "method"],
        },
    },
    {
        "name": "fetch_fee_schedule",
        "description": "Return the current fee schedule (basis points per payment method).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_precedents",
        "description": "Search past resolution precedents for similar exception clusters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Fingerprint or description of the cluster."},
                "k":     {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "propose_resolution",
        "description": (
            "Submit a resolution for the cluster.  Python will verify the arithmetic "
            "before accepting it.  If verification fails, you must revise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action":           {"type": "string", "enum": ["MATCH", "FLAG", "ESCALATE"]},
                "reason_code":      {"type": "string"},
                "rationale":        {"type": "string"},
                "payment_nets":     {"type": "array",  "items": {"type": "integer"}, "default": []},
                "refund_grosses":   {"type": "array",  "items": {"type": "integer"}, "default": []},
                "chargeback_amounts": {"type": "array","items": {"type": "integer"}, "default": []},
                "chargeback_fees":  {"type": "array",  "items": {"type": "integer"}, "default": []},
                "bank_deposit_paise": {"type": "integer", "default": 0},
            },
            "required": ["action", "reason_code", "rationale"],
        },
    },
    {
        "name": "flag_exception",
        "description": "Mark this cluster as requiring human review. Use when no confident resolution exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution functions
# ---------------------------------------------------------------------------

def _exec_query_ledger(args: dict, ledger_index: dict[str, dict]) -> dict:
    results = []
    for oid in args.get("order_ids", []):
        if oid in ledger_index:
            results.append(ledger_index[oid])
    for pref in args.get("payment_refs", []):
        for row in ledger_index.values():
            if row.get("payment_ref") == pref and row not in results:
                results.append(row)
    return {"orders": results, "count": len(results)}


def _exec_query_settlement(args: dict, settlement_index: dict[str, dict]) -> dict:
    results = []
    for eid in args.get("entity_ids", []):
        if eid in settlement_index:
            results.append(settlement_index[eid])
    for sid in args.get("settlement_ids", []):
        for row in settlement_index.values():
            if row.get("settlement_id") == sid and row not in results:
                results.append(row)
    return {"settlement_rows": results, "count": len(results)}


def _exec_compute_expected_net(args: dict) -> dict:
    gross_paise = int(args["gross_paise"])
    method = str(args["method"])
    if method not in DEFAULT_FEE_BPS:
        return {"error": f"Unknown method {method!r}. Known: {list(DEFAULT_FEE_BPS)}"}
    bd = compute_net(Paise(gross_paise), method)
    return {
        "gross_paise": bd.gross.value,
        "fee_paise":   bd.fee.value,
        "gst_paise":   bd.gst.value,
        "net_paise":   bd.net.value,
    }


def _exec_fetch_fee_schedule(_args: dict) -> dict:
    return {"fee_schedule_bps": DEFAULT_FEE_BPS, "gst_rate_bps": 1800}


def _exec_search_precedents(args: dict, store) -> dict:
    from milaan.agent.precedents import PrecedentStore
    k = int(args.get("k", 5))
    query = str(args["query"])
    records = store.search(query, k=k)
    return {
        "precedents": [
            {
                "exc_type":    r.exc_type,
                "action":      r.action,
                "reason_code": r.reason_code,
                "rationale":   r.rationale,
                "confidence":  r.confidence,
            }
            for r in records
        ],
        "count": len(records),
    }


def _exec_propose_resolution(args: dict) -> dict:
    payment_nets      = [int(x) for x in args.get("payment_nets", [])]
    refund_grosses    = [int(x) for x in args.get("refund_grosses", [])]
    chargeback_amounts= [int(x) for x in args.get("chargeback_amounts", [])]
    chargeback_fees   = [int(x) for x in args.get("chargeback_fees", [])]
    bank_deposit      = int(args.get("bank_deposit_paise", 0))

    if payment_nets and bank_deposit:
        passes, delta = check(
            bank_deposit_paise=bank_deposit,
            payment_nets=payment_nets,
            refund_grosses=refund_grosses or None,
            chargeback_amounts=chargeback_amounts or None,
            chargeback_fees=chargeback_fees or None,
        )
        return {
            "verified":     passes,
            "delta_paise":  delta,
            "action":       args["action"],
            "reason_code":  args["reason_code"],
            "rationale":    args["rationale"],
        }
    # No arithmetic to verify (e.g. NEVER_SETTLED — no bank row).
    return {
        "verified":    True,
        "delta_paise": 0,
        "action":      args["action"],
        "reason_code": args["reason_code"],
        "rationale":   args["rationale"],
    }


def _exec_flag_exception(args: dict) -> dict:
    return {"action": "FLAG", "reason": args["reason"], "verified": True}


def dispatch(
    tool_name: str,
    tool_input: dict,
    ledger_index: dict[str, dict],
    settlement_index: dict[str, dict],
    precedent_store,
) -> dict:
    """Route a tool call to its implementation."""
    if tool_name == "query_ledger":
        return _exec_query_ledger(tool_input, ledger_index)
    if tool_name == "query_settlement":
        return _exec_query_settlement(tool_input, settlement_index)
    if tool_name == "compute_expected_net":
        return _exec_compute_expected_net(tool_input)
    if tool_name == "fetch_fee_schedule":
        return _exec_fetch_fee_schedule(tool_input)
    if tool_name == "search_precedents":
        return _exec_search_precedents(tool_input, precedent_store)
    if tool_name == "propose_resolution":
        return _exec_propose_resolution(tool_input)
    if tool_name == "flag_exception":
        return _exec_flag_exception(tool_input)
    return {"error": f"Unknown tool: {tool_name}"}
