"""Guard: the pipeline never sees ground truth.

labels.json is generated alongside each batch and read only by eval/. If any
module under src/ can reach it, every metric in the submission is invalid --
and the invalidity is invisible, because the numbers get better, not worse.

Expect the panel to ask how you know the pipeline isn't peeking. This test is
the answer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

FORBIDDEN_TOKENS = ("labels.json", "labels_path", "ground_truth", "injected_discrepancies")

# The generator writes labels; it is the one place the token may legitimately
# appear. Everything else in src/ is consuming the pipeline, not creating it.
WRITER_ALLOWLIST = {"src/milaan/generate/labels.py"}


def src_files() -> list[Path]:
    if not SRC.exists():
        return []
    return sorted(SRC.rglob("*.py"))


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


@pytest.mark.parametrize("path", src_files(), ids=lambda p: str(p))
def test_no_label_references(path: Path):
    if _relative(path) in WRITER_ALLOWLIST:
        pytest.skip("generator is permitted to write labels")
    source = path.read_text(encoding="utf-8")
    hits = [token for token in FORBIDDEN_TOKENS if token in source]
    if hits:
        pytest.fail(
            f"{_relative(path)} references ground truth: {', '.join(hits)}.\n"
            "Nothing in the resolution path may read labels.json. If you need "
            "this signal to resolve a cluster, the cluster belongs in the "
            "exception queue instead."
        )


@pytest.mark.parametrize("path", src_files(), ids=lambda p: str(p))
def test_no_eval_imports_in_src(path: Path):
    """src/ must not import eval/ -- that is the other direction of the same leak."""
    if _relative(path) in WRITER_ALLOWLIST:
        pytest.skip("generator is permitted to write labels")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("eval"), f"{_relative(path)} imports eval"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("eval"), f"{_relative(path)} imports from eval"
