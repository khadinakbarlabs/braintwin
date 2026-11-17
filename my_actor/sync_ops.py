"""Pure helpers for incremental plugin sync (watermark + content-hash change detection).

Callers: my_actor/sync_runner.py, tests/test_sync_ops.py. No I/O.

Design notes
- Timestamps are parsed into aware datetimes. Comparing ISO strings breaks when one side
  ends in 'Z' and the other in '+00:00', so never compare them as text.
- Records without a timestamp are never dropped by the watermark: they are re-examined
  every run and settled by content hash (cheap, idempotent).
- The watermark is a pure optimisation. Correctness comes from the content hash, so a
  record sitting exactly on the watermark is re-checked and found unchanged.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, UTC
from typing import Any

_FAR_PAST = datetime.min.replace(tzinfo=UTC)


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 string (Z or offset) or epoch seconds/milliseconds → aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(ts: datetime | None) -> str | None:
    return ts.astimezone(UTC).isoformat() if ts else None


def record_timestamp(rec: dict[str, Any]) -> datetime | None:
    """Best-effort 'last changed' time of a raw or normalized record."""
    for key in ('updatedAt', 'updated_at', 'createdAt', 'created_at', 'timestamp'):
        ts = parse_ts(rec.get(key))
        if ts:
            return ts
    prov = rec.get('provenance')
    if isinstance(prov, dict):
        for key in ('originalUpdatedAt', 'originalCreatedAt'):
            ts = parse_ts(prov.get(key))
            if ts:
                return ts
    return None


def content_hash(title: str, content: str) -> str:
    raw = f'{(title or "").strip()}\n{(content or "").strip()}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def select_for_sync(
    records: list[dict[str, Any]],
    watermark: str | None,
) -> tuple[list[dict[str, Any]], int]:
    """Order records oldest-first and drop those strictly older than the watermark.

    Returns (kept, skipped_older). Timestamp-less records sort first and are always kept.
    """
    wm = parse_ts(watermark)
    kept: list[tuple[datetime, int, dict[str, Any]]] = []
    skipped = 0
    for idx, rec in enumerate(records):
        ts = record_timestamp(rec)
        if ts is not None and wm is not None and ts < wm:
            skipped += 1
            continue
        kept.append((ts or _FAR_PAST, idx, rec))
    kept.sort(key=lambda t: (t[0], t[1]))
    return [rec for _, _, rec in kept], skipped


def advance_watermark(previous: str | None, last_examined: dict[str, Any] | None) -> str | None:
    """Move the watermark forward to the last examined record's time; never backwards."""
    prev = parse_ts(previous)
    ts = record_timestamp(last_examined) if last_examined else None
    if ts is None:
        return iso(prev)
    if prev is not None and prev > ts:
        return iso(prev)
    return iso(ts)
