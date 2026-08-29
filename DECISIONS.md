# Decisions and Failures

Append-only. One entry per non-obvious choice or real failure. Write the entry
the day it happens -- the submission asks what broke, and you will not
reconstruct it on day 14.

Format:

## YYYY-MM-DD | Title
**Context:** what I was doing
**Expected:** what I thought would happen
**Observed:** what actually happened
**Diagnosis:** why
**Fix:** what I changed
**Metric:** auto-resolve X% -> Y%, false-match A% -> B%

---

## 2026-08-29 | Integer paise as the money type
**Context:** Phase 0, before any pipeline code.
**Expected:** Decimal would be sufficient for fee and GST maths.
**Observed:** n/a -- decided ahead of the failure rather than after it.
**Diagnosis:** Fee and GST round per row. Across a 400-row batch the drift
accumulates, and a float or unguarded Decimal path makes the subset-sum target
unreachable while the solver reports "no match" instead of an error. The bug is
silent, appears days later, and looks like a solver problem.
**Fix:** A frozen `Paise` type over int, division disallowed, rates applied via
basis points with explicit half-up rounding, plus an AST test that fails the
build on any float in the money path.
**Metric:** n/a (preventive).
