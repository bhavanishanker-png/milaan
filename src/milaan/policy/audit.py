"""Append-only audit log.

Every decision in the pipeline — T1 match, T2 match, T3 resolution, gate
result, exception — gets one entry.  The log is a newline-delimited JSON
file so it can be streamed, grepped, and replayed without loading everything
into memory.

The log reconstructs the full decision path for any bank row.  The panel can
trace from raw CSV line to final journal entry in one grep.

Hash chaining: each entry includes the SHA-256 of the previous entry, so
any tampering is detectable.  The first entry uses a zero hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

_ZERO_HASH = "0" * 64
_UTC = timezone.utc


class AuditLog:
    """Append-only, hash-chained audit log backed by a NDJSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._prev_hash = self._read_last_hash()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        actor: str,           # "tier1" | "tier2" | "tier3" | "gate" | "human"
        event: str,           # "MATCH" | "EXCEPTION" | "GATE_PASS" | "GATE_BLOCK" | ...
        bank_row:  int | None = None,
        settlement_id: str | None = None,
        tier: str | None = None,
        confidence: float | None = None,
        reason_code: str | None = None,
        rationale: str | None = None,
        detail: dict | None = None,
    ) -> dict:
        """Append one entry to the log and return it."""
        entry: dict = {
            "ts":            datetime.now(tz=_UTC).isoformat(),
            "actor":         actor,
            "event":         event,
            "bank_row":      bank_row,
            "settlement_id": settlement_id,
            "tier":          tier,
            "confidence":    confidence,
            "reason_code":   reason_code,
            "rationale":     rationale,
            "prev_hash":     self._prev_hash,
        }
        if detail:
            entry["detail"] = detail

        line = json.dumps(entry, separators=(",", ":"))
        self._prev_hash = hashlib.sha256(line.encode()).hexdigest()
        entry["hash"] = self._prev_hash

        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")

        return entry

    def query(self, *, bank_row: int | None = None, settlement_id: str | None = None) -> list[dict]:
        """Return all entries matching the given filter (AND of non-None args)."""
        if not self._path.exists():
            return []
        results = []
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if bank_row is not None and entry.get("bank_row") != bank_row:
                    continue
                if settlement_id is not None and entry.get("settlement_id") != settlement_id:
                    continue
                results.append(entry)
        return results

    def verify_chain(self) -> tuple[bool, str]:
        """Verify the hash chain is unbroken.  Returns (ok, message)."""
        if not self._path.exists():
            return True, "empty log"
        prev = _ZERO_HASH
        with self._path.open(encoding="utf-8") as fh:
            for i, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    return False, f"line {i}: JSON parse error"

                stored_hash = entry.pop("hash", None)
                expected_prev = entry.get("prev_hash")
                if expected_prev != prev:
                    return False, f"line {i}: prev_hash mismatch"

                computed = hashlib.sha256(
                    json.dumps(entry, separators=(",", ":")).encode()
                ).hexdigest()
                if stored_hash and stored_hash != computed:
                    return False, f"line {i}: hash mismatch (tampered?)"

                prev = stored_hash or prev
        return True, "ok"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_last_hash(self) -> str:
        if not self._path.exists() or self._path.stat().st_size == 0:
            return _ZERO_HASH
        with self._path.open("rb") as fh:
            # Walk backwards to find the last non-empty line.
            fh.seek(0, 2)
            pos = fh.tell()
            buf = b""
            while pos > 0:
                step = min(256, pos)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step)
                buf = chunk + buf
                lines = buf.splitlines()
                if len(lines) >= 2 or pos == 0:
                    break
            for line in reversed(buf.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    return entry.get("hash", _ZERO_HASH)
                except json.JSONDecodeError:
                    continue
        return _ZERO_HASH
