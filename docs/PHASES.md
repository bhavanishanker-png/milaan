# Milaan — Phased Build Plan

Eight phases across four stages. Each phase has a **measurable exit gate**. Do not start the next phase until the gate passes — this project's failure mode is a half-built agent sitting on top of an unmeasured pipeline.

```
STAGE 1  FOUNDATION      P0 · P1 · P2        Days 1–3    no AI yet
STAGE 2  MEASURE         P3                  Days 3–4    ← the critical gate
STAGE 3  RESOLVE         P4 · P5 · P6        Days 5–11   the actual system
STAGE 4  PROVE           P7 · P8             Days 12–14  evidence + narrative
```

---

## STAGE 1 — FOUNDATION

Days 1–3. No AI in this stage at all. If you reach for an LLM here, you're solving the wrong problem.

---

### Phase 0 — Skeleton and money type
**Half a day. Day 1 morning.**

**Goal:** make it structurally impossible to introduce the bug that kills this project.

**Tasks**
- `git init`, repo structure per the spec, `pyproject.toml`, `Makefile` stubs
- `src/milaan/normalize/money.py` — a `Paise` type. Integer-backed. Explicit constructors from rupees and from strings. Arithmetic operators that refuse to touch floats.
- `tests/test_no_floats.py` — AST-walks `src/` and fails on any float literal or `float()` call in the money path
- `DECISIONS.md` — first entry today, dated
- Pin your model version in `config/`

**Exit gate**
- `make test` passes
- The float-guard test **actually fails** when you deliberately introduce a float. Verify this. A guard you never saw fail is a guard you don't know works.

**If behind:** nothing here is cuttable. It's half a day and everything depends on it.

---

### Phase 1 — Synthetic data engine
**Days 1 (afternoon) – 2.**

**Goal:** three input files plus ground truth, reproducible from a seed. This phase *is* half the project — the quality of your evaluation is capped by the quality of your generator.

**Tasks**
- Clean-path generators: orders → payments → settlement batches → bank credits, with correct fee/GST/net arithmetic
- `discrepancies.py` — one function per D-code, D01 through D15, each returning the label record it injected
- `narration.py` — the six bank narration templates, including truncated and absent UTRs
- Noise lines: rent, GST payments, sweeps, direct customer NEFTs
- Deliberately unresolvable cases (2–3 per batch)
- Emit `labels.json` with `true_matches`, `injected_discrepancies`, `unresolvable`
- Generate batches A (seed 1), B (seed 2), C (seed 3) at 1,000 records each

**Exit gate**
- Same seed → byte-identical output, twice in a row
- All 15 D-codes present in each batch, with counts printed
- Hand-verify **three** injected discrepancies by opening the CSVs and tracing them yourself. If you can't trace them by hand, your labels are wrong and every metric downstream is fiction.
- Batch C generated and then **not opened again until Phase 8**

**Risk:** the temptation to make the data easy. Resist it. A generator that only produces clean matches gives you a 99% match rate that means nothing and collapses under one panel question.

**If behind:** drop D09, D13, D15 (international, adjustments, currency mismatch). Keep D03, D05, D06, D07, D08 — those are the ones that make the solver and agent necessary.

---

### Phase 2 — Normalization
**Day 3 (morning).**

**Goal:** three messy sources become one canonical internal representation.

**Tasks**
- Source adapters: `bank_hdfc.py`, `psp_razorpay.py`, `ledger.py` — each maps a schema, nothing more
- `utr.py` — regex family for extraction, fuzzy recovery for truncated refs, emits `utr_confidence`
- Timezone canonicalisation to IST, once, at the boundary
- Everything monetary through `Paise`

**Exit gate**
- 100% of rows parse, zero crashes, zero silent drops
- Print UTR extraction stats: exact / fuzzy / none. Compare against what the generator injected — you know the true count of D06 and D07, so you can measure the extractor directly.
- `test_no_floats` still green

**If behind:** ship regex-only UTR extraction, skip fuzzy recovery, and let D06 fall through to the solver. Note it as a known gap.

---

## STAGE 2 — MEASURE

Day 3–4. One phase, and it's the one that decides whether this submission is credible.

