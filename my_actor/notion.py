"""Notion connector: bring the pages you shared with your own Notion integration into memory.

How it works for the owner: create an internal integration at notion.so/my-integrations, share
the pages (or a parent page) with it, and paste its secret into the dashboard. The secret is kept
in the owner's own storage. A sync reads the most recently edited pages the integration can see,
saves each as a page with searchable passages, and replaces the stored copy when a page changed.

Only Notion's official API on its fixed address is called. Nothing is written to Notion.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

API = 'https://api.notion.com/v1'
NOTION_VERSION = '2022-06-28'
KEY_NOTION = 'config.notion'
MAX_PAGES = 40  # new or changed pages taken per check; the next check continues
MAX_SEARCH_ROUNDS = 10  # x100 pages listed per check
MAX_BLOCK_ROUNDS = 3  # x100 blocks per page
MAX_TEXT_CHARS = 20_000
STEP_TIMEOUT_S = 15.0
SYNC_TIMEOUT_S = 120.0
_TEXT_BLOCKS = frozenset(
    {
        'paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item',
        'to_do', 'toggle', 'quote', 'callout', 'code',
    }
)  # fmt: skip

logger = logging.getLogger('apify.second_brain.notion')


def token_problem(token: Any) -> str | None:
    if not isinstance(token, str) or not (20 <= len(token.strip()) <= 300):
        return 'Paste the integration secret from notion.so/my-integrations.'
    if not all(33 <= ord(ch) < 127 for ch in token.strip()):
        return 'That does not look like a Notion secret.'
    return None


async def notion_status(brain: Any) -> dict[str, Any]:
    raw = await brain.kv.get_value(KEY_NOTION)
    raw = raw if isinstance(raw, dict) else {}
    return {'on': bool(raw.get('token')), 'lastSyncAt': raw.get('lastSyncAt'), 'pages': len(raw.get('seen') or {})}


async def set_notion_token(brain: Any, token: str | None) -> dict[str, Any]:
    if token is None:
        await brain.kv.set_value(KEY_NOTION, None)
    else:
        raw = await brain.kv.get_value(KEY_NOTION)
        raw = raw if isinstance(raw, dict) else {}
        await brain.kv.set_value(KEY_NOTION, {**raw, 'token': token.strip()})
    return await notion_status(brain)


def _plain(rich: Any) -> str:
    return ''.join(str(r.get('plain_text') or '') for r in rich if isinstance(r, dict)) if isinstance(rich, list) else ''


def page_title(page: dict[str, Any]) -> str:
    for prop in (page.get('properties') or {}).values():
        if isinstance(prop, dict) and prop.get('type') == 'title':
            title = _plain(prop.get('title')).strip()
            if title:
                return title[:300]
    return 'Untitled Notion page'


def _prop_value(prop: dict[str, Any]) -> str:
    """Readable value of one database column. Unknown or empty kinds give ''."""
    kind = prop.get('type')
    val = prop.get(kind) if isinstance(kind, str) else None
    if val is None or val == '' or val == []:
        return ''
    if kind == 'rich_text':
        return _plain(val).strip()
    if kind in ('url', 'email', 'phone_number'):
        return str(val)
    if kind == 'number':
        return str(val)
    if kind == 'checkbox':
        return 'yes' if val else 'no'
    if kind in ('select', 'status'):
        return str(val.get('name') or '') if isinstance(val, dict) else ''
    if kind == 'multi_select':
        return ', '.join(str(v.get('name') or '') for v in val if isinstance(v, dict))
    if kind == 'date':
        if not isinstance(val, dict):
            return ''
        return ' to '.join(str(x) for x in (val.get('start'), val.get('end')) if x)
    return ''


def properties_text(page: dict[str, Any]) -> str:
    """A database row keeps its facts in columns, not in the page body: turn them into lines."""
    lines: list[str] = []
    for name, prop in (page.get('properties') or {}).items():
        if not isinstance(prop, dict) or prop.get('type') == 'title':
            continue
        value = _prop_value(prop)
        if value:
            lines.append(f'{str(name)[:80]}: {value[:500]}')
    return '\n'.join(lines[:60])


def blocks_text(blocks: list[Any]) -> str:
    lines: list[str] = []
    for block in blocks:
        kind = block.get('type') if isinstance(block, dict) else None
        if kind not in _TEXT_BLOCKS:
            continue
        text = _plain((block.get(kind) or {}).get('rich_text')).strip()
        if text:
            lines.append(text)
    return '\n'.join(lines)


async def _page_text(client: httpx.AsyncClient, headers: dict[str, str], page_id: str) -> str:
    parts: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_BLOCK_ROUNDS):
        params = {'page_size': '100', **({'start_cursor': cursor} if cursor else {})}
        resp = await client.get(f'{API}/blocks/{page_id}/children', headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        parts.append(blocks_text(data.get('results') or []))
        cursor = data.get('next_cursor') if data.get('has_more') else None
        if not cursor or sum(len(p) for p in parts) >= MAX_TEXT_CHARS:
            break
    return '\n'.join(p for p in parts if p)[:MAX_TEXT_CHARS]


async def notion_sync(brain: Any, *, limit: int = MAX_PAGES, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Save new and changed pages. Returns {ok, saved, updated, unchanged, failed, message}."""
    raw = await brain.kv.get_value(KEY_NOTION)
    raw = raw if isinstance(raw, dict) else {}
    token = str(raw.get('token') or '')
    if not token:
        return {'ok': False, 'error': 'NOT_CONNECTED', 'message': 'Connect Notion first.'}
    headers = {'Authorization': f'Bearer {token}', 'Notion-Version': NOTION_VERSION, 'Content-Type': 'application/json'}
    seen: dict[str, Any] = dict(raw.get('seen') or {})
    counts = {'saved': 0, 'updated': 0, 'unchanged': 0, 'failed': 0, 'empty': 0}
    max_pages = max(1, min(int(limit), MAX_PAGES))
    more = False
    own = client is None
    client = client or httpx.AsyncClient(timeout=STEP_TIMEOUT_S, follow_redirects=False)
    try:
        async with asyncio.timeout(SYNC_TIMEOUT_S):
            cursor: str | None = None
            handled = 0
            for _ in range(MAX_SEARCH_ROUNDS):
                body: dict[str, Any] = {
                    'filter': {'property': 'object', 'value': 'page'},
                    'sort': {'direction': 'descending', 'timestamp': 'last_edited_time'},
                    'page_size': 100,
                    **({'start_cursor': cursor} if cursor else {}),
                }
                resp = await client.post(f'{API}/search', headers=headers, json=body)
                if resp.status_code == 401:
                    return {'ok': False, 'error': 'BAD_TOKEN', 'message': 'Notion refused the secret. Paste it again.'}
                resp.raise_for_status()
                listing = resp.json()
                for page in listing.get('results') or []:
                    if not isinstance(page, dict) or page.get('archived') or page.get('in_trash'):
                        continue
                    pid, edited, url = str(page.get('id') or ''), str(page.get('last_edited_time') or ''), str(page.get('url') or '')
                    if not pid or not url.startswith('https://'):
                        continue
                    if (seen.get(pid) or {}).get('edited') == edited:
                        counts['unchanged'] += 1
                        continue
                    if handled >= max_pages:
                        more = True  # the rest is picked up by the next check
                        continue
                    handled += 1
                    try:
                        body_text = await _page_text(client, headers, pid)
                        text = '\n'.join(t for t in (properties_text(page), body_text) if t)[:MAX_TEXT_CHARS]
                        ingest = {'ok': True, 'statusCode': 200, 'url': url, 'title': page_title(page), 'text': text, 'charCount': len(text), 'version': edited, 'kind': 'notion'}
                        res = await brain.source_refresh(url=url, title=ingest['title'], tags=['notion'], ingest=ingest)
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.info('notion page failed: %s', type(exc).__name__)
                        counts['failed'] += 1
                        continue
                    if not res.get('ok'):
                        counts['failed'] += 1
                        continue
                    if res.get('filedNew'):
                        counts['updated' if pid in seen else 'saved'] += 1
                    else:
                        counts['empty'] += 1
                    seen[pid] = {'edited': edited}
                cursor = listing.get('next_cursor') if listing.get('has_more') else None
                if not cursor:
                    break
    except TimeoutError:
        pass  # keep what was done; the next sync continues with the rest
    except httpx.HTTPError as exc:
        return {'ok': False, 'error': 'NOTION_ERROR', 'message': f'Notion could not be reached ({type(exc).__name__}).', **counts}
    finally:
        if own:
            await client.aclose()
        fresh = await brain.kv.get_value(KEY_NOTION)
        if isinstance(fresh, dict) and fresh.get('token'):  # not disconnected meanwhile
            from datetime import UTC, datetime

            await brain.kv.set_value(KEY_NOTION, {**fresh, 'seen': seen, 'lastSyncAt': datetime.now(UTC).isoformat()})
    changed = counts['saved'] + counts['updated']
    message = f"{counts['saved']} new, {counts['updated']} updated, {counts['unchanged']} unchanged"
    if counts['empty']:
        message += f", {counts['empty']} with no text"
    if counts['failed']:
        message += f", {counts['failed']} failed"
    if more:
        message += '. More pages are waiting: check again to bring in the next batch'
    return {'ok': True, **counts, 'changed': changed, 'more': more, 'message': message}
