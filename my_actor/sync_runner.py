"""Incremental sync of another agent-memory plugin into the brain (AgentMemory first).

Callers: my_actor/tools.py (memory_propose with sync=true), my_actor/cli.py (sync).

Unlike a one-shot import, running this repeatedly is safe and cheap:
- A per-plugin watermark skips records older than the last run.
- A content hash decides what really changed, so re-running never rewrites or duplicates.
- Nothing already approved is ever silently overwritten: a changed record that is approved
  becomes a *new proposal* that points at the old one (`supersedes`) for the user to review.
- Memories the user already rejected or forgot stay that way.

Data shape in (AgentMemory memory_export item, synthetic):
{"id": "mem_1", "type": "fact", "title": "T", "content": "C", "concepts": ["x"],
 "files": [], "createdAt": "2026-11-01T10:00:00.000Z", "updatedAt": "2026-11-02T10:00:00.000Z",
 "strength": 7, "version": 1, "isLatest": true}
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, UTC
from typing import Any

from .constants import MAX_SYNC_WRITES
from .import_memories import normalize_plugin_export
from .sync_ops import advance_watermark, content_hash, select_for_sync

_MAX_EXPORT = 100_000
_DETAIL_CAP = 50
_PENDING = ('proposed', 'working', 'approved')


def _merge_tags(new: list[str], extra: list[str], prior: list[str] | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in [*new, *extra, *(prior or [])]:
        low = str(tag).lower()
        if tag and low not in seen:
            seen.add(low)
            out.append(str(tag))
    return out[:40]


async def _sync_one(
    brain: Any,
    rec: dict[str, Any],
    *,
    approve: bool,
    dry_run: bool,
    extra_tags: list[str],
    agent: str | None,
) -> tuple[str, str | None]:
    """Return (outcome, memory_id). Outcomes: created updated superseded unchanged skippedDecided error."""
    key = rec.get('dedupe_key')
    new_hash = content_hash(rec['title'], rec['content'])
    provenance = {**(rec.get('provenance') or {}), 'contentHash': new_hash}
    if agent:
        provenance['agent'] = agent
    base = {
        'title': rec['title'],
        'content': rec['content'],
        'memory_type': rec.get('memory_type') or 'note',
        'citations': rec.get('citations'),
        'confidence': rec.get('confidence', 0.7),
        'external_ids': rec.get('external_ids'),
        'dry_run': dry_run,
    }
    tags = _merge_tags(rec.get('tags') or [], extra_tags)

    existing_id = await brain.find_by_dedupe(key) if key else None
    existing = await brain.get_memory(existing_id) if existing_id else None

    if existing is None:
        res = await brain.memory_save(**base, tags=tags, approve=approve, dedupe_key=key, provenance=provenance)
        if not res.get('ok'):
            return 'error', None
        if res.get('duplicate'):
            return 'unchanged', (res.get('memory') or {}).get('id')
        return 'created', (res.get('memory') or {}).get('id')

    if content_hash(existing.get('title', ''), existing.get('content', '')) == new_hash:
        return 'unchanged', existing_id

    status = existing.get('status')
    if status in ('proposed', 'working'):
        res = await brain.memory_save(
            **base,
            tags=_merge_tags(rec.get('tags') or [], extra_tags, existing.get('tags')),
            approve=False,
            dedupe_key=key,
            memory_id=existing_id,
            provenance=provenance,
        )
        return ('updated', existing_id) if res.get('ok') else ('error', existing_id)

    if status == 'approved':
        rev_key = f'{key}:rev:{new_hash[:8]}'
        rev_id = await brain.find_by_dedupe(rev_key)
        rev = await brain.get_memory(rev_id) if rev_id else None
        if rev is not None:
            return ('unchanged' if rev.get('status') in _PENDING else 'skippedDecided'), rev_id
        res = await brain.memory_save(
            **base,
            tags=_merge_tags(rec.get('tags') or [], [*extra_tags, 'supersedes']),
            approve=False,  # an approved memory is never replaced without the user's say-so
            dedupe_key=rev_key,
            provenance={**provenance, 'supersedes': existing_id},
        )
        return ('superseded', (res.get('memory') or {}).get('id')) if res.get('ok') else ('error', None)

    return 'skippedDecided', existing_id  # decayed / rejected: the user already decided


async def sync_plugin_export(
    brain: Any,
    raw: Any,
    *,
    source_plugin: str | None = None,
    approve: bool = False,
    dry_run: bool = False,
    limit: int = MAX_SYNC_WRITES,
    use_watermark: bool = True,
    agent: str | None = None,
    extra_tags: list[str] | None = None,
) -> dict[str, Any]:
    normalized = normalize_plugin_export(
        raw, source_plugin=source_plugin, limit=_MAX_EXPORT, hard_cap=_MAX_EXPORT
    )
    if not normalized.get('ok'):
        return normalized

    plugin = normalized['sourcePlugin']
    state = (await brain.get_sync_state()).get(plugin) or {}
    watermark_before = state.get('watermark') if use_watermark else None
    ordered, skipped_older = select_for_sync(normalized['records'], watermark_before)

    write_cap = max(1, min(int(limit or MAX_SYNC_WRITES), MAX_SYNC_WRITES))
    counts: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    writes = 0
    last_examined: dict[str, Any] | None = None
    more = False
    tags = list(extra_tags or [])

    if not dry_run:
        await brain.begin_batch()
    try:
        for rec in ordered:
            if writes >= write_cap:
                more = True
                break
            outcome, memory_id = await _sync_one(
                brain, rec, approve=approve, dry_run=dry_run, extra_tags=tags, agent=agent
            )
            counts[outcome] += 1
            if outcome in ('created', 'updated', 'superseded'):
                writes += 1
            if outcome != 'unchanged' and len(details) < _DETAIL_CAP:
                details.append({'outcome': outcome, 'id': memory_id, 'title': rec['title'][:120]})
            last_examined = rec
    finally:
        if not dry_run:
            await brain.end_batch()

    # A failed record must be retried next run, so never move the watermark past an error.
    watermark_after = (
        watermark_before if counts['error'] or dry_run else advance_watermark(watermark_before, last_examined)
    )
    if not dry_run:
        await brain.set_sync_state(
            plugin,
            {
                'watermark': watermark_after,
                'lastSyncAt': datetime.now(UTC).isoformat(),
                'lastCounts': dict(counts),
                'moreRemaining': more,
            },
        )
        await brain.audit('sync', detail=f'plugin={plugin} {dict(counts)} more={more}'[:1000])

    parts = [
        f"{counts['created']} new",
        f"{counts['updated']} updated",
        f"{counts['superseded']} changed-after-approval (review in inbox)",
        f"{counts['unchanged']} unchanged",
    ]
    if counts['skippedDecided']:
        parts.append(f"{counts['skippedDecided']} left alone (you rejected/forgot them)")
    if counts['error']:
        parts.append(f"{counts['error']} errors")
    return {
        'ok': True,
        'dryRun': dry_run,
        'approve': approve,
        'plugin': plugin,
        'format': normalized.get('format'),
        'watermarkBefore': watermark_before,
        'watermarkAfter': watermark_after,
        'examined': sum(counts.values()),
        'skippedOlder': skipped_older,
        'skippedAtSource': normalized.get('skipped', 0),
        'counts': {k: counts[k] for k in ('created', 'updated', 'superseded', 'unchanged', 'skippedDecided', 'error')},
        'more': more,
        'items': details,
        'message': (
            f"{'Would sync' if dry_run else 'Synced'} {plugin}: {', '.join(parts)}."
            + (' More remain — run again to continue.' if more else '')
        ),
    }
