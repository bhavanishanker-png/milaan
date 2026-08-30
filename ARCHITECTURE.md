# Milaan — Architecture

## Pipeline diagram

```
┌────────────┐  ┌──────────────┐  ┌─────────────┐
│  bank.csv  │  │settlement.csv│  │  ledger.csv  │
└─────┬──────┘  └──────┬───────┘  └──────┬───────┘
      │                │                  │
      └────────────────┴──────────────────┘
                       │
                  ┌────▼────┐
                  │  T0     │  normalize: Paise, IST, UTR regex
                  │ normalize│  adapters: bank_hdfc, psp_razorpay, ledger
                  └────┬────┘
                       │ CanonicalBankRow, SettlementBatch, CanonicalOrderRow
                  ┌────▼────┐
                  │  T1     │  exact UTR + exact batch-total amount
                  │  match  │  deterministic · ~70–90% of batches
                  └────┬────┘
                       │ T1Result (matches + exceptions)
                  ┌────▼────┐
                  │  T2     │  fuzzy UTR (rapidfuzz)
                  │  solver │  amount tolerance (±n×3 paise)
                  │         │  date+amount window (−1 to +3 days)
                  │         │  ambiguity rule: two subsets → escalate
                  └────┬────┘
                       │ T2Result (matches + residual exceptions)
                  ┌────▼────┐
                  │  T3     │  LangGraph agent (residue only)
                  │  agent  │  classify → hypothesise → verify → propose|flag
                  │         │  7 tools · arithmetic verified by Python
                  │         │  precedent RAG (TF-IDF cosine)
                  └────┬────┘
                       │ Resolutions
                  ┌────▼────┐
                  │  T4     │  confidence ∧ amount ∧ class
                  │  gate   │  chargebacks: NEVER auto-apply
                  └────┬────┘
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
    journal.ndjson  audit.ndjson  results.json
    (accounting)    (hash-chain)  (pipeline output)
```

## Tier rationale and the choices that matter

### Why subset-sum, not ML matching?

The match criterion is arithmetic: `Σnet(payments) − Σrefund_gross − Σchargeback_amount − Σchargeback_fee = bank_credit ± tolerance`. This is a deterministic membership test with a known error bound (per-step half-up rounding creates at most `n × 1` paise drift per payment). Subset-sum gives an exact answer. An ML classifier would need labelled training data, would generalise poorly to new PSPs, and would be confidently wrong on ambiguous subsets — the case where we most need it to say "I don't know."

### Why the ambiguity rule is non-negotiable

If two subsets both satisfy the bank credit within tolerance, there is no arithmetic basis to prefer one over the other. Picking one introduces a false match. The false-match rate is the headline evidence metric; one wrong guess degrades it permanently. The correct action is to escalate both subsets for human review. This is also why T3 exists: it can gather evidence (payment timestamps, order receipts, PSP API) that breaks the tie without arithmetic.

### Why LangGraph over a plain agentic loop?

The graph `classify → hypothesise → verify_arithmetic → propose | flag` is a first-class directed graph with typed state. The conditional back-edge from `verify` to `hypothesise` is explicit and inspectable. A plain while-loop hides the retry logic and makes the maximum-iteration guarantee hard to audit. LangGraph also separates node logic from routing logic, which is the right decomposition: nodes do work, edges decide what comes next.

### Why claude-haiku-4-5-20251001?

T3 handles only the arithmetic-resistant residue — typically fewer than 10% of clusters. The task is tool-use reliability (call the right tools in the right order), not reasoning depth. Haiku is the cheapest and fastest model with reliable tool-use. Switching to Sonnet/Opus would increase cost with no measured accuracy benefit on this workload.

### Why 0.85 confidence threshold?

The sweep (`eval/sweep.py`) shows that at confidence ≥ 0.85, auto-resolve = 100% and false-match = 0% on all three batches. At 0.90, auto-resolve drops 3.3 pp (two T2 tolerance-based matches fall below). There is no false-match benefit from going above 0.85 — the tradeoff is pure recall loss for zero safety gain at this operating point. The threshold is in `config/policy.yaml`; change it with a sweep behind the decision.

## D-code → expected tier

