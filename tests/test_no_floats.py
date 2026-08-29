"""Guard: no floats in the money path.

Confidence scores are legitimately floats and live in the agent and policy
tiers, so this scan is scoped to the modules that touch money directly.

Verify this guard actually works before trusting it -- test_guard_detects_a_float
plants a float and asserts the checker catches it. A guard you have never seen
fail is a guard you do not know works.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules where money is constructed or arithmetic is performed.
MONEY_PATH = [
    "src/milaan/normalize",
    "src/milaan/generate",
    "src/milaan/match",
    "src/milaan/journal.py",
]


class FloatFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            self.hits.append((node.lineno, f"float literal {node.value!r}"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "float":
            self.hits.append((node.lineno, "call to float()"))
        self.generic_visit(node)


def scan_source(source: str) -> list[tuple[int, str]]:
    finder = FloatFinder()
    finder.visit(ast.parse(source))
    return finder.hits


def money_path_files() -> list[Path]:
    files: list[Path] = []
    for entry in MONEY_PATH:
        target = REPO_ROOT / entry
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    return files


def test_guard_detects_a_float():
    """The guard must catch a planted float, or it is decorative."""
    planted = "from milaan.normalize.money import Paise\nfee = amount * 0.02\n"
    assert scan_source(planted), "FloatFinder failed to catch a planted float"

    planted_call = "value = float(row['amount'])\n"
    assert scan_source(planted_call), "FloatFinder failed to catch float()"


def test_guard_passes_clean_source():
    clean = "from milaan.normalize.money import Paise\nfee = amount.apply_rate_bps(200)\n"
    assert scan_source(clean) == []


@pytest.mark.parametrize("path", money_path_files(), ids=lambda p: str(p))
def test_no_floats_in_money_path(path: Path):
    hits = scan_source(path.read_text(encoding="utf-8"))
    if hits:
        detail = "\n".join(f"  line {line}: {what}" for line, what in hits)
        pytest.fail(
            f"Float found in the money path ({path.relative_to(REPO_ROOT)}):\n{detail}\n\n"
            "Money is integer paise. Use Paise.apply_rate_bps() for rates and "
            "Paise.split_evenly() for division -- both round explicitly."
        )
