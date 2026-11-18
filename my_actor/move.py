"""Move a whole memory between places: this computer (SQLite) and the cloud (the Apify store).

A move is a copy. Nothing is deleted at the source, and running it twice adds nothing twice:
every copied memory remembers the id it had where it came from.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Any

from .brain import BrainStore
from .constants import BACKEND_APIFY, MEMORY_PREFIX

SOURCE_NAME = 'braintwin'  # the "plugin" name our own export file is imported under
CHUNK = 200  # one import call never writes more than this
_KEEP = ('id', 'title', 'content', 'type', 'tags', 'citations', 'confidence', 'createdAt', 'updatedAt')


def _portable(memory: dict[str, Any]) -> dict[str, Any]:
    """One exported memory, reduced to what the importer reads. Bookkeeping tags stay behind."""
    out = {k: memory.get(k) for k in _KEEP if memory.get(k) is not None}
    origin = (memory.get('externalIds') or {}).get(SOURCE_NAME)
    if origin:  # it was copied here earlier: keep its first id so a trip back is seen as the same memory
        out['id'] = origin
    out['tags'] = [t for t in (memory.get('tags') or []) if not str(t).startswith('plugin:')]
    return out


async def backup_json(brain: BrainStore, statuses: list[str], totals: dict):
    """Enumerate canonical records, including ones absent from a damaged derived index."""
    keys = await brain.kv.list_keys(MEMORY_PREFIX)
    stamp = datetime.now(UTC).isoformat()
    yield '{"ok":true,"exportedAt":' + json.dumps(stamp) + ',"memories":['
    count = 0
    for start in range(0, len(keys), 20):
        rows = await asyncio.gather(*(brain.kv.get_value(k) for k in keys[start:start + 20]))
        for row in rows:
            if isinstance(row, dict) and row.get('status') in statuses:
                yield (',' if count else '') + json.dumps(row, ensure_ascii=False)
                count += 1
    totals['count'] = count
    yield '],"count":' + str(count) + ',"truncated":false}'


async def export_to_file(brain: BrainStore, path: Path, *, everything: bool = False) -> dict[str, Any]:
    statuses = ['approved', 'proposed', 'rejected', 'decayed'] if everything else ['approved']
    temporary = None
    totals = {}
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            async for chunk in backup_json(brain, statuses, totals):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        return {'ok': False, 'error': 'FILE_ERROR', 'message': f'Cannot write {path}: {exc}'}
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {'ok': True, 'file': str(path), 'count': totals['count'], 'truncated': False}


async def copy_memories(source: BrainStore, target: BrainStore, *, dry_run: bool = False) -> dict[str, Any]:
    """Copy every kept memory from one store to another. Safe to repeat."""
    data = await source.export_memories(['approved'])
    there = (await target.export_memories(['approved', 'proposed', 'rejected', 'decayed'])).get('memories') or []
    target_ids = {str(m.get('id')) for m in there}
    known = {str((m.get('externalIds') or {}).get(SOURCE_NAME) or '') for m in there}
    memories = []
    already = 0
    for m in data.get('memories') or []:
        item = _portable(m)
        if item.get('id') in target_ids or item.get('id') in known:  # it started there, or was copied before
            already += 1
            continue
        memories.append(item)
    copied = 0
    errors: list[dict[str, Any]] = []
    for start in range(0, len(memories), CHUNK):
        res = await target.import_plugin_export(
            {'memories': memories[start : start + CHUNK]},
            source_plugin=SOURCE_NAME,
            approve=True,  # the owner already said yes to these where they came from
            dry_run=dry_run,
            limit=CHUNK,
        )
        if not res.get('ok'):
            return {'ok': False, 'error': res.get('error'), 'message': res.get('message'), 'copied': copied}
        fresh = sum(1 for i in res.get('items') or [] if not i.get('duplicate'))
        copied += fresh
        already += len(res.get('items') or []) - fresh
        errors.extend(res.get('errors') or [])
    return {
        'ok': True,
        'dryRun': dry_run,
        'found': data.get('count', 0),
        'copied': copied,
        'alreadyThere': already,
        'errors': errors[:20],
        'truncated': bool(data.get('truncated')),
    }


async def move_memories(source: str, target: str, *, dry_run: bool = False) -> dict[str, Any]:
    if source == target:
        return {'ok': False, 'error': 'INVALID_INPUT', 'message': '--from and --to must be different places.'}
    if BACKEND_APIFY in (source, target) and not os.environ.get('APIFY_TOKEN'):
        return {'ok': False, 'error': 'NO_TOKEN', 'message': 'The cloud side needs APIFY_TOKEN set to your own Apify token.'}
    src, dst = BrainStore(backend=source), BrainStore(backend=target)
    await src.init()
    await dst.init()
    return {**(await copy_memories(src, dst, dry_run=dry_run)), 'from': source, 'to': target}
