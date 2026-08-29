# Milaan — Autonomous Settlement Reconciliation Controller

**Track:** 04 — AI Finance Controller
**Submission:** public GitHub repo + 5-min pitch video + architecture doc

*Milaan* (मिलान) is Hindi for "matching / reconciliation". Name it whatever you like, but a name that isn't `recon-agent-final-v2` signals you treated this as a product.

---

## 1. The one-liner

Milaan closes the three-way settlement reconciliation loop — bank statement ↔ PSP settlement report ↔ internal order ledger — across a 1,000-record batch, auto-resolving the routine matches deterministically and escalating only the genuinely ambiguous residue to an LLM agent operating under a hard policy gate. It reports match rate, false-match rate, and an honest exception list on a held-out batch it has never seen.

---

## 2. Domain primer — read this before you write code

You cannot build a convincing finance agent without understanding the money movement. The flow:

1. A customer pays ₹5,000 on a merchant's checkout. This creates a **payment** in the PSP system and should correspond to an **order** in the merchant's own database.
2. The PSP holds the money, deducts a **platform fee** (say 2%) and **GST on that fee** (18% of the fee, not of the transaction).
3. On a T+2 cycle, the PSP batches up all eligible payments and wires the **net** amount to the merchant's bank account as a **single credit** with a **UTR** (Unique Transaction Reference).
4. Refunds and chargebacks that occurred in the window are **netted off inside that same batch** — they don't arrive as separate debits.

### The core difficulty

The bank sees **one credit line**. The settlement report sees **many payment rows**. The mapping is **many-to-one**, and the only join key is a UTR that lives inside a free-text bank narration string, often truncated or mangled by the bank's own systems.

So the actual problem is: *given a bank credit of ₹4,82,331.44 on 29 Aug, which set of payments, minus which fees, minus which refunds, explains exactly this number?* And then: *what's in the ledger that isn't in the settlement, and what's in the settlement that isn't in the ledger?*

That's a constrained search problem with a messy-text key recovery problem bolted on top. Both are real. Neither is solved by pasting CSVs into an LLM.

### The fee arithmetic

```
fee      = round(gross × fee_rate)
gst      = round(fee × 0.18)
net      = gross − fee − gst
batch_credit = Σ net(payments) − Σ refund_gross − Σ chargeback_amount − Σ chargeback_fee
```

Every one of those `round()` calls is a place where paise go missing. **Work in integer paise everywhere.** Never a float, not once, not in the generator, not in the tests. If you take one thing from this spec, take that.

---

## 3. Data model

Three inputs plus a ground-truth labels file that only the metrics harness may read.

### 3.1 Internal order ledger (`ledger.csv`)

The merchant's own truth about what they sold.

| Column | Type | Notes |
|---|---|---|
| `order_id` | str | `ORD-2026-000001` |
| `order_date` | date | |
| `customer_id` | str | |
| `gross_amount_paise` | int | |
| `currency` | str | `INR`, occasionally `USD` |
| `status` | enum | `paid`, `refunded`, `partially_refunded`, `pending` |
| `payment_ref` | str | the PSP payment id, **nullable and sometimes wrong** |
| `channel` | enum | `web`, `app`, `pos` |

### 3.2 PSP settlement report (`settlement.csv`)

One row per money-moving entity in a settlement batch.

| Column | Type | Notes |
|---|---|---|
| `entity_id` | str | `pay_XXXX`, `rfnd_XXXX`, `disp_XXXX`, `adj_XXXX` |
| `type` | enum | `payment`, `refund`, `chargeback`, `adjustment` |
| `debit_paise` | int | |
| `credit_paise` | int | |
| `amount_paise` | int | gross |
| `fee_paise` | int | |
| `tax_paise` | int | GST on fee |
| `settlement_id` | str | `setl_XXXX` — groups rows into a batch |
| `settlement_utr` | str | the join key to the bank |
| `created_at` | timestamp | |
| `settled_at` | timestamp | |
| `method` | enum | `upi`, `card`, `netbanking`, `wallet`, `emi` |
| `order_receipt` | str | merchant's `order_id`, **nullable** |
| `notes` | str | free text |

> Before you finalise this, pull Razorpay's published settlement-report column list and match their exact names and casing. It costs you an hour. A panel of Razorpay engineers will recognise their own schema instantly, and it converts your project from "a reconciliation demo" into "a reconciliation demo for *our* system."

### 3.3 Bank statement (`bank.csv`)

Deliberately hostile. This is the file that makes the project hard.

