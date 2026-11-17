"""Followed content: the second memory layer.

The owner follows YouTube channels, blogs and newsletters. New items arrive as memories
tagged `followed`, so an agent can answer "what did I read/watch lately?" and search it.

Consent model: following a source is the owner's decision (CLI only, never an MCP tool).
`auto_approve` is set per source at follow time; otherwise items land in the inbox as
proposals like any other memory. Feeds are fetched directly over HTTP (public RSS/Atom,
YouTube's public channel feed) — no third-party Actors. Transcripts are an optional PAID
step using the owner's own Actor, capped and CLI-only (see `queue_transcripts`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .brain import allow_reserved
from .feeds import YOUTUBE_FEED, channel_id_from_page, parse_feed, youtube_channel_id

KEY_FOLLOWS = 'config.follows'
MAX_FOLLOWS = 200
MAX_SEEN = 500
FIRST_SYNC_LIMIT = 20
TRANSCRIPT_ACTOR = 'khadinakbar/youtube-ai-transcript-extractor'

FetchText = Callable[[str], Awaitable[dict[str, Any]]]
_ALT_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.IGNORECASE
)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _follow_id(feed_url: str) -> str:
    return 'fol_' + hashlib.sha256(feed_url.encode()).hexdigest()[:12]


async def _load(brain: Any) -> list[dict[str, Any]]:
    data = await brain.kv.get_value(KEY_FOLLOWS) or {'follows': []}
    return list(data.get('follows') or [])


async def _store(brain: Any, follows: list[dict[str, Any]]) -> None:
    await brain.kv.set_value(KEY_FOLLOWS, {'follows': follows, 'updatedAt': _now()})


async def follow_list(brain: Any) -> dict[str, Any]:
    follows = await _load(brain)
    return {
        'ok': True,
        'count': len(follows),
        'follows': [
            {k: f.get(k) for k in ('id', 'kind', 'title', 'feedUrl', 'autoApprove', 'lastSyncAt', 'lastError', 'itemsSeen')}
            for f in follows
        ],
    }


async def _resolve_feed(target: str, fetch_text: FetchText) -> tuple[str | None, str, dict[str, Any] | None]:
    """Return (feed_url, kind, error)."""
    target = (target or '').strip()
    cid = youtube_channel_id(target)
    if cid:
        return YOUTUBE_FEED.format(cid=cid), 'youtube', None
    if re.match(r'^https?://(www\.|m\.)?youtube\.com/@', target, re.IGNORECASE):
        page = await fetch_text(target)
        if not page.get('ok'):
            return None, 'youtube', {'ok': False, 'error': page.get('error'), 'message': page.get('message')}
        cid = channel_id_from_page(page.get('text') or '')
        if not cid:
            return None, 'youtube', {'ok': False, 'error': 'NO_CHANNEL_ID', 'message': 'Could not find the channel id on that page. Use the /channel/UC… URL.'}
        return YOUTUBE_FEED.format(cid=cid), 'youtube', None
    page = await fetch_text(target)
    if not page.get('ok'):
        return None, 'feed', {'ok': False, 'error': page.get('error'), 'message': page.get('message')}
    text = page.get('text') or ''
    if parse_feed(text).get('ok'):
        return str(page.get('url') or target), 'feed', None
    for tag in _ALT_RE.findall(text[:200_000]):
        m = _HREF_RE.search(tag)
        if m:
            from urllib.parse import urljoin

            return urljoin(str(page.get('url') or target), m.group(1)), 'feed', None
    return None, 'feed', {'ok': False, 'error': 'NO_FEED', 'message': 'No RSS/Atom feed found at that address.'}


async def follow_add(
    brain: Any, target: str, fetch_text: FetchText, *, title: str | None = None, auto_approve: bool = False
) -> dict[str, Any]:
    feed_url, kind, error = await _resolve_feed(target, fetch_text)
    if error or not feed_url:
        return error or {'ok': False, 'error': 'NO_FEED', 'message': 'No feed.'}
    probe = await fetch_text(feed_url)
    parsed = parse_feed(probe.get('text') or '') if probe.get('ok') else {'ok': False, 'error': probe.get('error'), 'message': probe.get('message')}
    if not parsed.get('ok'):
        return {'ok': False, 'error': parsed.get('error') or 'NOT_A_FEED', 'message': parsed.get('message')}
    async with brain._sources_lock:  # same lock family as sources; follows are low-traffic
        follows = await _load(brain)
        fid = _follow_id(feed_url)
        existing = next((f for f in follows if f['id'] == fid), None)
        if existing:
            return {'ok': True, 'duplicate': True, 'follow': existing}
        if len(follows) >= MAX_FOLLOWS:
            return {'ok': False, 'error': 'TOO_MANY_FOLLOWS', 'message': f'Follow list is full ({MAX_FOLLOWS}).'}
        follow = {
            'id': fid,
            'kind': kind,
            'title': (title or parsed.get('title') or feed_url)[:200],
            'feedUrl': feed_url,
            'autoApprove': bool(auto_approve),
            'createdAt': _now(),
            'lastSyncAt': None,
            'lastError': None,
            'itemsSeen': 0,
            'seen': [],
            'recentVideos': [],
        }
        follows.append(follow)
        await _store(brain, follows)
    await brain.audit('follow', detail=f'add:{kind}:{feed_url}'[:300])
    return {'ok': True, 'duplicate': False, 'follow': {k: v for k, v in follow.items() if k not in ('seen', 'recentVideos')}}


async def follow_remove(brain: Any, follow_id: str) -> dict[str, Any]:
    async with brain._sources_lock:
        follows = await _load(brain)
        kept = [f for f in follows if f['id'] != follow_id]
        if len(kept) == len(follows):
            return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No follow with id {follow_id}'}
        await _store(brain, kept)
    await brain.audit('follow', detail=f'remove:{follow_id}')
    return {'ok': True, 'removed': follow_id, 'note': 'Memories already saved from it are kept; forget them individually if wanted.'}


async def follow_update(
    brain: Any, follow_id: str, *, auto_approve: bool | None = None, title: str | None = None
) -> dict[str, Any]:
    """Change one source's settings. Free: nothing is remembered or forgotten here."""
    changes: dict[str, Any] = {}
    if auto_approve is not None:
        changes['autoApprove'] = bool(auto_approve)
    if title is not None and title.strip():
        changes['title'] = title.strip()[:200]
    if not changes:
        return {'ok': False, 'error': 'INVALID_INPUT', 'message': 'Nothing to change.'}
    if not any(f['id'] == follow_id for f in await _load(brain)):
        return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No follow with id {follow_id}'}
    await _merge_follow(brain, follow_id, changes)
    await brain.audit('follow', detail=f'update:{follow_id}:{sorted(changes)}'[:300])
    follow = next((f for f in await _load(brain) if f['id'] == follow_id), {})
    return {'ok': True, 'follow': {k: v for k, v in follow.items() if k not in ('seen', 'recentVideos')}}


