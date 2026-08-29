"""Integer-paise money type.

Every monetary value in Milaan is a Paise. There are no floats anywhere in the
money path -- see tests/test_no_floats.py, which enforces this by AST scan.

Why: fee and GST rounding happens per-transaction. Across a 400-row settlement
batch, float drift accumulates until the subset-sum target becomes unreachable
and the solver silently stops matching. Integer arithmetic makes the class of
bug impossible rather than merely unlikely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

_RUPEE_STR = re.compile(r"^-?\d{1,3}(,\d{2,3})*(\.\d{1,2})?$|^-?\d+(\.\d{1,2})?$")


class MoneyError(ValueError):
    """Raised on any invalid monetary construction or operation."""


@dataclass(frozen=True, order=True)
class Paise:
    """An exact monetary amount, stored as a whole number of paise.

    100 paise = 1 rupee. Negative values are permitted (debits, chargebacks).
    """

    value: int

    def __post_init__(self) -> None:
        # bool is a subclass of int; reject it explicitly.
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise MoneyError(
                f"Paise requires an int, got {type(self.value).__name__}. "
                "If you have a float, you have already lost precision -- "
                "construct from a string or Decimal instead."
            )

    # ---------- constructors ----------

    @classmethod
    def zero(cls) -> "Paise":
        return cls(0)

    @classmethod
    def from_rupees(cls, rupees: int | str | Decimal) -> "Paise":
        """Build from rupees. Accepts int, Decimal, or a string like '1,234.56'.

        Floats are rejected: 0.1 + 0.2 is not 0.3 and there is no safe way to
        guess what the caller meant.
        """
        if isinstance(rupees, float):
            raise MoneyError(
                "from_rupees() rejects float. Pass a string ('1234.56') or Decimal."
            )
        if isinstance(rupees, bool):
            raise MoneyError("from_rupees() rejects bool.")
        if isinstance(rupees, int):
            return cls(rupees * 100)
        if isinstance(rupees, str):
            cleaned = rupees.strip().replace(",", "").replace("\u20b9", "").strip()
            if not _RUPEE_STR.match(rupees.strip().replace("\u20b9", "").strip()):
                raise MoneyError(f"Unparseable rupee string: {rupees!r}")
            return cls._from_decimal(Decimal(cleaned))
        if isinstance(rupees, Decimal):
            return cls._from_decimal(rupees)
        raise MoneyError(f"Cannot build Paise from {type(rupees).__name__}")

    @classmethod
    def _from_decimal(cls, d: Decimal) -> "Paise":
        scaled = d * 100
        if scaled != scaled.to_integral_value():
            raise MoneyError(
                f"{d} has sub-paise precision; money must be exact to the paise."
            )
        return cls(int(scaled))

    @classmethod
    def sum(cls, items: Iterable["Paise"]) -> "Paise":
        total = 0
        for item in items:
            if not isinstance(item, Paise):
                raise MoneyError(f"Paise.sum() got a {type(item).__name__}")
            total += item.value
        return cls(total)

    # ---------- arithmetic ----------

    def __add__(self, other: "Paise") -> "Paise":
        if not isinstance(other, Paise):
            raise MoneyError("Can only add Paise to Paise.")
        return Paise(self.value + other.value)

    def __sub__(self, other: "Paise") -> "Paise":
        if not isinstance(other, Paise):
            raise MoneyError("Can only subtract Paise from Paise.")
        return Paise(self.value - other.value)

    def __neg__(self) -> "Paise":
        return Paise(-self.value)

    def __abs__(self) -> "Paise":
        return Paise(abs(self.value))

    def __mul__(self, count: int) -> "Paise":
        """Multiply by a whole count (e.g. quantity). Rates use basis points."""
        if isinstance(count, bool) or not isinstance(count, int):
            raise MoneyError(
                "Paise can only be multiplied by an int. For percentage rates "
                "use apply_rate_bps(), which rounds explicitly."
            )
        return Paise(self.value * count)

    __rmul__ = __mul__

    def __truediv__(self, other):  # pragma: no cover - always raises
        raise MoneyError(
            "Division on Paise is disallowed: it produces a float and hides a "
            "rounding decision. Use apply_rate_bps() or split_evenly()."
        )

    # ---------- rate application ----------

    def apply_rate_bps(self, rate_bps: int) -> "Paise":
        """Apply a rate in basis points, rounding half-up, in pure integers.

        200 bps = 2.00%. Half-up matches how Indian PSPs and banks round fees.
        """
        if isinstance(rate_bps, bool) or not isinstance(rate_bps, int):
            raise MoneyError("rate_bps must be an int (200 bps = 2.00%).")
        n = self.value * rate_bps
        if n >= 0:
            return Paise((n + 5000) // 10000)
        return Paise(-((-n + 5000) // 10000))

    def split_evenly(self, parts: int) -> list["Paise"]:
        """Split into `parts`, distributing the remainder one paisa at a time.

        The parts always sum back to the original -- no paise created or lost.
        """
        if isinstance(parts, bool) or not isinstance(parts, int) or parts <= 0:
            raise MoneyError("parts must be a positive int.")
        base, remainder = divmod(self.value, parts)
        return [Paise(base + (1 if i < remainder else 0)) for i in range(parts)]

    # ---------- predicates ----------

    def is_zero(self) -> bool:
        return self.value == 0

    def within(self, other: "Paise", tolerance: "Paise") -> bool:
        """True if self and other differ by no more than tolerance."""
        if not isinstance(other, Paise) or not isinstance(tolerance, Paise):
            raise MoneyError("within() takes Paise arguments.")
        return abs(self.value - other.value) <= abs(tolerance.value)

    # ---------- display ----------

    def to_rupee_str(self) -> str:
        sign = "-" if self.value < 0 else ""
        whole, part = divmod(abs(self.value), 100)
        return f"{sign}{whole:,}.{part:02d}"

    def __str__(self) -> str:
        return f"\u20b9{self.to_rupee_str()}"

    def __repr__(self) -> str:
        return f"Paise({self.value})"


# Common tolerances, expressed once so they are greppable.
ZERO = Paise(0)
ONE_PAISA = Paise(1)


def rounding_tolerance(row_count: int) -> Paise:
    """Tolerance for a batch total: up to 3 paise of drift per constituent row.

    Each row carries a fee round and a GST round, so worst-case drift grows
    linearly with batch size. Keep this tight -- a loose tolerance is the
    fastest route to false matches.
    """
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise MoneyError("row_count must be a non-negative int.")
    return Paise(row_count * 3)