---

### Phase 3 — Baseline match and metrics harness
**Days 3 (afternoon) – 4.**

**Goal:** a number. Any number, as long as it's honestly measured.

**Tasks**
- T1 deterministic matcher: exact UTR + exact batch-total amount. Deliberately dumb.
- `eval/evaluate.py` — the **only** file permitted to read `labels.json`
- `tests/test_no_label_leak.py` — fails if anything under `src/` imports or opens the labels file
- Metric computation: auto-resolve rate, precision, recall, F1, **false-match rate as its own line**
- Exception list grouped by reason code
- `make evaluate` prints a clean table

**Exit gate**
- Baseline reported on batch A. Expect roughly 70–80% auto-resolve from T1 alone; if it's 95%+, your generator is too clean — go back to Phase 1.
- False-match rate computed and displayed separately from precision
- Label-leak test green

**This is the project's critical gate.** Everything after this phase is measured against the baseline you set here. Without it you're building blind and the ablation table — your strongest single piece of evidence — becomes impossible to produce retroactively.

**If behind:** cut *anything else in the project* before you cut this. A submission with T1 + T2 and rigorous metrics scores better on this track's stated bar than a full agent with hand-waved results.

---

## STAGE 3 — RESOLVE

Days 5–11. The actual system.

---

### Phase 4 — Constrained solver
**Days 5–7.**

**Goal:** close the many-to-one mapping problem algorithmically, before any LLM touches it.

**Tasks**
- Candidate windowing: `[date − 1, date + 3]`, bucketed by settlement_id
- Subset-sum against the bank credit, tolerance `±(n × 3)` paise
- Bounded search: cap subset cardinality, meet-in-the-middle or DP, hard time-box per cluster
- **Ambiguity detection** — if two or more subsets satisfy the target, resolve nothing and mark the cluster for T3
- Reverse direction: settlement rows with no bank credit, ledger orders with no settlement (D10)

**Exit gate**
- Auto-resolve materially above the Phase 3 baseline — target +10 to +15 points
- **False-match rate did not rise.** If it did, your tolerance is too loose or ambiguity detection isn't firing. Fix before proceeding.
- Zero hangs across all three batches, with the time-box logged when it triggers
- Per-class table shows D01, D02, D07 largely resolved here

**Risk:** this is where the combinatorial explosion lives. Time-box early rather than after it hangs — though if it does hang first, write it up, because it's an excellent `what broke` answer.

**If behind:** cap subset size at 3 and accept lower recall on large batches. Record the limitation.

---

### Phase 5 — Agent tier
**Days 8–10.**

**Goal:** resolve the residue that resists arithmetic — and *only* the residue.

**Tasks**
- LangGraph: `classify → hypothesise → gather_evidence → verify_arithmetic → propose | flag`, iteration cap 6
- The seven tools from the spec
- `verify.py` — deterministic Python arithmetic check. **Not the model.** Rejected hypotheses loop back.
- Precedent RAG: index resolved cases, retrieve k=5 by cluster fingerprint
- Every tool call recorded into `evidence` for the audit trail

**Exit gate**
- Residual clusters resolved with traceable rationales
- **Ablation run:** T1+T2 vs. T1+T2+T3. The agent's marginal contribution is quantified in points of auto-resolve and rupees of cost. If it's not positive, say so — that's a finding, not a failure.
- Resolution rate on the first 200 clusters vs. the last 200, showing whether precedent retrieval actually helps
- False-match rate still under control

**Risk:** scope creep into a general-purpose chatbot. The agent gets one cluster, has seven tools, and terminates. It is not a conversational assistant.

**If behind:** drop the RAG layer and run the agent without precedent. Keep the verify node — without it the agent produces confident arithmetic nonsense and your false-match rate blows up.

---

### Phase 6 — Policy gate, audit, journal
**Day 11.**

**Goal:** make the system safe to deploy, and visibly so.

**Tasks**
- `gate.py` — confidence ∧ amount ∧ class, thresholds in `config/policy.yaml`, never hardcoded
- `audit.py` — append-only log, every tier, every decision, with actor and rationale. Hash-chain if time allows.
- `journal.py` — proposed accounting entries for auto-applied resolutions
- `eval/sweep.py` — threshold sweep, plot auto-resolve against false-match rate

