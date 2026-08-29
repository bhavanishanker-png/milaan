# Architecture

> Written properly in P8. Keep the diagram current as you build; fill the tables at the end.

## Pipeline

```
bank.csv   settlement.csv   ledger.csv
                  |
        T0  NORMALIZE          schema adapters, integer paise,
                               UTR extraction, IST canonicalisation
                  |
        T1  DETERMINISTIC      exact UTR + exact amount        ~70-80%
                  | residual
        T2  CONSTRAINED        bounded subset-sum, ambiguity   +10-15%
                  | residual
        T3  AGENT              LangGraph + tools + precedents  the rest
                  |
        T4  POLICY GATE        confidence ^ amount ^ class
                  |
   journal entries | exception queue | append-only audit log
```

## Why tiered

Most reconciliation is arithmetic. Sending arithmetic to a language model costs money, adds latency, and introduces a class of error that deterministic code does not have. The LLM earns its place only on clusters where the arithmetic is ambiguous — and the ablation table in the README quantifies exactly how much it earns.

## Discrepancy class → expected tier

| Code | Class | Expected tier | Actually resolved by | Note |
|---|---|---|---|---|
| D01 | Timing lag | T2 | | |
| D02 | Fee rounding drift | T2 | | |
| D03 | Netted refund | T2/T3 | | |
| D04 | Chargeback debit | T3 | | never auto-applies |
| D05 | Duplicate payment | T3 | | |
| D06 | Mangled narration | T1 (fuzzy) | | |
| D07 | Missing narration | T2 | | |
| D08 | Split payment | T3 | | |
| D09 | International txn | T3 | | |
| D10 | Never settled | T2 → exception | | |
| D11 | Unidentified credit | T3 → classify out | | never auto-applies |
| D12 | Partial refund | T3 | | |
| D13 | Adjustment entry | T3 | | |
| D14 | Ledger typo | T3 | | |
| D15 | Currency mismatch | T3 | | never auto-applies |

Fill the "actually resolved by" column from real runs. **Where reality disagreed with the expectation is the most interesting paragraph in this document** — it is evidence you measured rather than assumed.

## Key design decisions

- **Integer paise everywhere.** See `DECISIONS.md`, first entry.
- **Ambiguity escalates rather than guesses.** If more than one subset of payments sums to a bank credit within tolerance, the solver resolves nothing. Picking one is how false matches happen.
- **Arithmetic verification is Python, not the model.** `agent/verify.py` rejects hypotheses that don't sum.
- **Thresholds are config, with a sweep behind them.** `config/policy.yaml` plus `make sweep`.