| Column | Type | Notes |
|---|---|---|
| `txn_date` | date | |
| `value_date` | date | sometimes differs from `txn_date` |
| `narration` | str | free text, the UTR is in here somewhere |
| `ref_no` | str | often blank or a bank-internal code |
| `withdrawal_paise` | int | |
| `deposit_paise` | int | |
| `closing_balance_paise` | int | |

Narration variants to generate — mix them:

```
NEFT CR-HDFC0000060-RAZORPAY SOFTWARE PVT LTD-ACME RETAIL-N226082912345678
RTGS/UTR226082900098765/RAZORPAY/SETTLEMENT
IMPS/226082912345/RZPY SOFTWARE/SETTLEMENT AUG
NEFT-RAZORPAY-SETTLEMENT-AUG26                     <- UTR entirely absent
NEFT CR-...-N22608291234                           <- UTR truncated mid-string
UPI/226082912345678/COLLECT/CUSTOMER PAYMENT       <- not a settlement at all
```

Also seed genuinely unrelated lines: rent debits, GST payments, a direct customer NEFT, an inter-account sweep. A reconciliation system that assumes every credit is a settlement is not a reconciliation system.

### 3.4 Ground truth (`labels.json`)

Written by the generator, read **only** by `evaluate.py`. Your pipeline must never import it — enforce this with a test.

```json
{
  "batch_id": "C",
  "seed": 20260829,
  "true_matches": [
    {"bank_row": 14, "settlement_ids": ["setl_A1"], "entity_ids": ["pay_001", "..."]}
  ],
  "injected_discrepancies": [
    {"id": "D-0041", "class": "NETTED_REFUND", "entities": ["rfnd_007"], "bank_row": 14,
     "expected_resolution": "REFUND_NETTED_IN_BATCH", "expected_action": "auto_resolve"}
  ],
  "unresolvable": ["D-0093"]
}
```

Include a few **deliberately unresolvable** discrepancies — a credit with no supporting data anywhere. A system that claims 100% resolution is a system that is lying, and including known-impossible cases lets you prove your escalation path works.

---

## 4. Discrepancy taxonomy

Inject at ~15% of records. Each class gets a stable reason code — these codes flow all the way through to your exception report.

| Code | Class | Injection | Which tier should catch it |
|---|---|---|---|
| `D01` | Timing lag | T+2 crossing month boundary | Tier 2 (date window) |
| `D02` | Fee rounding drift | ±1–3 paise on net | Tier 2 (tolerance) |
| `D03` | Netted refund | refund inside a batch | Tier 2 or 3 |
| `D04` | Chargeback debit | negative line in a clean batch | Tier 3 |
| `D05` | Duplicate payment | same order charged twice | Tier 3 |
| `D06` | Mangled narration | UTR truncated | Tier 1 (fuzzy) |
| `D07` | Missing narration | no UTR at all | Tier 2 (amount+date search) |
| `D08` | Split payment | one order, two payments | Tier 3 |
| `D09` | International txn | different fee slab + FX | Tier 3 |
| `D10` | Never settled | captured, no settlement row | Tier 2 → exception |
| `D11` | Unidentified credit | non-PSP money | Tier 3 → classify & exclude |
| `D12` | Partial refund | refund ≠ order amount | Tier 3 |
| `D13` | Adjustment entry | PSP correction, no order | Tier 3 |
| `D14` | Ledger typo | `payment_ref` points to wrong payment | Tier 3 |
| `D15` | Currency mismatch | USD order, INR settlement | Tier 3 |

Mapping each class to the tier you *expect* to catch it is a strong architecture-doc artifact. Then show, in your results, where reality disagreed with that expectation. Panels love that; it's evidence you measured instead of assumed.

### Batch discipline

Generate three batches with different seeds:

- **A** — development. Look at it as much as you want.
- **B** — tuning. Set thresholds here.
- **C** — held-out. **Run it once, at the end.** Whatever it says is what goes in your README.

State this protocol explicitly in the README. It's how you satisfy the brief's demand for measured accuracy that isn't cherry-picked, and it's a discipline most student submissions won't bother with.

---

## 5. Architecture

Five tiers. Money flows down; only the residue reaches the LLM.

