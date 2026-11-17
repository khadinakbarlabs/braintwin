"""Owner dashboard: one web page + a small JSON API in front of the MCP endpoint.

Who may use it: whoever the platform lets through to this server. On Apify Standby that means a
valid Apify token (the page is opened as `<standby-url>/?token=...` and the page re-uses that same
query string for its API calls; nothing is stored in the browser). There is no cookie, so a
cross-site form cannot act for the owner; POSTs must also carry `X-Dashboard: 1` and JSON.

What it can do, with the same rules and the same charges as the MCP tools:
  read     overview, inbox, memories (filter / sort / page), one memory in full   (free)
  write    approve, reject, forget, add a note                                   ($0.01 per request
           that really changed something, same `tool-call` event as the tools)

Owner-level approval lives here on purpose (the dashboard is the human side of the inbox). Turn the
whole thing off with SECOND_BRAIN_DASHBOARD=off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .settings import KEY_EMBEDDINGS as KEY_USER_EMBEDDINGS
from .notion import notion_status, notion_sync, set_notion_token, token_problem
from . import digest
from . import builtin_ai
from .autopilot import auto_status, set_follow_daily
from .settings import on_platform as on_platform  # noqa: PLC0414
from .capture import extract_candidates
from .dashboard_page import render_page
from .follow import follow_add, follow_list, follow_memory_ids, follow_remove, follow_sync, follow_update
from .import_memories import text_to_records
from .ingest import check_url_async, fetch_landing_page, fetch_text
from .settings import KeyGate, env_dashboard_key, key_problem

PAGE_PATHS = ('/', '/dashboard', '/dashboard/')
MAX_BODY_BYTES = 64 * 1024
MAX_IMPORT_BODY_BYTES = 1024 * 1024  # an import file is bigger than any other request
MAX_IDS = 100
MAX_TITLE = 200
MAX_CONTENT = 20_000
MAX_TAGS = 10
MAX_TAG_CHARS = 40
ALLOWED_STATUSES = ('approved', 'proposed', 'decayed', 'rejected')
ALLOWED_TYPES = ('note', 'preference', 'fact', 'decision', 'person', 'project', 'procedure')

log = logging.getLogger('second_brain.dashboard')
MAX_QUERY = 300
MAX_TARGET = 500
MAX_FORGET_WITH_SOURCE = 300  # a click must answer in time; the rest stay and can be cleared by hand
SYNC_ITEMS_PER_FEED = 10
SYNC_TIMEOUT_S = 90.0  # a dashboard click must answer before the proxy gives up; the daily run finishes the rest

BrainGetter = Callable[[], Awaitable[Any]]
Charger = Callable[[], Awaitable[None]]


def dashboard_enabled() -> bool:
    return (os.environ.get('SECOND_BRAIN_DASHBOARD') or 'on').strip().lower() not in ('off', '0', 'false', 'no')


def dashboard_key() -> str:
    """Environment key for CHANGES. Used only off the Apify platform; on Apify each user sets their own
    key in the dashboard (stored hashed in their own storage)."""
    return env_dashboard_key()


def _json(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={'Cache-Control': 'no-store'})


def _err(status: int, code: str, message: str) -> JSONResponse:
    return _json({'ok': False, 'error': code, 'message': message}, status)


def _clean_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [str(v).strip() for v in value if isinstance(v, str) and v.strip() and len(v) <= 128]
    return list(dict.fromkeys(out))[:MAX_IDS]


def _clean_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags = [str(t).strip().lower()[:MAX_TAG_CHARS] for t in value if isinstance(t, str) and t.strip()]
    return list(dict.fromkeys(tags))[:MAX_TAGS]


# Settings that decide where your text goes or how much is saved without you.
LINK_FETCH_SECS = 25
LOCKED_UNTIL_KEY = frozenset({'settings/ai', 'settings/digest', 'digest/now', 'settings/saving', 'settings/capture', 'settings/meaning', 'settings/notion', 'follows/auto'})

class DashboardMiddleware:
    """ASGI layer: serves the page and `/api/*`; everything else goes on to the MCP app."""

    def __init__(self, app: ASGIApp, *, get_brain: BrainGetter, charge: Charger, approval: str = 'auto') -> None:
        self.app = app
        self.get_brain = get_brain
        self.charge = charge
        self.approval = approval
        self.enabled = dashboard_enabled()
        self.key = dashboard_key()
        self.gate = KeyGate(self.key)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type') != 'http' or not self.enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        method = scope.get('method', 'GET')
        is_page = path in PAGE_PATHS and method == 'GET'
        is_api = path.startswith('/api/')
        # The platform's readiness probe also does GET /: answer 200 (the page) so Standby comes up.
        if not (is_page or is_api):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            response = self._page() if is_page else await self._api(request, path[len('/api/') :], method)
        except Exception as exc:  # noqa: BLE001 - never leak internals to the browser
            log.error('dashboard %s %s failed: %s', method, path, type(exc).__name__)  # no query string: it may hold a token
            response = _err(500, 'SERVER_ERROR', 'Something went wrong. Check the Actor log.')
        await response(scope, receive, send)

    # ------------------------------------------------------------------ page
    def _page(self) -> Response:
        nonce = secrets.token_urlsafe(16)
        return HTMLResponse(
            render_page(nonce),
            headers={
                'Content-Security-Policy': (
                    "default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; "
                    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'" % (nonce, nonce)
                ),
                'X-Frame-Options': 'DENY',
                'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer',
                'Cache-Control': 'no-store',
            },
        )

    # ------------------------------------------------------------------ api
    def _key_check(self, request: Request, in_body: str = '') -> JSONResponse | None:
        """None when the request may go ahead; otherwise the refusal to send.

        The key comes in the `X-Dashboard-Key` header (the page served by this Actor) or in the
        JSON body as `_key` (the hosted page: Apify's gateway lets no custom header through from
        another site).
        """
        if not self.gate.required:
            return None
        supplied = request.headers.get('x-dashboard-key') or in_body or ''
        if not supplied:
            return _err(403, 'KEY_REQUIRED', 'This dashboard needs its key to make changes.')
        if self.gate.locked():
            return _err(429, 'KEY_LOCKED', 'Too many wrong keys. Wait half a minute and try again.')
        if not self.gate.verify(supplied):
            return _err(403, 'BAD_KEY', 'Wrong dashboard key.')
        return None

    def _needs_key_first(self) -> JSONResponse | None:
        """On Apify, settings that change who sees your data stay locked until a dashboard key exists.

        Agents you connect hold the same Apify token as you do. Without this, one of them could
        switch these settings through the page's own endpoints.
        """
        if self.gate.required or not on_platform():
            return None
        return _err(403, 'KEY_NEEDED', 'Set a dashboard key first (Settings tab). It keeps these settings out of reach of anything else that holds your Apify token.')

    async def _body(self, request: Request, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any] | JSONResponse:
        marked = request.headers.get('x-dashboard') == '1'
        if marked:  # same-site page: refuse a wrong key before reading anything
            denied = self._key_check(request)
            if denied is not None:
                return denied
        if 'application/json' not in (request.headers.get('content-type') or ''):
            return _err(403, 'FORBIDDEN', 'Missing X-Dashboard header.') if not marked else _err(415, 'UNSUPPORTED', 'Send JSON.')
        length = request.headers.get('content-length')
        if length and length.isdigit() and int(length) > max_bytes:
            return _err(413, 'TOO_LARGE', 'Request too large.')
        raw = b''
        async for chunk in request.stream():  # stop reading at the cap (no Content-Length on chunked bodies)
            raw += chunk
            if len(raw) > max_bytes:
                return _err(413, 'TOO_LARGE', 'Request too large.')
        try:
            data = json.loads(raw or b'{}')
        except ValueError:
            return _err(400, 'BAD_JSON', 'Body is not valid JSON.')
        if not isinstance(data, dict):
            return _err(400, 'BAD_JSON', 'Body must be a JSON object.')
        in_body_key = data.pop('_key', '')
        in_body_mark = data.pop('_dash', None)
        if not marked:
            # Hosted page on another site: a JSON body forces the browser to ask permission first
            # (a plain cross-site form cannot send one), and the marker rides inside the body.
            if in_body_mark != 1:
                return _err(403, 'FORBIDDEN', 'Missing X-Dashboard header.')
            denied = self._key_check(request, str(in_body_key or ''))
            if denied is not None:
                return denied
        return data

    @staticmethod
    async def _export(brain: Any, status: Any) -> Response:
        statuses = [x for x in str(status or 'approved').split(',') if x in ALLOWED_STATUSES] or ['approved']
        data = await brain.export_memories(statuses)
        return JSONResponse(
            data,
            headers={'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="second-brain-export.json"'},
        )

    async def _api(self, request: Request, name: str, method: str) -> Response:
        brain = await self.get_brain()
        await self.gate.load(brain.kv)
        q = request.query_params
        if method == 'GET':
            if name == 'overview':
                health = await brain.brain_health(include_sources=True)
                inbox = await brain.inbox_list()  # one after the other: both take the same lock
                following = int((await follow_list(brain)).get('count') or 0)  # channels and blogs, not saved pages
                return _json(
                    {
                        'ok': True,
                        'health': health,
                        'approvalMode': self.approval,
                        'recentAutoApproved': inbox.get('recentAutoApproved') or [],
                        'inboxCount': inbox.get('count', 0),
                        'sourcesFollowed': following,
                        'keyRequired': self.gate.required,
                        'keySetByUser': self.gate.user_set,
                        'meaningSearch': brain.meaning_status(),
                        'smartCapture': brain.capture_status(),
                        'builtinAi': (await builtin_ai.provider.status(brain)) if builtin_ai.provider else {'available': False, 'on': False},
                        'savingMode': (await brain.get_policy()).get('mode'),
                    }
                )
            if name == 'inbox':
                return _json(await brain.inbox_list())
            if name == 'memories':
                status = q.get('status') or 'approved'
                statuses = [s for s in status.split(',') if s in ALLOWED_STATUSES] or ['approved']
                text = (q.get('q') or '').strip()[:MAX_QUERY]
                limit = max(1, min(_int(q.get('limit'), 50), 100))
                if text:
                    res = await brain.memory_search(text, approved_only=False, statuses=statuses, limit=min(limit, 50))
                    items = [r['memory'] for r in (res.get('results') or []) if isinstance(r.get('memory'), dict)]
                    return _json({'ok': True, 'total': len(items), 'offset': 0, 'items': items, 'searched': True})
                sort = q.get('sort') if q.get('sort') in ('newest', 'oldest', 'title') else 'newest'
                return _json(
                    await brain.memory_list(
                        statuses=statuses, tag=q.get('tag'), sort=sort, limit=limit, offset=_int(q.get('offset'), 0)
                    )
                )
            if name == 'follows':
                listing = await follow_list(brain)
                ids = await follow_memory_ids(brain)
                for f in listing['follows']:  # what each source brought in: kept, and still waiting for a yes
                    got = ids.get(str(f.get('id'))) or {}
                    f['saved'], f['waiting'] = len(got.get('approved') or []), len(got.get('proposed') or [])
                return _json({**listing, 'auto': await auto_status(brain), 'notion': await notion_status(brain), 'digest': await digest.digest_status(brain)})
            if name == 'export':  # the whole memory, forgotten and rejected items included: owner only
                denied = self._needs_key_first() or self._key_check(request)
                if denied is not None:
                    return denied
                return await self._export(brain, q.get('status'))
            if name == 'memory':
                mid = (q.get('id') or '').strip()
                if not mid:
                    return _err(400, 'INVALID_INPUT', 'id required')
                return _json(await brain.memory_explain(mid))
            return _err(404, 'NOT_FOUND', 'Unknown endpoint.')

        if method != 'POST':
            return _err(405, 'METHOD', 'Use GET or POST.')
        body = await self._body(request, MAX_IMPORT_BODY_BYTES if name == 'import' else MAX_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body

        if name == 'export':  # same download for the hosted page, which cannot send the key in a header
            denied = self._needs_key_first()
            return denied if denied is not None else await self._export(brain, body.get('status'))
        if name in LOCKED_UNTIL_KEY:
            denied = self._needs_key_first()
            if denied is not None:
                return denied
        if name == 'settings/key':  # free: a setting, not a memory change
            new_key = body.get('newKey')
            if new_key in (None, ''):
                if not self.gate.user_set:
                    return _err(400, 'INVALID_INPUT', 'No key is set.')
                await self.gate.clear(brain.kv)
                return _json({'ok': True, 'keyRequired': self.gate.required})
            problem = key_problem(new_key)
            if problem:
                return _err(400, 'INVALID_INPUT', problem)
            await self.gate.set(brain.kv, new_key)
            return _json({'ok': True, 'keyRequired': True})
        if name == 'settings/meaning':  # free: a setting, not a memory change
            if body.get('off') is True:
                await brain.set_meaning_search(None)
                return _json({'ok': True, 'meaningSearch': brain.meaning_status()})
            record = {k: str(body.get(k) or '').strip()[:500] for k in ('url', 'model', 'key', 'dims')}
            if not await brain.set_meaning_search(record):
                return _err(400, 'INVALID_INPUT', 'Need a public https address, a model name and an API key.')
            return _json({'ok': True, 'meaningSearch': brain.meaning_status()})

        if name == 'settings/saving':  # free: a setting. Owner-only by design (the page, not an agent tool).
            mode = str(body.get('mode') or '').strip().lower()
            if mode not in ('open', 'smart', 'off'):
                return _err(400, 'INVALID_INPUT', 'mode must be open, smart or off')
            return _json({'ok': True, 'savingMode': (await brain.set_policy({'mode': mode})).get('mode')})
        if name == 'settings/capture':  # free: a setting, not a memory change
            if body.get('off') is True:
                await brain.set_smart_capture(None)
                return _json({'ok': True, 'smartCapture': brain.capture_status()})
            record = {k: str(body.get(k) or '').strip()[:500] for k in ('url', 'model', 'key')}
            if body.get('sameAsMeaning') is True:
                # One step for people who already set up meaning search: same service, same key.
                stored = await brain.kv.get_value(KEY_USER_EMBEDDINGS)
                if not isinstance(stored, dict) or not stored.get('url') or not stored.get('key'):
                    return _err(400, 'INVALID_INPUT', 'Turn on meaning search first, or enter the address and key here.')
                record['url'], record['key'] = str(stored['url']), str(stored['key'])
            if not await brain.set_smart_capture(record):
                return _err(400, 'INVALID_INPUT', 'Need a public https address, a model name and an API key.')
            return _json({'ok': True, 'smartCapture': brain.capture_status()})

        if name == 'follows/add':  # free: nothing is remembered until a check finds new items
            target = str(body.get('target') or '').strip()
            if not target or len(target) > MAX_TARGET:
                return _err(400, 'INVALID_INPUT', 'Paste a YouTube channel address or a blog / newsletter address.')
            if not target.lower().startswith(('http://', 'https://')) and not target.startswith('UC'):
                target = 'https://' + target
            return _json(await follow_add(brain, target, fetch_text, auto_approve=body.get('autoApprove') is True))
        if name == 'settings/ai':  # free: a setting. Switching the paid AI step on or off is the owner's call.
            state = await builtin_ai.set_builtin(brain, body.get('on') is True)
            return _json({'ok': True, 'builtinAi': {'on': state['builtin']}})
        if name == 'settings/digest':  # free: a setting. Each digest written later has its own price.
            every = str(body.get('every') or 'off').strip().lower()
            if every not in ('off', 'daily', 'weekly', 'monthly'):
                return _err(400, 'INVALID_INPUT', 'every must be off, daily, weekly or monthly')
            await digest.set_digest_every(brain, every)
            return _json({'ok': True, 'digest': await digest.digest_status(brain)})
        if name == 'digest/now':
            res = await digest.make_digest(brain, charge_save=self.charge, force=True)
            return _json({**res, 'digest': await digest.digest_status(brain)})
        if name == 'settings/notion':  # free: a setting
            if body.get('off') is True:
                return _json({'ok': True, 'notion': await set_notion_token(brain, None)})
            problem = token_problem(body.get('token'))
            if problem:
                return _err(400, 'INVALID_INPUT', problem)
            return _json({'ok': True, 'notion': await set_notion_token(brain, str(body['token']))})
        if name == 'notion/sync':
            res = await notion_sync(brain)
            if res.get('ok') and res.get('changed'):
                await self.charge()  # one tool-call per sync that saved something, however many pages
            return _json(res)
        if name == 'follows/auto':  # free: a setting
            return _json({'ok': True, 'auto': await set_follow_daily(brain, body.get('on') is True)})
        if name == 'follows/remove':
            fid = str(body.get('id') or '').strip()[:128]
            if not fid:
                return _err(400, 'INVALID_INPUT', 'id required')
            res = await follow_remove(brain, fid)
            if res.get('ok') and body.get('forget') is True:  # also clear out what this source brought in
                got = (await follow_memory_ids(brain, fid)).get(fid) or {}
                forgotten = 0
                for mid in (got.get('proposed') or [])[:MAX_FORGET_WITH_SOURCE]:
                    done = await brain.inbox_reject(mid, reason='source removed')
                    forgotten += 1 if done.get('ok', True) and done.get('changed', True) else 0
                for mid in (got.get('approved') or [])[:MAX_FORGET_WITH_SOURCE]:
                    done = await brain.memory_forget(mid, reason='source removed')
                    forgotten += 1 if done.get('ok', True) and done.get('changed', True) else 0
                if forgotten:
                    await self.charge()  # one tool-call, however many items were cleared
                res = {**res, 'forgotten': forgotten, 'note': f'Removed, and cleared {forgotten} item(s) it brought in.'}
            return _json(res)
        if name == 'follows/update':  # free: a setting of one source
            fid = str(body.get('id') or '').strip()[:128]
            if not fid:
                return _err(400, 'INVALID_INPUT', 'id required')
            auto = body.get('autoApprove')
            title = body.get('title')
            return _json(
                await follow_update(
                    brain,
                    fid,
                    auto_approve=auto if isinstance(auto, bool) else None,
                    title=str(title)[:200] if isinstance(title, str) else None,
                )
            )
        if name == 'follows/sync':
            fid = str(body.get('id') or '').strip()[:128] or None
            try:
                res = await asyncio.wait_for(
                    follow_sync(brain, fetch_text, follow_id=fid, limit=SYNC_ITEMS_PER_FEED), SYNC_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                return _err(504, 'SLOW', 'Checking took too long. Some feeds were checked; try again or let the daily run finish.')
            saved = sum(int(f.get('saved') or 0) for f in res.get('feeds') or [])
            if saved:
                await self.charge()  # one tool-call per check that saved something, however many items
            return _json({**res, 'saved': saved})

        if name == 'approve':
            ids = _clean_ids(body.get('ids'))
            if not ids:
                return _err(400, 'INVALID_INPUT', 'ids required')
            result = await brain.inbox_approve_many(ids, dry_run=False)
            if result.get('ok') and result.get('approved', 0) > 0:
                await self.charge()
            return _json(result)
        if name == 'reject':
            ids = _clean_ids(body.get('ids'))
            if not ids:
                return _err(400, 'INVALID_INPUT', 'ids required')
            reason = str(body.get('reason') or '')[:200] or None
            changed = 0
            for mid in ids:
                res = await brain.inbox_reject(mid, reason=reason)
                if res.get('ok', True) and res.get('changed', True):
                    changed += 1
            if changed:
                await self.charge()
            return _json({'ok': True, 'rejected': changed, 'requested': len(ids)})
        if name == 'forget':
            mid = str(body.get('id') or '').strip()
            if not mid:
                return _err(400, 'INVALID_INPUT', 'id required')
            res = await brain.memory_forget(mid, reason=str(body.get('reason') or '')[:200] or None)
            if res.get('ok', True) and res.get('changed', True):
                await self.charge()
            return _json(res)
        if name == 'import':
            text = str(body.get('text') or '').strip()
            if not text:
                return _err(400, 'INVALID_INPUT', 'Paste some text or an export file first.')
            payload: Any = None
            if text[:1] in '[{':
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
            source = 'paste'
            if payload is None:
                payload = text_to_records(text)
                source = 'text'
                if not payload:
                    return _err(400, 'INVALID_INPUT', 'Found nothing to import in that text.')
            preview = body.get('preview') is True
            res = await brain.import_plugin_export(
                payload,
                source_plugin=None if source == 'paste' else 'text',
                approve=body.get('approve') is True,
                dry_run=preview,
                extra_tags=['imported'],
            )
            saved = sum(1 for i in res.get('items') or [] if not i.get('duplicate')) if res.get('ok') and not preview else 0
            if saved:
                await self.charge()  # one tool-call per import that saved something
            items = [{'title': i.get('title'), 'duplicate': i.get('duplicate')} for i in (res.get('items') or [])[:50]]
            return _json({**{k: v for k, v in res.items() if k != 'items'}, 'items': items, 'saved': saved})
        if name == 'edit':
            mid = str(body.get('id') or '').strip()
            title = str(body.get('title') or '').strip()
            content = str(body.get('content') or '').strip()
            if not mid or not title or not content:
                return _err(400, 'INVALID_INPUT', 'id, title and content required')
            if len(title) > MAX_TITLE or len(content) > MAX_CONTENT:
                return _err(400, 'TOO_LONG', f'Title max {MAX_TITLE}, content max {MAX_CONTENT} characters.')
            tags = _clean_tags(body.get('tags')) if isinstance(body.get('tags'), list) else None
            res = await brain.memory_edit(mid, title=title, content=content, tags=tags)
            if res.get('ok') and res.get('changed'):
                await self.charge()
            return _json(res)
        if name == 'link':  # a pasted link: read the page now and file it; charged only when something is saved
            url = str(body.get('url') or '').strip()
            if not url or len(url) > 2000:
                return _err(400, 'INVALID_INPUT', 'Paste one link.')
            if await check_url_async(url):
                return _json({'ok': False, 'error': 'BAD_URL', 'message': 'Only public web links can be saved.'})
            try:
                async with asyncio.timeout(LINK_FETCH_SECS):
                    ingest = await fetch_landing_page(url)
            except TimeoutError:
                return _json({'ok': False, 'error': 'TIMEOUT', 'message': 'That page took too long. Nothing saved, not charged.'})
            if not ingest.get('ok'):
                return _json({'ok': False, 'error': 'FETCH_FAILED', 'message': 'Could not read that page. Nothing saved, not charged.'})
            res = await brain.source_add(url=url, title=None, tags=['dashboard'], ingest=ingest, auto=self.approval == 'auto')
            if not res.get('ok'):
                return _json({'ok': False, 'error': res.get('error') or 'FAILED', 'message': res.get('message') or 'Could not save that page.'})
            src = res.get('source') or {}
            if res.get('duplicate') or src.get('alreadyDecided') or src.get('filedNew') is False:
                return _json({'ok': True, 'saved': False, 'message': 'Already in your memory. Not charged.'})
            await self.charge()
            return _json({'ok': True, 'saved': True, 'message': 'Page saved.'})
        if name == 'add':
            title = str(body.get('title') or '').strip()
            content = str(body.get('content') or '').strip()
            if not title or not content:
                return _err(400, 'INVALID_INPUT', 'title and content required')
            if len(title) > MAX_TITLE or len(content) > MAX_CONTENT:
                return _err(400, 'TOO_LONG', f'Title max {MAX_TITLE}, content max {MAX_CONTENT} characters.')
            mtype = str(body.get('type') or 'auto').lower()
            if mtype == 'auto':
                # Work out what the sentence is (a rule, a fact about the owner, a decision) from its wording.
                found = extract_candidates(content, max_items=1)
                mtype = found[0]['type'] if found else 'note'
            res = await brain.memory_save(
                title=title,
                content=content,
                memory_type=mtype if mtype in ALLOWED_TYPES else 'note',
                tags=_clean_tags(body.get('tags')) + ['dashboard'],
                confidence=1.0,
                approve=True,  # typed by the owner in their own dashboard
                provenance={'via': 'dashboard'},
            )
            if res.get('ok', True) and not res.get('duplicate'):
                await self.charge()
            return _json(res)
        return _err(404, 'NOT_FOUND', 'Unknown endpoint.')


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
