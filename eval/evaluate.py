"""Metrics harness. Built in P3 -- see tasks/P3.md.

THE ONLY FILE PERMITTED TO READ labels.json. tests/test_no_label_leak.py
enforces that nothing under src/ can reach ground truth.

Must report, at minimum:
  - auto-resolve rate
  - false-match rate  (its own line, prominently -- this is the number that
    costs real money, and volunteering it unprompted is the clearest maturity
    signal in the submission)
  - precision / recall / F1
  - exception list grouped by reason code
  - records/minute, LLM cost per 100 records
"""