async def follow_memory_ids(brain: Any, follow_id: str | None = None) -> dict[str, dict[str, list[str]]]:
    """Free read: per source, the ids of what it brought in, split into kept and still waiting."""
    out: dict[str, dict[str, list[str]]] = {}
    for mid, entry in (await brain._current_index_entries()).items():
        if not isinstance(entry, dict) or entry.get('status') not in ('approved', 'proposed'):
            continue
        for tag in entry.get('tags') or []:
            tag = str(tag)
            if tag.startswith('follow:') and (follow_id is None or tag == f'follow:{follow_id}'):
                bucket = out.setdefault(tag[7:], {'approved': [], 'proposed': []})
                bucket[entry['status']].append(str(mid))
    return out


def _item_memory(follow: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    lines = [item['title']]
    meta = ' • '.join(x for x in (item.get('author'), item.get('published')) if x)
    if meta:
        lines.append(meta)
    if item.get('summary'):
        lines += ['', item['summary']]
    lines += ['', f"Source: {item['url']}"]
    key = hashlib.sha256(f"{follow['id']}|{item['id']}".encode()).hexdigest()[:24]
    return {
        'title': item['title'],
        'content': '\n'.join(lines),
        'memory_type': 'followed',
        'tags': ['followed', f"follow:{follow['id']}", follow['kind']],
        'citations': [{'url': item['url']}],
        'confidence': 0.6,
        'dedupe_key': f'follow:{key}',
        'provenance': {
            'source': 'follow',
            'followId': follow['id'],
            'published': item.get('published'),
            'videoId': item.get('videoId'),
            'untrusted': True,  # third-party text; treat as data, never as instructions
        },
    }


async def _merge_follow(brain: Any, follow_id: str, changes: dict[str, Any]) -> None:
    """Write only the fields a sync changed into the freshly loaded record (adds are never lost)."""
    async with brain._sources_lock:
        follows = await _load(brain)
        for f in follows:
            if f['id'] == follow_id:
                f.update(changes)
        await _store(brain, follows)


async def _sync_one(brain: Any, fetch_text: FetchText, follow: dict[str, Any], limit: int, dry_run: bool) -> dict[str, Any]:
    fetched = await fetch_text(follow['feedUrl'])
    parsed = parse_feed(fetched.get('text') or '') if fetched.get('ok') else {
        'ok': False, 'error': fetched.get('error'), 'message': fetched.get('message'),
    }
    if not parsed.get('ok'):
        err = f"{parsed.get('error')}: {parsed.get('message')}"[:300]
        if not dry_run:
            await _merge_follow(brain, follow['id'], {'lastError': err, 'lastSyncAt': _now()})
        return {'id': follow['id'], 'ok': False, 'error': err}
    seen = set(follow.get('seen') or [])
    fresh = [i for i in parsed['items'] if i['id'] not in seen][:limit]
    saved = 0
    done_ids: list[str] = []
    if not dry_run and fresh:
        approve_all = bool(follow.get('autoApprove')) or await brain.open_mode()
        token = allow_reserved.set(True)
        await brain.begin_batch()
        try:
            for item in fresh:
                res = await brain.memory_save(approve=approve_all, **_item_memory(follow, item))
                if res.get('ok'):
                    done_ids.append(item['id'])  # only stored items count as seen; failures retry next sync
                    if not res.get('duplicate'):
                        saved += 1
        finally:
            await brain.end_batch()
            allow_reserved.reset(token)
    if not dry_run:
        vids = [i['url'] for i in fresh if i.get('videoId') and i['id'] in done_ids]
        await _merge_follow(
            brain,
            follow['id'],
            {
                'seen': (done_ids + list(follow.get('seen') or []))[:MAX_SEEN],
                'itemsSeen': int(follow.get('itemsSeen') or 0) + len(done_ids),
                'recentVideos': (vids + list(follow.get('recentVideos') or []))[:20],
                'lastError': None,
                'lastSyncAt': _now(),
            },
        )
    return {'id': follow['id'], 'ok': True, 'title': follow['title'], 'newItems': len(fresh), 'saved': saved, 'dryRun': dry_run}


async def follow_sync(
    brain: Any,
    fetch_text: FetchText,
    *,
    follow_id: str | None = None,
    limit: int = FIRST_SYNC_LIMIT,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Pull new items from every followed feed (or one). A broken feed never stops the others."""
    follows = await _load(brain)
    chosen = [f for f in follows if follow_id in (None, f['id'])]
    if follow_id and not chosen:
        return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No follow with id {follow_id}'}
    limit = max(1, min(int(limit), 50))
    report: list[dict[str, Any]] = []
    for follow in chosen:
        try:
            report.append(await _sync_one(brain, fetch_text, follow, limit, dry_run))
        except Exception as exc:  # noqa: BLE001 - one hostile/broken feed must not stop the rest
            err = f'{type(exc).__name__}: {exc}'[:300]
            report.append({'id': follow['id'], 'ok': False, 'error': err})
            if not dry_run:
                await _merge_follow(brain, follow['id'], {'lastError': err, 'lastSyncAt': _now()})
    await brain.audit('follow', detail=f"sync:{len(chosen)} feeds"[:300])
    return {'ok': all(r['ok'] for r in report) if report else True, 'feeds': report}


async def queue_transcripts(brain: Any, follow_id: str, *, max_cost_usd: float, max_videos: int = 5) -> dict[str, Any]:
    """Queue ONE capped, paid run of the owner's own transcript Actor for recent videos."""
    follows = await _load(brain)
    follow = next((f for f in follows if f['id'] == follow_id), None)
    if not follow:
        return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No follow with id {follow_id}'}
    if follow['kind'] != 'youtube':
        return {'ok': False, 'error': 'NOT_YOUTUBE', 'message': 'Transcripts apply to YouTube follows only.'}
    urls = list(follow.get('recentVideos') or [])[: max(1, min(int(max_videos), 20))]
    if not urls:
        return {'ok': False, 'error': 'NO_VIDEOS', 'message': 'Run `follow sync` first so there are recent videos.'}
    run_input = {
        'videoUrls': urls,
        'includeSummary': False,
        'includeTopics': False,
        'includeChapters': False,
        'includeActionItems': False,
        'includeSentiment': False,
        'includeQuotes': False,
    }
    queued = await brain.queue.enqueue_actor(TRANSCRIPT_ACTOR, run_input, max_cost_usd=max_cost_usd, max_items=len(urls))
    return {'ok': 'error' not in queued, 'videos': len(urls), **queued}


def _entry_time(entry: dict[str, Any]) -> datetime | None:
    """Best date for a followed item: feed publish date, else save date; never in the future."""
    from email.utils import parsedate_to_datetime

    now = datetime.now(UTC)
    for key in ('publishedAt', 'updatedAt'):
        raw = str(entry.get(key) or '').strip()
        if not raw:
            continue
        ts = None
        try:
            ts = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            try:
                ts = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                ts = None
        if ts is not None:
            ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
            return min(ts, now)
    return None


async def recent_followed(brain: Any, *, days: int = 7, limit: int = 20, follow_id: str | None = None) -> dict[str, Any]:
    """Free read: newest followed items (approved), plus how many still wait in the inbox."""
    from datetime import timedelta

    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    cutoff = datetime.now(UTC) - timedelta(days=days)
    entries = await brain._current_index_entries()
    rows: list[dict[str, Any]] = []
    pending = 0
    for entry in entries.values():
        tags = {str(t).lower() for t in (entry.get('tags') or [])}
        if 'followed' not in tags or (follow_id and f'follow:{follow_id}' not in tags):
            continue
        if entry.get('status') == 'proposed':
            pending += 1
            continue
        if entry.get('status') != 'approved':
            continue
        ts = _entry_time(entry)
        if ts is None:
            continue
        if ts >= cutoff:
            rows.append({'ts': ts, 'entry': entry})
    rows.sort(key=lambda r: r['ts'], reverse=True)
    return {
        'ok': True,
        'days': days,
        'count': min(len(rows), limit),
        'pendingInInbox': pending,
        'items': [
            {
                'id': r['entry'].get('id'),
                'title': r['entry'].get('title'),
                'when': r['ts'].isoformat(),
                'summary': (r['entry'].get('summary') or '')[:300],
                'url': (r['entry'].get('sourceUris') or [None])[0],
                'tags': r['entry'].get('tags'),
            }
            for r in rows[:limit]
        ],
    }
