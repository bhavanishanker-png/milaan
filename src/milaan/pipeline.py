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
from datetime import datetime
from pathlib import Path

import yaml

from milaan.normalize.adapters import bank_hdfc, psp_razorpay, ledger
from milaan.normalize.canonical import IST
from milaan.match import deterministic, solver

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

    # T1 — exact UTR + exact amount
    t1 = deterministic.run(bank_rows, setl_rows)

    # T2 — constrained solver on T1 residual
    t2 = solver.run(bank_rows, setl_rows, t1, policy=policy)

    # T3 — LangGraph agent on T2 residual
    t3_matches: list[dict] = []
    t3_exceptions: list[dict] = []

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

    now = datetime.now(tz=IST).isoformat()

    def _match_dict(m, tier: str) -> dict:
        return {
            "bank_row":       m.bank_row_index,
            "settlement_id":  m.settlement_id,
            "settlement_utr": m.settlement_utr,
            "deposit_paise":  m.bank_deposit_paise,
            "tier":           tier,
        }

    all_matches = (
        [_match_dict(m, "T1") for m in t1.matches]
        + [_match_dict(m, "T2") for m in t2.matches]
        + t3_matches
    )

    # T2 residual exceptions not escalated by T3
    t2_exception_ids = {(e.bank_row_index, e.settlement_id) for e in t2.exceptions}
    t3_exc_ids       = {(e.get("bank_row"), e.get("settlement_id")) for e in t3_exceptions}

    all_exceptions = []
    for e in t2.exceptions:
        key = (e.bank_row_index, e.settlement_id)
        tier = "T3" if (key in t3_exc_ids) else "T2"
        all_exceptions.append({
            "exc_type":       e.exc_type,
            "bank_row":       e.bank_row_index,
            "settlement_id":  e.settlement_id,
            "settlement_utr": e.settlement_utr,
            "detail":         e.detail,
            "tier_reached":   tier,
        })
    # Any pure T3 exceptions (e.g. T3_INIT_ERROR)
    for e in t3_exceptions:
        if (e.get("bank_row"), e.get("settlement_id")) not in t2_exception_ids:
            all_exceptions.append(e)

    n_settlement_credits = sum(1 for r in bank_rows if r.is_settlement_credit)
    total_matched = t1.n_matched + t2.n_matched + len(t3_matches)

    tiers = ["T1", "T2"]
    if run_t3:
        tiers.append("T3")

    return {
        "run_at":                   now,
        "pipeline_tiers":           tiers,
        "data_dir":                 str(data_dir),
        "total_bank_rows":          len(bank_rows),
        "total_order_rows":         len(order_rows),
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
