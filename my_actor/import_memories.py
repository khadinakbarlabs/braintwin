"""Normalize exports from agent memory plugins into CanonMCP propose records.

Callers: `my_actor.brain.BrainStore.import_plugin_export`, `my_actor.main.memory_propose`.
No existing `my_actor/import*.py` (Glob). Pure in-memory JSON normalize — no file I/O.
User: "also the import from any other memory plugin connected to agent" + preferred MCP upgrades.

Supported shapes (auto-detected):
- AgentMemory memory_export: {version, memories:[{id,type,title,content,concepts,files,strength}]}
- Mem0-like: {results|memories:[{id,memory|content|text}]} or bare list
- Basic Memory notes: {notes|documents:[{title|path, content|body|text|markdown}]}
- Generic: {memories|items|records:[...]} or list of {title,content} / single object

Default destination is status=proposed (consent gate).
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_IMPORT = 200

_TYPE_MAP = {
    'bug': 'note',
    'workflow': 'note',
    'fact': 'note',
    'preference': 'preference',
    'architecture': 'note',
    'pattern': 'note',
    'decision': 'decision',
    'note': 'note',
    'source_snapshot': 'source_snapshot',
    'source_chunk': 'source_chunk',
    'question': 'question',
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [p.strip() for p in value.split(',') if p.strip()]
    return [value]


def _clamp_confidence(value: Any, *, default: float = 0.7) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v > 1.0 and v <= 10.0:  # AgentMemory strength 1–10
        v = v / 10.0
    return max(0.0, min(1.0, v))


def _first_str(*values: Any, limit: int = 500) -> str:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()[:limit]
    return ''


def parse_import_payload(raw: Any) -> dict[str, Any]:
    """Accept dict, list, or JSON string; return {ok, payload|error}."""
    if raw is None:
        return {'ok': False, 'error': 'EMPTY', 'message': 'import_json is required'}
    if isinstance(raw, (dict, list)):
        return {'ok': True, 'payload': raw}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {'ok': False, 'error': 'EMPTY', 'message': 'import_json is empty'}
        fence = re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', text, re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        try:
            return {'ok': True, 'payload': json.loads(text)}
        except json.JSONDecodeError as exc:
            return {
                'ok': False,
                'error': 'INVALID_JSON',
                'message': f'import_json is not valid JSON: {exc}',
            }
    return {
        'ok': False,
        'error': 'INVALID_TYPE',
        'message': 'import_json must be a JSON object, array, or string',
    }


def detect_format(payload: Any) -> str:
    if isinstance(payload, list):
        if not payload:
            return 'empty_list'
        first = payload[0] if isinstance(payload[0], dict) else {}
        if 'concepts' in first or 'strength' in first:
            return 'agentmemory'
        if isinstance(first.get('id'), str) and str(first['id']).startswith('mem_'):
            return 'agentmemory'
        if 'memory' in first and isinstance(first.get('memory'), str):
            return 'mem0'
        if any(k in first for k in ('path', 'body', 'markdown')):
            return 'basic_memory'
        return 'generic_list'
    if not isinstance(payload, dict):
        return 'unknown'
    memories = payload.get('memories')
    if isinstance(memories, list) and (
        'version' in payload
        or any(isinstance(m, dict) and ('concepts' in m or 'strength' in m) for m in memories[:3])
    ):
        return 'agentmemory'
    if isinstance(payload.get('results'), list):
        return 'mem0'
    if isinstance(payload.get('notes'), list) or isinstance(payload.get('documents'), list):
        return 'basic_memory'
    if any(isinstance(payload.get(k), list) for k in ('memories', 'items', 'records')):
        return 'generic_bundle'
    if any(k in payload for k in ('content', 'text', 'memory', 'title', 'claim')):
        return 'single'
    return 'unknown'


def _items_from_payload(payload: Any, fmt: str) -> list[dict[str, Any]]:
    if fmt == 'empty_list':
        return []
    if fmt == 'single' and isinstance(payload, dict):
        return [payload]
    if fmt == 'agentmemory':
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        return [x for x in (payload.get('memories') or []) if isinstance(x, dict)]
    if fmt == 'mem0':
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        for key in ('results', 'memories', 'items'):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return []
    if fmt == 'basic_memory':
        for key in ('notes', 'documents', 'items'):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return []
    if fmt == 'generic_bundle' and isinstance(payload, dict):
        for key in ('memories', 'items', 'records'):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return []
    if fmt == 'generic_list' and isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _normalize_one(item: dict[str, Any], *, source_plugin: str) -> dict[str, Any] | None:
    external_id = _first_str(
        item.get('id'),
        item.get('memory_id'),
        item.get('memoryId'),
        item.get('uuid'),
        limit=120,
    )
    content = _first_str(
        item.get('content'),
        item.get('memory'),
        item.get('text'),
        item.get('body'),
        item.get('markdown'),
        item.get('claim'),
        limit=50_000,
    )
    title = _first_str(
        item.get('title'),
        item.get('name'),
        item.get('path'),
        item.get('subject'),
        limit=500,
    )
    if not content and not title:
        return None
    if not title:
        title = (content[:80] + '…') if len(content) > 80 else content
    if not content:
        content = title

    raw_type = _first_str(item.get('type'), item.get('memory_type'), item.get('kind')) or 'note'
    memory_type = _TYPE_MAP.get(raw_type.lower(), 'note')

    tags: list[str] = []
    for t in _as_list(item.get('tags')) + _as_list(item.get('concepts')):
        s = str(t).strip()
        if s and s not in tags:
            tags.append(s[:80])
    if raw_type and raw_type.lower() not in {t.lower() for t in tags}:
        tags.append(raw_type.lower()[:80])
    plugin_tag = f'import:{source_plugin}'
    if plugin_tag not in tags:
        tags.append(plugin_tag)

    citations: list[dict[str, Any]] = []
    for f in _as_list(item.get('files'))[:30]:
        path = str(f).strip()
        if path:
            citations.append({'type': 'file', 'path': path[:500], 'sourcePlugin': source_plugin})
    for cite in _as_list(item.get('citations'))[:30]:
        if isinstance(cite, str) and cite.strip():
            citations.append({'url': cite.strip()[:1000], 'sourcePlugin': source_plugin})
        elif isinstance(cite, dict):
            citations.append({**cite, 'sourcePlugin': source_plugin})

    confidence = _clamp_confidence(
        item.get('confidence', item.get('strength', item.get('score', 0.7)))
    )

    dedupe = f'ext:{source_plugin}:{external_id}' if external_id else None
    provenance = {
        'sourcePlugin': source_plugin,
        'externalId': external_id or None,
        'importedAt': None,  # filled by brain
        'originalType': raw_type,
        'originalCreatedAt': item.get('createdAt') or item.get('created_at'),
        'originalUpdatedAt': item.get('updatedAt') or item.get('updated_at'),
        'sessionIds': _as_list(item.get('sessionIds') or item.get('session_ids'))[:20],
    }

    return {
        'title': title,
        'content': content,
        'memory_type': memory_type,
        'tags': tags[:40],
        'citations': citations,
        'confidence': confidence,
        'dedupe_key': dedupe,
        'external_ids': {source_plugin: external_id} if external_id else {},
        'provenance': provenance,
    }


def normalize_plugin_export(
    raw: Any,
    *,
    source_plugin: str | None = None,
    limit: int = MAX_IMPORT,
    hard_cap: int = MAX_IMPORT,
) -> dict[str, Any]:
    """Return {ok, format, sourcePlugin, records[], skipped, truncated}.

    `hard_cap` is the absolute ceiling for `limit`. Imports keep the default (200); sync
    raises it so it can sort by time *before* applying its own per-run write cap.
    """
    parsed = parse_import_payload(raw)
    if not parsed.get('ok'):
        return parsed

    payload = parsed['payload']
    fmt = detect_format(payload)
    if fmt == 'unknown':
        return {
            'ok': False,
            'error': 'UNSUPPORTED_FORMAT',
            'message': (
                'Unrecognized memory export. Pass AgentMemory memory_export JSON, '
                'Mem0 results, Basic Memory notes, or a list of {title,content}.'
            ),
            'format': fmt,
        }
    if fmt == 'empty_list':
        return {
            'ok': False,
            'error': 'EMPTY',
            'message': 'Import payload has zero memories',
            'format': fmt,
        }

    plugin = (source_plugin or '').strip() or (
        'agentmemory'
        if fmt == 'agentmemory'
        else 'mem0'
        if fmt == 'mem0'
        else 'basic_memory'
        if fmt == 'basic_memory'
        else 'generic'
    )

    items = _items_from_payload(payload, fmt)
    records: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        # AgentMemory keeps old revisions of a memory with isLatest=false; only the
        # latest one is the memory the user actually holds today.
        if item.get('isLatest') is False:
            skipped += 1
            continue
        norm = _normalize_one(item, source_plugin=plugin)
        if not norm:
            skipped += 1
            continue
        records.append(norm)

    truncated = False
    cap = max(1, min(int(limit or hard_cap), hard_cap))
    if len(records) > cap:
        records = records[:cap]
        truncated = True

    if not records:
        return {
            'ok': False,
            'error': 'NO_RECORDS',
            'message': 'No importable memory records found in payload',
            'format': fmt,
            'sourcePlugin': plugin,
            'skipped': skipped,
        }

    return {
        'ok': True,
        'format': fmt,
        'sourcePlugin': plugin,
        'count': len(records),
        'skipped': skipped,
        'truncated': truncated,
        'maxImport': MAX_IMPORT,
        'records': records,
        'exportVersion': payload.get('version') if isinstance(payload, dict) else None,
    }


# ---------------------------------------------------------------- pasted plain text
_BULLET_RE = re.compile(r'^\s{0,8}(?:[-*•–—]|\d{1,3}[.)])\s+')
MAX_TEXT_ITEMS = 200
MIN_TEXT_ITEM_CHARS = 8
MAX_TEXT_CHARS = 200_000


def text_to_records(text: str) -> list[dict[str, Any]]:
    """Turn pasted plain text into one record per memory.

    Covers what people can copy out of tools that have no export file: the "saved memories" list
    of a chat assistant, a memory summary, a bullet list of notes. Rules, in plain words:
      * a bullet or numbered line is one memory;
      * otherwise, with blank lines in the text, each paragraph is one memory;
      * otherwise each line is one memory.
    Headings (`# ...`) are skipped. Nothing here calls an AI.
    """
    text = (text or '')[:MAX_TEXT_CHARS].replace('\r\n', '\n').replace('\r', '\n')
    lines = [ln.rstrip() for ln in text.split('\n')]
    lines = [ln for ln in lines if not ln.lstrip().startswith('#')]
    has_bullets = sum(1 for ln in lines if _BULLET_RE.match(ln)) >= 2
    has_blank = any(not ln.strip() for ln in lines) and any(ln.strip() for ln in lines)
    chunks: list[str] = []
    if has_bullets:
        current = ''
        for ln in lines:
            if _BULLET_RE.match(ln):
                if current:
                    chunks.append(current)
                current = _BULLET_RE.sub('', ln, count=1).strip()
            elif ln.strip() and current:
                current += ' ' + ln.strip()  # wrapped continuation of the bullet above
            elif ln.strip():
                chunks.append(ln.strip())
        if current:
            chunks.append(current)
    elif has_blank:
        block: list[str] = []
        for ln in [*lines, '']:
            if ln.strip():
                block.append(ln.strip())
            elif block:
                chunks.append(' '.join(block))
                block = []
    else:
        chunks = [ln.strip() for ln in lines if ln.strip()]
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        body = re.sub(r'\s+', ' ', chunk).strip()
        if len(body) < MIN_TEXT_ITEM_CHARS or body.lower() in seen:
            continue
        seen.add(body.lower())
        title = body if len(body) <= 80 else body[:77].rsplit(' ', 1)[0] + '…'
        records.append({'title': title, 'content': body[:20_000]})
        if len(records) >= MAX_TEXT_ITEMS:
            break
    return records
