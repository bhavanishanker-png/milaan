"""In-memory precedent store for T3 RAG.

At resolution time, each resolved cluster is added to the store with:
  - cluster fingerprint  (short string: exc_type + key identifiers)
  - resolution class
  - rationale

When a new cluster arrives, the k nearest precedents are retrieved and
injected into the system prompt to bootstrap the agent's reasoning.

This avoids the cold-start problem: the second D10 cluster is informed by
the first, which is typical in real reconciliation (same PSP, same holiday
pattern, same anomaly class repeats many times over a month).

Implementation: cosine similarity over simple bigram TF-IDF vectors.
ChromaDB is the dependency but we keep the interface minimal so swapping
to a remote store is one-line.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from math import log, sqrt
from pathlib import Path


@dataclass
class PrecedentRecord:
    fingerprint: str
    exc_type: str
    action: str
    reason_code: str
    rationale: str
    confidence: float


@dataclass
class PrecedentStore:
    """Simple in-memory TF-IDF precedent store."""

    _records: list[PrecedentRecord] = field(default_factory=list)

    # ---------- public API ----------

    def add(self, record: PrecedentRecord) -> None:
        self._records.append(record)

    def search(self, query_fingerprint: str, k: int = 5) -> list[PrecedentRecord]:
        """Return the k most similar precedents by TF-IDF cosine similarity."""
        if not self._records:
            return []
        query_vec = self._tfidf(query_fingerprint)
        scored = [
            (self._cosine(query_vec, self._tfidf(r.fingerprint)), r)
            for r in self._records
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:k] if _ > 0]

    def format_context(self, records: list[PrecedentRecord]) -> str:
        """Format precedents as a system-prompt block."""
        if not records:
            return "No matching precedents found."
        lines = ["Relevant past resolutions:"]
        for i, r in enumerate(records, 1):
            lines.append(
                f"  [{i}] {r.exc_type} → {r.action} ({r.reason_code}, "
                f"confidence={r.confidence:.2f}): {r.rationale[:120]}"
            )
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._records)

    # ---------- private helpers ----------

    @staticmethod
    def _tokenise(text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9_]+", text.lower())
        bigrams = [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        return tokens + bigrams

    def _tfidf(self, text: str) -> dict[str, float]:
        tokens = self._tokenise(text)
        tf = Counter(tokens)
        n_docs = max(1, len(self._records))
        vec: dict[str, float] = {}
        for term, count in tf.items():
            df = sum(1 for r in self._records if term in self._tokenise(r.fingerprint)) + 1
            idf = log(n_docs / df) + 1
            vec[term] = count * idf
        return vec

    @staticmethod
    def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
        dot = sum(a.get(k, 0) * v for k, v in b.items())
        norm_a = sqrt(sum(v * v for v in a.values()))
        norm_b = sqrt(sum(v * v for v in b.values()))
        denom = norm_a * norm_b
        return dot / denom if denom > 0 else 0


# Module-level singleton shared across the pipeline run.
_STORE = PrecedentStore()


def get_store() -> PrecedentStore:
    return _STORE


def reset_store() -> None:
    """Call between pipeline runs to avoid cross-contamination in tests."""
    global _STORE
    _STORE = PrecedentStore()
