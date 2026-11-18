"""Background chores the hosted server does on its own, so the owner does not have to.

While the server is awake (someone is using it) it will, without being asked:
  * fetch pages that were queued because they were slow to answer (charged only when one is saved);
  * check followed feeds for new items, at most once a day, ONLY if the owner switched that on.

The daily check is opt-in because a check that saves something new costs one tool-call. Nothing
here runs while the server sleeps; for checks on days you do not use it, add an Apify schedule
(input mode=follow). Chores never run two at a time and never block a request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from .constants import CONNECTION_PATH, READINESS_HEADER

KEY_AUTO = 'config.auto'
RECHECK_S = 600.0  # look for chores at most this often
FOLLOW_EVERY = timedelta(hours=24)
DRAIN_LIMIT = 5
ITEMS_PER_FEED = 5

logger = logging.getLogger('braintwin.autopilot')


async def auto_status(brain: Any) -> dict[str, Any]:
    raw = await brain.kv.get_value(KEY_AUTO)
    raw = raw if isinstance(raw, dict) else {}
    return {'followDaily': raw.get('followDaily') is True, 'lastFollowCheck': raw.get('lastFollowCheck')}


_state_lock = asyncio.Lock()  # the switch and the daily stamp share one record


async def set_follow_daily(brain: Any, on: bool) -> dict[str, Any]:
    async with _state_lock:
        state = await auto_status(brain)
        state['followDaily'] = bool(on)
        await brain.kv.set_value(KEY_AUTO, state)
        return state


def _due(last: Any, now: datetime) -> bool:
    try:
        then = datetime.fromisoformat(str(last).replace('Z', '+00:00'))
    except ValueError:
        return True
    return now - (then if then.tzinfo else then.replace(tzinfo=UTC)) >= FOLLOW_EVERY


class Autopilot:
    def __init__(self, get_brain: Callable[[], Awaitable[Any]], charge: Callable[[], Awaitable[None]]) -> None:
        self._get_brain = get_brain
        self._charge = charge
        self._busy = False
        self._next = 0.0
        self.task: asyncio.Task[None] | None = None

    def nudge(self) -> None:
        """Called after a request. Cheap: starts a chore run only when one is due."""
        if self._busy or time.monotonic() < self._next:
            return
        self._busy = True
        self._next = time.monotonic() + RECHECK_S
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self.run_once()
        except Exception as exc:  # noqa: BLE001 - a chore must never take the server down
            logger.warning('autopilot failed: %s: %s', type(exc).__name__, str(exc)[:200])
        finally:
            self._busy = False

    async def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        from .follow import follow_list, follow_sync
        from . import ingest
        from .ingest_worker import drain

        brain = await self._get_brain()
        out: dict[str, Any] = {'drained': 0, 'followSaved': None}
        if brain.queue is not None and await brain.queue.job_ids():
            result = await drain(brain, limit=DRAIN_LIMIT, allow_actors=False, fetch=ingest.fetch_landing_page)
            out['drained'] = int(result.get('ingested') or 0)
            for _ in range(int(result.get('filedPages') or 0)):
                await self._charge()  # a queued page is paid for when it is saved, not when it was queued
        state = await auto_status(brain)
        now = now or datetime.now(UTC)
        from .notion import notion_status as _notion_status

        has_sources = bool((await follow_list(brain)).get('count')) or (await _notion_status(brain))['on']
        if state['followDaily'] and _due(state.get('lastFollowCheck'), now) and has_sources:
            # Stamp first: a slow or failing check must not be retried on every request.
            async with _state_lock:
                fresh = await auto_status(brain)  # the owner may have switched it off a moment ago
                still_on = fresh['followDaily']
                if still_on:
                    await brain.kv.set_value(KEY_AUTO, {**fresh, 'lastFollowCheck': now.isoformat()})
            if still_on:
                # No overall timeout: cutting the check off half-way would keep what it saved but skip
                # the charge. Each feed fetch has its own time limit, and this runs in the background.
                res = await follow_sync(brain, ingest.fetch_text, limit=ITEMS_PER_FEED)
                saved = sum(int(f.get('saved') or 0) for f in res.get('feeds') or [])
                from .notion import notion_status, notion_sync

                if (await notion_status(brain))['on']:  # the same daily switch covers a connected Notion
                    saved += int((await notion_sync(brain)).get('changed') or 0)
                out['followSaved'] = saved
                if saved:
                    await self._charge()  # same single tool-call as pressing "Check now"
        from . import digest

        if digest.is_due(await digest.digest_setting(brain), now):
            made = await digest.make_digest(brain, charge_save=self._charge, now=now)
            out['digest'] = bool(made.get('made'))
        return out


class AutopilotMiddleware:
    """Nudge the autopilot after normal HTTP requests, excluding connection and readiness checks."""

    def __init__(self, app: ASGIApp, autopilot: Autopilot) -> None:
        self.app = app
        self.autopilot = autopilot

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        finally:
            if (
                scope.get('type') == 'http'
                and scope.get('path') != CONNECTION_PATH
                and READINESS_HEADER not in dict(scope.get('headers', []))
            ):
                self.autopilot.nudge()
