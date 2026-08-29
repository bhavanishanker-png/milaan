# Task briefs

One file per phase. Paste the contents into Claude Code as the task for that
phase, or point it at the file: `read tasks/P1.md and implement it`.

Each brief ends with an exit gate. Do not move to the next phase until it
passes. Gates exist because this project's failure mode is a sophisticated
agent sitting on top of a pipeline nobody measured.

| Phase | File | Days | Gate |
|---|---|---|---|
| P0 | P0.md | 1 (am) | float guard fails on a planted float |
| P1 | P1.md | 1-2 | same seed reproduces byte-identically; 3 discrepancies hand-traced |
| P2 | P2.md | 3 (am) | 100% parse; UTR stats match injected counts |
| P3 | P3.md | 3-4 | baseline + false-match rate printed; label-leak test green |
| P4 | P4.md | 5-7 | +10-15 pts auto-resolve, false-match did NOT rise |
| P5 | P5.md | 8-10 | agent's marginal contribution quantified |
| P6 | P6.md | 11 | threshold curve plotted, operating point justified |
| P7 | P7.md | 12 | ablation + per-class tables complete |
| P8 | P8.md | 13-14 | clean clone runs in 60s; batch C run once |
