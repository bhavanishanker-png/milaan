# CLAUDE.md

Instructions for Claude Code working in this repository. Read this before any task.

## What this is

**Milaan** — an autonomous settlement reconciliation controller. It reconciles three sources (bank statement, PSP settlement report, internal order ledger), auto-resolves what it can deterministically, escalates the ambiguous residue to an LLM agent under a hard policy gate, and reports honest metrics on a held-out batch.

Built for the Razorpay AI Buildathon, Track 04 — AI Finance Controller. The stated bar is **throughput plus measured accuracy plus an honest exception list**. Two of those three are evidence, not features. Optimise accordingly.

Full design: `docs/SPEC.md`. Build order and exit gates: `docs/PHASES.md`. Per-phase task briefs: `tasks/P0.md` … `tasks/P8.md`.

## Non-negotiable rules

These are not style preferences. Violating any of them breaks the project.

1. **Integer paise only.** Every monetary value is a `Paise` (see `src/milaan/normalize/money.py`). No floats in the money path, ever — not in the generator, not in tests, not "just for this one calculation". Floats accumulate rounding drift that makes the subset-sum target unreachable, and the failure appears days later in the solver.

2. **`labels.json` is readable only by `eval/`.** Nothing under `src/` may import, open, or reference it. There is a test enforcing this. If you find yourself wanting ground truth inside `src/`, you are about to invalidate every metric in the submission.

3. **Batch C is frozen.** `data/c/` is generated once and not read until Phase 8. Never tune against it, never inspect it, never "just check" it. Develop on A, tune thresholds on B.

4. **The LLM is the last tier, not the first.** Tiers 1 and 2 are deterministic algorithms and must stay that way. If a problem can be solved by arithmetic or search, solve it that way. The agent handles only what survives.

5. **Arithmetic verification is Python, not the model.** The agent proposes a hypothesis; `src/milaan/agent/verify.py` checks whether it actually sums. Never trust a model-asserted total.

6. **No auto-apply on chargebacks.** Regardless of confidence. There is a test.

7. **Thresholds live in `config/policy.yaml`.** Never hardcode a confidence or amount limit in Python.

## Architecture

```
T0 normalize  → schema adapters, paise conversion, UTR extraction, IST canonicalisation
T1 match      → exact UTR + exact amount            (deterministic, ~70-80%)
T2 solver     → bounded subset-sum + ambiguity check (deterministic, +10-15%)
T3 agent      → LangGraph, tools, RAG precedents     (LLM, residue only)
T4 gate       → confidence ∧ amount ∧ class          (deterministic)
              → journal entries + exception queue + append-only audit log
```

**Ambiguity rule:** if more than one subset of payments sums to a bank credit within tolerance, resolve nothing and escalate. Picking one is how false matches happen, and false-match rate is the headline number we report.

## Commands

```bash
make setup       # install deps
make generate    # build batches A, B, C from seeds
make run         # full pipeline on batch A
make evaluate    # metrics vs labels
make ablation    # T1 / T1+T2 / +T3 / +RAG comparison table
make sweep       # threshold sweep, auto-resolve vs false-match curve
make test        # pytest, includes the guard tests
make demo        # streamlit exception queue
```

## Working style

- **Never skip the metrics harness to move faster.** An unmeasured improvement is not an improvement. If a change can't be evaluated, it doesn't land.
- **Run `make evaluate` after every substantive change** and report the delta in auto-resolve *and* false-match rate. A gain in one bought with a loss in the other is usually a regression.
- **Append to `DECISIONS.md`** whenever you make a non-obvious choice or hit a real failure: what was expected, what happened, the diagnosis, the fix, and the metric before and after. The submission requires a "what broke" account and it cannot be reconstructed from memory on day 14.
- **Respect phase gates.** Do not start a phase whose predecessor's exit gate hasn't passed. `docs/PHASES.md` lists them.
- **Prefer small, testable modules.** One responsibility each. Adapters are per-source, discrepancy injectors are one function per D-code.
- **When behind schedule**, cut in the order listed at the bottom of `docs/PHASES.md`. Never cut the metrics harness, the held-out protocol, the ablation table, the verify node, the policy gate, or false-match reporting.

## Data model

Three inputs plus ground truth. Schemas in `docs/SPEC.md` §3. Key facts:

- Bank credits are **aggregated** — one credit line equals a whole settlement batch of many payments. The mapping is many-to-one and that's the core difficulty.
- The join key is a UTR buried in free-text bank narration, often truncated or absent.
- Fee arithmetic: `fee = round(gross × rate)`, `gst = round(fee × 0.18)`, `net = gross − fee − gst`. Every `round()` is a place paise go missing.
- Batch total: `Σ net(payments) − Σ refund_gross − Σ chargeback_amount − Σ chargeback_fee`.

15 discrepancy classes, `D01`–`D15`, defined in `docs/SPEC.md` §4. Each gets a stable reason code that flows through to the exception report.

## Testing

- `tests/test_no_floats.py` — AST-scans the money path, fails on float literals or `float()` calls
- `tests/test_no_label_leak.py` — fails if `src/` touches `labels.json`
- `tests/test_fee_math.py` — fee/GST/net rounding, including the paise-drift cases

All three must stay green. If a guard test starts failing, fix the code, not the test.

## Definition of done for the whole project

- Clean clone → `make generate && make run && make evaluate` in under 60 seconds
- README leads with a headline table including **false-match rate**
- Ablation table proves the LLM tier earned its cost, quantified in points and rupees
- Per-class recovery table across all 15 D-codes, including the ones we fail
- Final numbers come from a single run on batch C after code freeze