**Exit gate**
- Threshold curve plotted, operating point chosen **and justified** in one written sentence
- Audit log reconstructs the full decision path for any given bank row
- Chargebacks never auto-apply, regardless of confidence — verify with a test

**This phase is cheap and disproportionately valuable.** It's what separates a matching script from something a fintech would actually run, and the threshold curve is one of your best slides.

**If behind:** skip the hash chain and the journal entries. Never skip the gate itself.

---

## STAGE 4 — PROVE

Days 12–14. The system exists; now make the evidence undeniable.

---

### Phase 7 — Exception queue and full ablation
**Day 12.**

**Goal:** show the human-in-the-loop path, and fill in the evidence tables.

**Tasks**
- Streamlit exception queue: reason code, cluster detail, agent rationale, evidence trail, approve/reject
- Full four-row ablation table: T1 / T1+T2 / +T3 / +RAG, each with auto-resolve, false-match, cost per 100
- Per-class recovery table across all 15 D-codes: injected / resolved / escalated / missed
- Throughput and latency numbers, p50 and p95

**Exit gate**
- Both tables complete with real numbers
- The UI renders a real escalated exception end to end — this is what you'll demo in the video
- Cost per 100 records measured, not estimated

**Risk:** CSS. One functional page. Time spent styling is time stolen from Phase 8.

**If behind:** cut the UI to a printed exception report. Never cut the ablation table.

---

### Phase 8 — Freeze, held-out run, and narrative
**Days 13–14.**

**Goal:** the deliverables the panel actually consumes.

**Day 13 — freeze and document**
- **Code freeze.** No tuning after this line.
- Run batch C, once. Whatever it prints goes in the README. No re-runs, no adjustments.
- `README.md` — the ten-section skeleton from the spec, headline result table first
- `ARCHITECTURE.md` — diagram, tier rationale, D-code → expected-tier map, and where reality disagreed with your expectation
- Mine `DECISIONS.md` for the three strongest `what broke` incidents: expectation, symptom, diagnosis, fix, metric before and after
- Clean-clone test: fresh venv, `make generate && make run && make evaluate` inside 60 seconds

**Day 14 — pitch and buffer**
- Record to the 5:00 structure: problem 0:30, architecture 2:00, live batch-C run 3:30, metrics 4:30, honest gaps 5:00
- **Demo an escalation, not a clean match.** Show the audit trail on screen at least once.
- Submit: repo URL, video, architecture doc

**Exit gate**
- Clean clone runs green on a machine that never built the project
- Video under 5:00
- README leads with numbers, including false-match rate
- You can answer, without hesitation: why this model, why LangGraph over a plain loop, why subset-sum over an ML matcher, why 0.85

**Day 14 is buffer, not work.** If you're recording for the first time on day 14, you've already lost the buffer. Aim to have a rough cut on day 13 evening.

---

## Dependency map

```
P0 ──▶ P1 ──▶ P2 ──▶ P3 ──▶ P4 ──▶ P5 ──▶ P6 ──▶ P7 ──▶ P8
              │              │       │              ▲
              └──────────────┴───────┴──────────────┘
                    all feed the evidence tables
```

P3 blocks everything downstream — you cannot ablate what you never baselined.
P4 must land before P5, or the agent gets flooded with clusters arithmetic should have handled and both your cost and your false-match rate go through the roof.

---

## Cut order, if you fall behind

Cut from the bottom up, in this order:

1. Hash-chained audit log
2. Precedent RAG
3. Streamlit UI → printed report
4. D09, D13, D15 discrepancy classes
5. Fuzzy UTR recovery
6. Subset cardinality above 3

**Never cut:** the metrics harness, the held-out batch protocol, the ablation table, the verify node, the policy gate, or the false-match rate.

A smaller system with honest, held-out numbers beats a larger one with unmeasured claims — on this track, explicitly so. The stated bar is throughput plus measured accuracy plus an honest exception list. Two of those three are evidence, not features.