| Code | Class | Expected tier | Actual (batch C) |
|------|-------|--------------|-----------------|
| D01 | TIMING_LAG | T2 (date window) | T2 ✓ |
| D02 | FEE_ROUNDING | T2 (amount tolerance) | T2 ✓ |
| D03 | NETTED_REFUND | T1 (exact, nets to same total) | T1 ✓ |
| D04 | CHARGEBACK_DEBIT | T1 (exact, nets to same total) | T1 ✓ |
| D05 | PARTIAL_SETTLEMENT | T1 (batch total still exact) | T1 ✓ |
| D06 | MANGLED_NARRATION | T2 (fuzzy UTR) | T2 ✓ |
| D07 | MISSING_NARRATION | T2 (date+amount window) | T2 ✓ |
| D08 | DUPLICATE_PAYMENT | T1 (deduped in batch total) | T1 ✓ |
| D09 | INTERNATIONAL_FX | T1 (FX applied in settlement net) | T1 ✓ |
| D10 | NEVER_SETTLED | T3 → exception | **Miss** (no bank row to match) |
| D11 | UNIDENTIFIED_CREDIT | T3 → exception | **Miss** (no settlement row) |
| D12 | SPLIT_SETTLEMENT | T1 (both halves present in batch) | T1 ✓ |
| D13 | ADJUSTMENT_CREDIT | T1 (adjustment included in net) | T1 ✓ |
| D14 | REVERSED_PAYMENT | T1 (reversal nets off) | T1 ✓ |
| D15 | CURRENCY_MISMATCH | T2 (amount tolerance catches drift) | T2 ✓ |

### Where reality disagreed with expectation

**D03/D04/D05/D08/D12/D13 — expected T2, got T1.** Initial assumption: discrepancy batches would have amounts that differ from the simple sum of nets, requiring T2's tolerance machinery. Wrong. The generator correctly computes `batch_total = Σnet − Σrefund_gross − Σchargeback_amount − Σchargeback_fee` even for discrepancy batches. The bank credit still equals the settlement net exactly — the discrepancy is in the *components*, not the pairing. T1 matches these cleanly. This was the P3 labels design flaw: true_matches initially excluded discrepancy batches, producing 33% false-match rate on runs that were actually correct.

**D01 — expected T1 (1-day timing lag), got T2.** A 1-day bank credit lag does not change the batch total amount, only the `value_date`. T1 requires exact UTR *and* exact amount — it does match on amounts, but the UTR in the bank narration maps to a settlement_id that has a different `settled_at` date. T2's date window catches this.

**D10/D11 — expected T3 resolution, got missed.** D10 (NEVER_SETTLED) means an order appears in the ledger with no corresponding settlement row. There is no settlement batch to match against — the pipeline has nothing to anchor the T3 cluster. D11 (UNIDENTIFIED_CREDIT) is the inverse: a bank credit with no settlement_id anywhere. Both require PSP portal lookup or bank payer identification. These are genuinely infrastructure gaps, not algorithm failures. They belong in the exception queue.

## Data model summary

```
bank.csv          → CanonicalBankRow
                    row_index, value_date, narration, deposit_paise,
                    utr (str|None), utr_confidence (exact|partial|none),
                    is_settlement_credit

settlement.csv    → CanonicalSettlementRow → grouped into SettlementBatch
                    entity_id, entity_type, settlement_id, settlement_utr,
                    net_paise, amount_paise, fee_paise, tax_paise,
                    method, created_at, settled_at

ledger.csv        → CanonicalOrderRow
                    order_id, order_date, gross_paise, currency,
                    status, payment_ref, channel

Money             → Paise (int-backed, no floats anywhere in the pipeline)
Fee formula       → fee = round(gross × rate_bps / 10000)  [half-up]
                    gst = round(fee × 0.18)
                    net = gross − fee − gst
Batch total       → Σnet(payments) − Σrefund_gross − Σchargeback − Σchargeback_fee
```

## Audit log format (append-only, hash-chained NDJSON)

```json
{
  "ts": "2026-08-30T12:34:56.789+00:00",
  "actor": "tier1",
  "event": "AUTO_APPLY",
  "bank_row": 0,
  "settlement_id": "setl_0001",
  "tier": "T1",
  "confidence": 1,
  "reason_code": "EXACT_MATCH",
  "rationale": "all gate conditions satisfied",
  "prev_hash": "abc123...",
  "hash": "def456..."
}
```

Every decision — match, gate pass, gate block, exception — is a record. Query by `bank_row` or `settlement_id` to reconstruct the full decision path for any credit line.