```
bank.csv  settlement.csv  ledger.csv
                 │
                 ▼
    ┌────────────────────────────┐
    │ T0  NORMALIZE              │  schema map, paise conversion,
    │                            │  UTR extraction, date canonicalisation
    └────────────┬───────────────┘
                 ▼
    ┌────────────────────────────┐
    │ T1  DETERMINISTIC MATCH    │  exact UTR + exact amount
    │     ~70-80% cleared        │  → RESOLVED
    └────────────┬───────────────┘
                 │ residual
                 ▼
    ┌────────────────────────────┐
    │ T2  CONSTRAINED SOLVER     │  subset-sum within date window
    │     ~10-15% cleared        │  and fee tolerance; bipartite match
    └────────────┬───────────────┘
                 │ residual (~5-10%)
                 ▼
    ┌────────────────────────────┐
    │ T3  LANGGRAPH AGENT        │  tools + RAG over resolved precedents
    │     one cluster at a time  │  → proposed resolution + confidence
    └────────────┬───────────────┘
                 ▼
    ┌────────────────────────────┐
    │ T4  POLICY GATE            │  confidence ∧ amount ∧ class
    │                            │  → AUTO_APPLY | ESCALATE
    └────────────┬───────────────┘
                 ▼
      journal entries + exception queue + append-only audit log
```

### T0 — Normalize

- Schema adapters per source, so a new bank format is a new adapter, not a rewrite.
- `Decimal`/`int` paise only. Add a lint rule or a test that fails on any `float` in the money path.
- UTR extraction: regex family over narration, plus a fuzzy recovery pass for truncated refs. Emit a `utr_confidence` score — do not silently guess.
- Canonicalise timezones to IST once, at the boundary. The T+2 off-by-one will bite you otherwise.

### T1 — Deterministic

Exact UTR match + exact net-amount match against the settlement batch total. No cleverness. This is your baseline and your ablation floor.

### T2 — Constrained solver

For each unmatched bank credit:
1. Candidate settlement rows within a `[date − 1, date + 3]` window.
2. Subset-sum over candidate nets, target = bank credit, tolerance = `±(n_rows × 3)` paise for rounding.
3. Cap combinatorics: bucket by settlement_id first, cap subset size, use a DP/meet-in-the-middle approach, and **time-box each search**. An uncapped subset-sum over 400 candidates will hang, and "it hung and here's how I bounded it" is one of your best `what broke` answers.
4. Ambiguity check: if **more than one** subset satisfies the target, do **not** pick one. Emit the ambiguity to T3. This single rule prevents a whole category of false matches.

No LLM in this tier. Keeping it algorithmic is itself the signal — it shows you know where AI does and doesn't earn its cost.

### T3 — LangGraph agent

Receives one unresolved **cluster** (a bank row plus its candidate settlement rows plus related ledger orders), not the whole file.

**Tools:**

```python
query_ledger(order_id=None, amount_paise=None, date_range=None, customer_id=None) -> list[Order]
query_settlement(entity_id=None, utr=None, settlement_id=None, amount_paise=None) -> list[Entity]
compute_expected_net(gross_paise: int, method: str, currency: str) -> FeeBreakdown
fetch_fee_schedule(method: str, effective_date: date) -> FeeSchedule
search_precedents(cluster_summary: str, k: int = 5) -> list[ResolvedCase]   # RAG
propose_resolution(class_code: str, entity_ids: list[str], journal_action: dict,
                   confidence: float, rationale: str) -> Resolution
flag_exception(reason_code: str, note: str) -> Exception
```

**State:**

```python
class ReconState(TypedDict):
    cluster: Cluster
    hypotheses: list[Hypothesis]      # ranked candidate explanations
    evidence: list[ToolCall]          # every call + result, for the audit log
    resolution: Resolution | None
    confidence: float
    iterations: int                   # hard cap, e.g. 6
```

**Graph:** `classify → hypothesise → gather_evidence → verify_arithmetic → (loop if unresolved and under cap) → propose | flag`

The `verify_arithmetic` node is non-negotiable and must be **deterministic Python, not the model**. The agent proposes "this credit = payments X+Y minus refund Z"; Python checks whether that actually sums to the target. If it doesn't, the hypothesis is rejected and the loop continues. LLMs are good at hypothesis generation and bad at arithmetic; this node is where you encode that fact into the architecture.

**RAG:** index resolved cases as `{cluster fingerprint → resolution class + rationale}`. Retrieval gives the agent precedent, and it means the system genuinely improves as the batch progresses. Show that: resolution rate on the first 200 clusters vs. the last 200.

### T4 — Policy gate

```python
AUTO_APPLY if (confidence >= 0.85
               and amount_paise <= 50_000_00
               and class_code in AUTO_SAFE_CLASSES
               and not touches_chargeback)
else ESCALATE(reason_code)
```

Thresholds live in a config file, not in the code. Then run a **threshold sweep** and plot auto-resolution rate against false-match rate. That curve is a genuinely strong slide — it shows you understand this is a business tradeoff dial, not a fixed constant, and it lets you say "at our chosen operating point we accept X% escalation to hold false matches under Y%."

