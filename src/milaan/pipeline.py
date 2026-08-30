"""Full pipeline entry point.

Usage (via Makefile):
    python3 -m milaan.pipeline --data data/a --out out/a

Writes out/<batch>/results.json — the single artifact that evaluate.py reads.

Tier progression:
    T1 — exact UTR + exact amount            (deterministic)
    T2 — fuzzy UTR, tolerance, date window   (deterministic)
    T3 — LangGraph agent                     (LLM, residue only)
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import yaml

from milaan.normalize.adapters import bank_hdfc, psp_razorpay, ledger
from milaan.normalize.canonical import IST
from milaan.match import deterministic, solver
from milaan.policy import gate as gate_mod
from milaan.policy.audit import AuditLog
from milaan.journal import propose as journal_propose

_POLICY_PATH = Path(__file__).parents[2] / "config" / "policy.yaml"


def _load_policy() -> dict:
    with _POLICY_PATH.open() as fh:
        return yaml.safe_load(fh)


def _build_indices(order_rows, setl_batches) -> tuple[dict, dict]:
    """Build fast-lookup dicts for the agent tools."""
    ledger_idx: dict[str, dict] = {}
    for row in order_rows:
        d = {
            "order_id":    row.order_id,
            "payment_ref": row.payment_ref,
            "gross_paise": row.gross_paise,
            "method":      row.channel,
            "status":      row.status,
            "order_date":  row.order_date.isoformat(),
        }
        ledger_idx[row.order_id] = d

    settlement_idx: dict[str, dict] = {}
    for batch in setl_batches:
        for row in batch.rows:
            d = {
                "entity_id":     row.entity_id,
                "entity_type":   row.entity_type,
                "settlement_id": row.settlement_id,
                "settlement_utr": row.settlement_utr,
                "net_paise":     row.net_paise,
                "amount_paise":  row.amount_paise,
                "settled_at":    row.settled_at.isoformat(),
            }
            settlement_idx[row.entity_id] = d

    return ledger_idx, settlement_idx


def _build_t3_clusters(t2, bank_rows, setl_batches, order_rows):
    """Build Cluster objects for T2 residual exceptions."""
    from milaan.agent.types import Cluster

    bank_by_idx: dict[int, dict] = {}
    for br in bank_rows:
        bank_by_idx[br.row_index] = {
            "row_index":     br.row_index,
            "narration":     br.narration,
            "deposit_paise": br.deposit_paise.value,
            "value_date":    br.value_date.isoformat(),
            "utr":           br.utr,
            "utr_confidence": br.utr_confidence,
        }

    setl_by_id: dict[str, dict] = {}
    for b in setl_batches:
        setl_by_id[b.settlement_id] = {
            "settlement_id":     b.settlement_id,
            "settlement_utr":    b.settlement_utr,
            "n_rows":            len(b.rows),
            "batch_total_paise": sum(r.net_paise for r in b.rows),
        }

    clusters = []
    for exc in t2.exceptions:
        bank_dicts = []
        if exc.bank_row_index is not None and exc.bank_row_index in bank_by_idx:
            bank_dicts = [bank_by_idx[exc.bank_row_index]]

        setl_dicts = []
        if exc.settlement_id and exc.settlement_id in setl_by_id:
            setl_dicts = [setl_by_id[exc.settlement_id]]

        fingerprint = f"{exc.exc_type}:{exc.settlement_id or exc.bank_row_index}"

        clusters.append(Cluster(
            cluster_id=fingerprint,
            exc_type=exc.exc_type,
            bank_rows=bank_dicts,
            settlement_batches=setl_dicts,
            ledger_orders=[],
            fingerprint=fingerprint,
        ))

    return clusters


def _run(data_dir: Path, out_dir: Path, run_t3: bool = True) -> dict:
    """Load three sources, run T1→T2→T3, return serialisable results dict."""
    bank_rows  = bank_hdfc.load(data_dir / "bank.csv")
    setl_rows  = psp_razorpay.load_batches(data_dir / "settlement.csv")
    order_rows = ledger.load(data_dir / "ledger.csv")

    policy = _load_policy()

    _t0 = time.perf_counter()

    # T1 — exact UTR + exact amount
    t1 = deterministic.run(bank_rows, setl_rows)
    _t1_ms = int((time.perf_counter() - _t0) * 1000)

    _ts2 = time.perf_counter()
    # T2 — constrained solver on T1 residual
    t2 = solver.run(bank_rows, setl_rows, t1, policy=policy)
    _t2_ms = int((time.perf_counter() - _ts2) * 1000)

    # T3 — LangGraph agent on T2 residual
    t3_matches: list[dict] = []
    t3_exceptions: list[dict] = []
    _ts3 = time.perf_counter()
    _t3_ms = 0

    if run_t3 and t2.exceptions:
        try:
            from milaan.agent.graph import run_cluster
            from milaan.agent.precedents import reset_store
            reset_store()

            ledger_idx, settlement_idx = _build_indices(order_rows, setl_rows)
            clusters = _build_t3_clusters(t2, bank_rows, setl_rows, order_rows)

            for cluster in clusters:
                resolution = run_cluster(cluster, ledger_idx, settlement_idx)
                entry = {
                    "exc_type":       cluster.exc_type,
                    "bank_row":       cluster.bank_rows[0]["row_index"] if cluster.bank_rows else None,
                    "settlement_id":  cluster.settlement_batches[0]["settlement_id"] if cluster.settlement_batches else None,
                    "action":         resolution.action,
                    "reason_code":    resolution.reason_code,
                    "rationale":      resolution.rationale,
                    "confidence":     resolution.confidence,
                    "verified":       resolution.verified,
                    "iterations":     resolution.iterations,
                    "tier_reached":   "T3",
                }
                gate = policy.get("gate", {})
                min_conf = gate.get("min_confidence", 0)
                never_auto = gate.get("never_auto", [])

                if (
                    resolution.action == "MATCH"
                    and resolution.verified
                    and resolution.confidence >= min_conf
                    and resolution.reason_code not in never_auto
                ):
                    bank_row = cluster.bank_rows[0]["row_index"] if cluster.bank_rows else None
                    settlement_id = cluster.settlement_batches[0]["settlement_id"] if cluster.settlement_batches else None
                    t3_matches.append({
                        "bank_row":       bank_row,
                        "settlement_id":  settlement_id,
                        "settlement_utr": cluster.settlement_batches[0].get("settlement_utr") if cluster.settlement_batches else None,
                        "deposit_paise":  cluster.bank_rows[0]["deposit_paise"] if cluster.bank_rows else None,
                        "tier":           "T3",
                        "reason_code":    resolution.reason_code,
                        "confidence":     resolution.confidence,
                    })
                else:
                    t3_exceptions.append(entry)

        except Exception as exc:
            t3_exceptions.append({"exc_type": "T3_INIT_ERROR", "detail": str(exc), "tier_reached": "T3"})
    _t3_ms = int((time.perf_counter() - _ts3) * 1000)

    now = datetime.now(tz=IST).isoformat()

    # T4 — Policy gate + audit log + journal entries
    audit     = AuditLog(out_dir / "audit.ndjson")
    gate_cfg  = policy.get("gate", {})
    journal_lines: list[dict] = []

    all_matches: list[dict] = []
    all_exceptions: list[dict] = []

    # --- T1 matches (confidence = 1.0, reason = EXACT_MATCH) ---
    for m in t1.matches:
        gr = gate_mod.evaluate(
            confidence=1,
            amount_paise=m.bank_deposit_paise,
            reason_code="EXACT_MATCH",
            gate_config=gate_cfg,
        )
        audit.record(
            actor="tier1", event=gr.decision.value,
            bank_row=m.bank_row_index, settlement_id=m.settlement_id,
            tier="T1", confidence=1,
            reason_code="EXACT_MATCH",
            rationale=gr.reason,
        )
        if gr.decision == gate_mod.GateDecision.AUTO_APPLY:
            all_matches.append({
                "bank_row":       m.bank_row_index,
                "settlement_id":  m.settlement_id,
                "settlement_utr": m.settlement_utr,
                "deposit_paise":  m.bank_deposit_paise,
                "tier":           "T1",
                "confidence":     1,
            })
            je = journal_propose(
                settlement_id=m.settlement_id,
                bank_row_index=m.bank_row_index,
                bank_deposit_paise=m.bank_deposit_paise,
                settlement_net_paise=m.batch_total_paise,
                tier="T1", reason_code="EXACT_MATCH",
            )
            journal_lines.append({
                "settlement_id": je.settlement_id,
                "bank_row": je.bank_row,
                "tier": je.tier,
                "reason_code": je.reason_code,
                "balanced": je.is_balanced(),
                "lines": [
                    {"account": l.account, "debit": l.debit_paise, "credit": l.credit_paise}
                    for l in je.lines
                ],
            })
        else:
            all_exceptions.append({
                "exc_type": "GATE_BLOCK",
                "bank_row": m.bank_row_index,
                "settlement_id": m.settlement_id,
                "detail": gr.reason,
                "tier_reached": "T4",
            })

    # --- T2 matches ---
    for m in t2.matches:
        gr = gate_mod.evaluate(
            confidence=m.confidence,
            amount_paise=m.bank_deposit_paise,
            reason_code=m.resolution_method,
            gate_config=gate_cfg,
        )
        audit.record(
            actor="tier2", event=gr.decision.value,
            bank_row=m.bank_row_index, settlement_id=m.settlement_id,
            tier="T2", confidence=m.confidence,
            reason_code=m.resolution_method,
            rationale=gr.reason,
        )
        if gr.decision == gate_mod.GateDecision.AUTO_APPLY:
            all_matches.append({
                "bank_row":        m.bank_row_index,
                "settlement_id":   m.settlement_id,
                "settlement_utr":  m.settlement_utr,
                "deposit_paise":   m.bank_deposit_paise,
                "tier":            "T2",
                "confidence":      m.confidence,
                "resolution_method": m.resolution_method,
            })
            je = journal_propose(
                settlement_id=m.settlement_id,
                bank_row_index=m.bank_row_index,
                bank_deposit_paise=m.bank_deposit_paise,
                settlement_net_paise=m.batch_total_paise,
                tier="T2", reason_code=m.resolution_method,
            )
            journal_lines.append({
                "settlement_id": je.settlement_id,
                "bank_row": je.bank_row,
                "tier": je.tier,
                "reason_code": je.reason_code,
                "balanced": je.is_balanced(),
                "lines": [
                    {"account": l.account, "debit": l.debit_paise, "credit": l.credit_paise}
                    for l in je.lines
                ],
            })
        else:
            all_exceptions.append({
                "exc_type": "GATE_BLOCK",
                "bank_row": m.bank_row_index,
                "settlement_id": m.settlement_id,
                "settlement_utr": m.settlement_utr,
                "detail": gr.reason,
                "tier_reached": "T4",
            })

    # --- T3 matches (already gate-filtered in T3 block above) ---
    for m in t3_matches:
        audit.record(
            actor="tier3", event="AUTO_APPLY",
            bank_row=m.get("bank_row"), settlement_id=m.get("settlement_id"),
            tier="T3", confidence=m.get("confidence"),
            reason_code=m.get("reason_code"),
            rationale="T3 gate passed inside T3 block",
        )
        all_matches.append(m)

    # --- T2 residual exceptions ---
    t2_exception_ids = {(e.bank_row_index, e.settlement_id) for e in t2.exceptions}
    t3_exc_ids       = {(e.get("bank_row"), e.get("settlement_id")) for e in t3_exceptions}

    for e in t2.exceptions:
        key = (e.bank_row_index, e.settlement_id)
        tier = "T3" if (key in t3_exc_ids) else "T2"
        audit.record(
            actor=f"tier{tier[-1].lower()}" if tier else "tier2",
            event="EXCEPTION",
            bank_row=e.bank_row_index, settlement_id=e.settlement_id,
            tier=tier, reason_code=e.exc_type, rationale=str(e.detail),
        )
        all_exceptions.append({
            "exc_type":       e.exc_type,
            "bank_row":       e.bank_row_index,
            "settlement_id":  e.settlement_id,
            "settlement_utr": e.settlement_utr,
            "detail":         e.detail,
            "tier_reached":   tier,
        })

    for e in t3_exceptions:
        if (e.get("bank_row"), e.get("settlement_id")) not in t2_exception_ids:
            all_exceptions.append(e)

    # Write journal
    journal_path = out_dir / "journal.ndjson"
    with journal_path.open("w", encoding="utf-8") as fh:
        for je in journal_lines:
            fh.write(json.dumps(je) + "\n")

    n_settlement_credits = sum(1 for r in bank_rows if r.is_settlement_credit)
    total_matched = len(all_matches)

    tiers = ["T1", "T2"]
    if run_t3:
        tiers.append("T3")
    tiers.append("T4")

    _total_ms = int((time.perf_counter() - _t0) * 1000)
    n_records = len(order_rows)

    return {
        "run_at":                   now,
        "pipeline_tiers":           tiers,
        "data_dir":                 str(data_dir),
        "total_bank_rows":          len(bank_rows),
        "total_order_rows":         n_records,
        "total_settlement_rows":    sum(len(b.rows) for b in setl_rows),
        "total_settlement_batches": len(setl_rows),
        "settlement_credits_in_bank": n_settlement_credits,
        "t1_matched":               t1.n_matched,
        "t2_matched":               t2.n_matched,
        "t3_matched":               len(t3_matches),
        "total_matched":            total_matched,
        "t2_exceptions":            t2.n_exceptions,
        "t2_timeouts":              len(t2.timeouts),
        "t3_exceptions":            len(t3_exceptions),
        "timing_ms": {
            "t1": _t1_ms,
            "t2": _t2_ms,
            "t3": _t3_ms,
            "total": _total_ms,
        },
        "throughput_records_per_min": (
            int(n_records / (_total_ms / 60000)) if _total_ms > 0 else 0
        ),
        "matches":                  all_matches,
        "exceptions":               all_exceptions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Milaan reconciliation pipeline.")
    parser.add_argument("--data", required=True, help="Directory with bank.csv, settlement.csv, ledger.csv")
    parser.add_argument("--out",  required=True, help="Output directory for results.json")
    args = parser.parse_args()

    data_dir = Path(args.data)
    out_dir  = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = _run(data_dir, out_dir)

    out_path = out_dir / "results.json"
    out_path.write_text(json.dumps(results, indent=2))

    n_batches = results["total_settlement_batches"]
    t1m = results["t1_matched"]
    t2m = results["t2_matched"]
    t3m = results.get("t3_matched", 0)
    print(f"Pipeline complete → {out_path}")
    print(f"  T1 matched  : {t1m}/{n_batches}")
    print(f"  T2 matched  : {t2m}/{n_batches - t1m}")
    print(f"  T3 matched  : {t3m}/{results.get('t2_exceptions', 0)}")
    print(f"  Total       : {results['total_matched']}/{n_batches}")
    print(f"  Exceptions  : {results.get('t3_exceptions', results.get('t2_exceptions', 0))}")
    if results["t2_timeouts"]:
        print(f"  TIMEOUTS    : {results['t2_timeouts']} (logged in exceptions)")


if __name__ == "__main__":
    main()
