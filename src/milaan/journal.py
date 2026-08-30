"""Proposed double-entry accounting journal entries.

Each auto-applied reconciliation match produces two journal entries:
  DR  Accounts Receivable (settlement due)
  CR  Bank (actual credit received)

If net = bank credit (no discrepancy) the entries zero out.
If there is a residual (fee rounding, timing) an additional entry is posted
to the appropriate adjustment account.

All amounts are integer paise.  No floats touch this module.

Journal entries are proposed only, not booked.  A human (or a future
booking service) reads the journal and confirms.  The audit log records
the proposal; the booking service records the confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JournalLine:
    """One side of a double-entry line."""

    account:     str    # e.g. "settlement_receivable", "bank", "fee_rounding_adj"
    debit_paise: int    # 0 if credit side
    credit_paise: int   # 0 if debit side
    ref:         str    # bank row index or settlement_id
    narration:   str


@dataclass(frozen=True)
class JournalEntry:
    """A balanced pair of lines (debits = credits)."""

    settlement_id: str
    bank_row:      int | None
    tier:          str
    reason_code:   str
    lines:         tuple[JournalLine, ...]

    def is_balanced(self) -> bool:
        total_dr = sum(l.debit_paise  for l in self.lines)
        total_cr = sum(l.credit_paise for l in self.lines)
        return total_dr == total_cr


def propose(
    settlement_id: str,
    bank_row_index: int | None,
    bank_deposit_paise: int,
    settlement_net_paise: int,
    tier: str,
    reason_code: str,
) -> JournalEntry:
    """Produce a proposed journal entry for an auto-applied match.

    If the bank deposit equals the settlement net, no adjustment is needed.
    If there is a residual (positive or negative), an adjustment line is added
    to a fee-rounding or timing-lag account so the entry stays balanced.
    """
    ref_bank = str(bank_row_index) if bank_row_index is not None else "N/A"
    residual = bank_deposit_paise - settlement_net_paise

    lines: list[JournalLine] = [
        JournalLine(
            account="settlement_receivable",
            debit_paise=0,
            credit_paise=settlement_net_paise,
            ref=settlement_id,
            narration=f"Settlement {settlement_id} — {reason_code}",
        ),
        JournalLine(
            account="bank",
            debit_paise=bank_deposit_paise,
            credit_paise=0,
            ref=ref_bank,
            narration=f"Bank row {ref_bank} — {reason_code}",
        ),
    ]

    if residual != 0:
        if residual > 0:
            # Bank credited more than settlement net → extra income (e.g. reversal)
            lines.append(JournalLine(
                account="reconciliation_adjustment",
                debit_paise=0,
                credit_paise=residual,
                ref=settlement_id,
                narration=f"Adjustment ({residual} paise over) — {reason_code}",
            ))
        else:
            # Bank credited less → shortfall expense
            lines.append(JournalLine(
                account="reconciliation_adjustment",
                debit_paise=-residual,
                credit_paise=0,
                ref=settlement_id,
                narration=f"Adjustment ({-residual} paise short) — {reason_code}",
            ))

    entry = JournalEntry(
        settlement_id=settlement_id,
        bank_row=bank_row_index,
        tier=tier,
        reason_code=reason_code,
        lines=tuple(lines),
    )
    assert entry.is_balanced(), (
        f"Journal entry for {settlement_id} is unbalanced: "
        f"DR={sum(l.debit_paise for l in lines)} CR={sum(l.credit_paise for l in lines)}"
    )
    return entry