Every action — every tier, not just T3 — appends to an immutable audit log: timestamp, actor (`tier1` / `tier2` / `agent` / `human`), inputs, decision, confidence, rationale, resulting journal entry. Hash-chain the entries if you want an extra half-point of fintech credibility.

---

## 6. Metrics

Build the harness on **day 3**, before the solver and long before the agent. Every subsequent decision then gets measured rather than argued about.

**Primary:**
- Auto-resolution rate — % closed with zero human touch
- **False-match rate** — % of auto-applied matches that contradict ground truth. *Report this in its own line, prominently.* It's the number that costs real money, and volunteering it unprompted is the single clearest maturity signal in the whole submission.
- Precision / recall / F1 on match decisions
- Exception list grouped by reason code, with counts and a one-line human-readable explanation each

**Operational:**
- Records/minute end-to-end
- LLM cost per 100 records
- % of records that ever reached T3 (should be small — that's the point)
- p50 / p95 latency per cluster

**The ablation — your money chart:**

| Configuration | Auto-resolve | False match | Cost/100 |
|---|---|---|---|
| T1 only | | | ₹0 |
| T1 + T2 | | | ₹0 |
| T1 + T2 + T3 | | | |
| T1 + T2 + T3 + RAG | | | |

If the agent tier adds 6 points of resolution on 4% of records for ₹2 per 100, **say exactly that**. A quantified, honest marginal contribution beats a vague claim of AI-poweredness every single time — and it directly answers the "sensible AI usage" criterion Razorpay lists.

**Per-class recovery table:** for each of your 15 discrepancy codes, injected count vs. correctly resolved vs. escalated vs. missed. This is your honest exception list, and it doubles as a roadmap slide: "D09 and D15 are where we're weakest, here's why."

---

## 7. Repo structure

```
milaan/
├── README.md                 # the most important file in the repo
├── ARCHITECTURE.md           # diagram + tier rationale + class→tier map
├── DECISIONS.md              # running log, start it on day 1
├── Makefile                  # make generate / run / evaluate / demo
├── pyproject.toml
├── config/
│   ├── policy.yaml           # gate thresholds
│   └── fee_schedule.yaml
├── src/milaan/
│   ├── generate/             # synthetic data + labels
│   │   ├── entities.py
│   │   ├── discrepancies.py  # one function per D-code
│   │   └── narration.py      # bank narration templates
│   ├── normalize/
│   │   ├── adapters/         # bank_hdfc.py, psp_razorpay.py, ledger.py
│   │   ├── money.py          # paise-only arithmetic, no floats
│   │   └── utr.py            # extraction + fuzzy recovery
│   ├── match/
│   │   ├── deterministic.py  # T1
│   │   └── solver.py         # T2, bounded subset-sum
│   ├── agent/
│   │   ├── graph.py          # LangGraph definition
│   │   ├── tools.py
│   │   ├── verify.py         # deterministic arithmetic check
│   │   └── precedents.py     # RAG index
│   ├── policy/
│   │   ├── gate.py           # T4
│   │   └── audit.py          # append-only log
│   ├── journal.py            # proposed accounting entries
│   └── report.py             # exception list + metrics output
├── eval/
│   ├── evaluate.py           # ONLY file permitted to read labels.json
│   ├── ablation.py
│   └── sweep.py              # threshold sweep
├── ui/                       # thin Streamlit exception queue
├── data/{a,b,c}/             # generated batches, gitignored
└── tests/
    ├── test_no_floats.py     # fails if a float enters the money path
    ├── test_no_label_leak.py # fails if src/ imports labels.json
    └── test_fee_math.py
```

Those two guard tests are cheap and they're a talking point in the panel. "How do you know your pipeline isn't peeking at the answers?" — "There's a test for that."

**Stack:** Python 3.11+, LangGraph, Pydantic (strict money types), DuckDB or SQLite, FAISS or Chroma for precedents, Streamlit for the queue, `structlog` for the audit trail. Pin your model version in config; the panel will ask which model and why.

---

## 8. Fourteen-day plan

| Days | Deliverable | Done when |
|---|---|---|
| 1–2 | Generator + labels + all 15 D-codes | 3 batches generate reproducibly from a seed |
| 3–4 | T0 normalize, T1 deterministic, **metrics harness** | Baseline number printed on batch A |
| 5–7 | T2 solver with bounded search + ambiguity detection | Auto-resolve materially above T1, zero hangs |
| 8–10 | T3 LangGraph agent, tools, verify node, RAG | Residual clusters resolved with rationales |
| 11 | T4 gate, audit log, journal entries | Threshold sweep curve plotted |
| 12 | Streamlit exception queue + ablation runs | Ablation table filled in |
| 13 | README, ARCHITECTURE, **held-out run on batch C** | Final numbers locked, no re-tuning after |
| 14 | Pitch video + buffer | Recorded, under 5:00 |

**Day-3 rule:** if the metrics harness isn't running by end of day 3, cut the agent's RAG layer rather than the harness. Unmeasured output fails this track's stated bar outright; a slightly simpler agent does not.

---

## 9. Scope guards — what *not* to build

Explicit non-goals belong in your README. They read as judgement, not laziness.

- **No auth, no multi-tenancy, no user accounts.** Single merchant, single account.
- **No real bank or PSP integration.** Synthetic data is the correct choice here and you should defend it as such: it's the only way to have ground truth, which is the only way to report honest precision and recall.
- **No pretty dashboard.** One functional Streamlit exception queue. Time spent on CSS is time not spent on the solver.
- **No forecasting, no anomaly detection, no chatbot.** One loop, closed properly, beats four loops half-built. The brief says *one* finance-ops loop.
- **No fine-tuning.** Prompting plus retrieval plus deterministic verification is the right call at this scale, and knowing that is itself the signal.

---

## 10. README skeleton

Write this on day 13, but know the shape now — it's what the panel reads first.

1. **What it does** — two sentences, in money terms
2. **The result** — headline table: records processed, auto-resolve %, false-match %, escalation %, cost/100, on batch C, held out
3. **Why it's structured this way** — the tiering rationale, one paragraph
4. **Architecture diagram**
5. **The ablation table** — proof the AI earned its keep
6. **Per-class recovery table** — the honest exception list, including what it can't do
7. **Evaluation protocol** — three batches, C untouched until the end, labels never read by `src/`
8. **What broke** — three real incidents with the fix and the before/after metric
9. **Run it in 60 seconds** — `make generate && make run && make evaluate`
10. **Non-goals** — section 9 above

Reproducibility is a stated bar. Test the 60-second path on a clean clone in a fresh venv before you submit. Every year, submissions die on a missing `requirements.txt`.

---

## 11. The "what broke" answer

They explicitly ask what broke and how you recovered. Keep `DECISIONS.md` from day one — you will not remember by day 14.

Failures this build reliably produces, any of which makes a strong answer:

- **Float rounding on GST.** Paise drift accumulates across a 400-row batch until the subset-sum target is unreachable. Fix: integer paise everywhere, plus a regression test.
- **Confident wrong subsets.** The solver finds *a* combination summing to the target that isn't the *true* combination. This is the false-match problem in its purest form. Fix: ambiguity detection — if multiple subsets fit, escalate rather than choose.
- **Combinatorial explosion.** Uncapped subset-sum on a large batch hangs. Fix: bucket by settlement_id, cap subset cardinality, time-box, degrade to T3 on timeout.
- **Timezone off-by-one.** T+2 across an IST/UTC boundary silently shifts a whole day's batch out of the date window.
- **Agent arithmetic.** The model asserts a sum that doesn't add up. Fix: the deterministic `verify` node — and note that you found this *because* you had a metric, not because you eyeballed it.

Tell one of these properly: what you expected, what happened, how you diagnosed it, the fix, the metric before and after. That is a far better answer than a polished non-story, and it's the question most candidates will fumble.

---

## 12. Pitch video — 5:00

| Time | Content |
|---|---|
| 0:00–0:30 | The problem in money terms. One bank credit, forty payments, where do the paise go. |
| 0:30–2:00 | Architecture. Emphasise that **most records never touch an LLM** — and why that's the point. |
| 2:00–3:30 | Live run on batch C. **Demo an exception being correctly escalated, not the happy path.** |
| 3:30–4:30 | Metrics: auto-resolve, false-match rate, ablation table, threshold curve. |
| 4:30–5:00 | Honest exception list, weakest classes, what you'd build next. |

Show the audit trail on screen at least once. In fintech, "who decided this and why" is a product feature, and the panel will register that you knew it.

Know your architecture cold. Be ready to justify: why this model, why LangGraph over a plain loop, why subset-sum over an ML matcher, why 0.85 as the confidence threshold. "It felt right" is where weak submissions get exposed. Every threshold in your config should have a sweep behind it.

---

## First commit

```bash
mkdir milaan && cd milaan && git init
mkdir -p src/milaan/{generate,normalize,match,agent,policy} eval tests config data
touch DECISIONS.md ARCHITECTURE.md
```

Then write `src/milaan/normalize/money.py` — the paise type, with tests — before anything else. Everything downstream depends on getting money representation right, and it is the cheapest thing to fix on day 1 and the most expensive on day 10.
