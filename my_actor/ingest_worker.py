"""Drain the ingest queue: fetch queued URLs (and, only when allowed, run owned Actors).

Callers: my_actor/main.py (Actor run with input mode=ingest), my_actor/cli.py.

Everything fetched or scraped lands as a *proposed* memory — the worker never approves.

Paid (owned-Actor) jobs are fenced several ways, because a mistake costs real money:
- they only run when the caller passes allow_actors=True (the CLI flag; never the cloud Actor);
- ANY failure of a paid job — including one after the run succeeded — is permanent, so the
  same paid run can never be repeated by retry logic;
- a paid job found mid-run (worker crashed) is failed, never resumed;
- one drain commits to at most `max_total_cost_usd` of per-job caps;
- unknown job kinds fail closed.
Re-running a failed paid job takes an explicit human re-enqueue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .constants import MAX_DRAIN_BUDGET_USD, MAX_JOB_ATTEMPTS
from .ingest import fetch_landing_page
from .ingest_queue import STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING, IngestQueue
from .owned_actors import check_owned_actor, items_to_memories, run_owned_actor, validate_budget

KIND_URL = 'url'
KIND_ACTOR = 'actor'
PERMANENT_FETCH_ERRORS = frozenset(
    {'INVALID_URL', 'CREDENTIALS_IN_URL', 'BLOCKED_HOST', 'TOO_MANY_REDIRECTS', 'UNSUPPORTED_ENCODING', 'BAD_ENCODING'}
)
_RETRYABLE_STATUS = frozenset({408, 425, 429})

Fetch = Callable[[str], Awaitable[dict[str, Any]]]
ActorRunner = Callable[..., Awaitable[dict[str, Any]]]


class _Outcome:
    __slots__ = ('done', 'error', 'permanent', 'info')

    def __init__(self, done: bool, error: str | None = None, permanent: bool = False, info: str | None = None) -> None:
        self.done = done
        self.error = error
        self.permanent = permanent
        self.info = info


def _is_permanent_fetch_failure(ingest: dict[str, Any]) -> bool:
    err = ingest.get('error')
    if err in PERMANENT_FETCH_ERRORS:
        return True
    status = ingest.get('statusCode')
    return err == 'HTTP_STATUS' and isinstance(status, int) and 400 <= status < 500 and status not in _RETRYABLE_STATUS


async def _do_url(brain: Any, job: dict[str, Any], fetch: Fetch) -> _Outcome:
    ingest = await fetch(job['url'])
    if not ingest.get('ok'):
        code = str(ingest.get('error') or 'FETCH_FAILED')
        return _Outcome(False, error=f"{code}: {ingest.get('message') or ''}"[:300], permanent=_is_permanent_fetch_failure(ingest))
    applied = await brain.source_apply_ingest(job['sourceId'], ingest)
    if not applied.get('ok'):
        # The source was removed after queueing; nothing left to attach the page to.
        return _Outcome(True, info='source removed; job dropped')
    src = applied.get('source') or {}
    await brain.auto_file(src.get('proposedMemoryId'))  # no-op unless the owner chose automatic
    return _Outcome(True, info='ingested' if src.get('filedNew') is not False else 'ingested, nothing new')


async def _do_actor(brain: Any, job: dict[str, Any], runner: ActorRunner, token: str | None) -> _Outcome:
    actor, error = check_owned_actor(job.get('actor'))
    if error:
        return _Outcome(False, error=error, permanent=True)
    budget_error = validate_budget(job.get('maxCostUsd'), job.get('maxItems'))
    if budget_error:
        return _Outcome(False, error=budget_error, permanent=True)
    run = await runner(
        actor,
        job.get('input') or {},
        max_cost_usd=float(job['maxCostUsd']),
        max_items=int(job['maxItems']),
        token=token,
    )
    if not run.get('ok'):
        return _Outcome(False, error=f"{run.get('error')}: {run.get('message')}"[:300], permanent=True)
    # From here the run has been paid for. Any exception below is turned into a permanent
    # failure by _run_one (kind == actor), so the paid run is never repeated.
    memories = items_to_memories(run.get('items') or [], actor, limit=int(job['maxItems']), run_id=run.get('runId'))
    created = 0
    await brain.begin_batch()
    try:
        for mem in memories:
            res = await brain.memory_save(approve=False, **mem)
            if res.get('ok') and not res.get('duplicate'):
                created += 1
    finally:
        await brain.end_batch()
    await brain.audit('scrape', detail=f"{actor} run={run.get('runId')} cost={run.get('costUsd')} proposed={created}")
    return _Outcome(True, info=f'proposed {created} of {len(run.get("items") or [])} items (cost ${run.get("costUsd")})')


async def _run_one(
    brain: Any,
    job: dict[str, Any],
    sem: asyncio.Semaphore,
    fetch: Fetch,
    runner: ActorRunner,
    token: str | None,
    allow_actors: bool,
) -> str:
    q: IngestQueue = brain.queue
    kind = job.get('kind')
    async with sem:
        job['attempts'] = int(job.get('attempts') or 0) + 1
        job['status'] = STATUS_RUNNING
        await q.save(job)
        try:
            if kind == KIND_URL:
                outcome = await _do_url(brain, job, fetch)
            elif kind == KIND_ACTOR:
                if allow_actors:
                    outcome = await _do_actor(brain, job, runner, token)
                else:
                    outcome = _Outcome(False, error='ACTORS_NOT_ALLOWED', permanent=True)
            else:
                outcome = _Outcome(False, error=f'UNKNOWN_KIND: {kind!r}', permanent=True)
        except Exception as exc:  # noqa: BLE001 — one bad job must not abort the whole drain
            # A paid job is never retried, whatever failed.
            outcome = _Outcome(False, error=f'{type(exc).__name__}: {exc}'[:300], permanent=kind != KIND_URL)
        if outcome.done:
            await q.delete(job['id'])
            # 'filed' = a queued web page that really added something new (the only case that is charged)
            return 'filed' if kind == KIND_URL and outcome.info == 'ingested' else 'ingested'
        job['error'] = outcome.error
        if outcome.permanent or job['attempts'] >= MAX_JOB_ATTEMPTS:
            job['status'] = STATUS_FAILED
            await q.save(job)
            return 'failed'
        job['status'] = STATUS_QUEUED
        await q.save(job)
        return 'requeued'


async def _fail(q: IngestQueue, job: dict[str, Any], reason: str) -> None:
    job['status'] = STATUS_FAILED
    job['error'] = reason
    await q.save(job)


async def drain(
    brain: Any,
    *,
    limit: int = 20,
    concurrency: int = 3,
    allow_actors: bool = False,
    max_total_cost_usd: float = MAX_DRAIN_BUDGET_USD,
    fetch: Fetch = fetch_landing_page,
    actor_runner: ActorRunner = run_owned_actor,
    token: str | None = None,
) -> dict[str, Any]:
    """Process up to `limit` runnable jobs; returns a summary (never raises per-job)."""
    q: IngestQueue = brain.queue
    all_jobs = await q.list_jobs()

    # Settle what a crashed worker left behind, so nothing sits in limbo forever.
    interrupted_actor = 0
    for job in all_jobs:
        kind, status = job.get('kind'), job.get('status')
        if status == STATUS_FAILED:
            continue
        if kind not in (KIND_URL, KIND_ACTOR):
            await _fail(q, job, f'UNKNOWN_KIND: {kind!r}')
        elif status == STATUS_RUNNING and kind == KIND_ACTOR:
            await _fail(q, job, 'INTERRUPTED: a paid run may have started; re-enqueue to run it again')
            interrupted_actor += 1
        elif status == STATUS_RUNNING and int(job.get('attempts') or 0) >= MAX_JOB_ATTEMPTS:
            await _fail(q, job, 'INTERRUPTED: worker stopped before the last attempt finished')

    runnable = [j for j in all_jobs if q.is_runnable(j)]
    skipped_actor = [j for j in runnable if j.get('kind') == KIND_ACTOR] if not allow_actors else []
    batch: list[dict[str, Any]] = []
    over_budget = 0
    committed = 0.0
    for job in runnable:
        if len(batch) >= max(1, int(limit)):
            break
        if job.get('kind') == KIND_ACTOR:
            if not allow_actors:
                continue
            cost = float(job.get('maxCostUsd') or 0)
            if committed + cost > max_total_cost_usd:
                over_budget += 1
                continue
            committed += cost
        batch.append(job)

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    results = await asyncio.gather(
        *[_run_one(brain, j, sem, fetch, actor_runner, token, allow_actors) for j in batch],
        return_exceptions=True,
    )
    outcomes = ['failed' if isinstance(r, BaseException) else r for r in results]

    remaining = len([j for j in await q.list_jobs() if q.is_runnable(j)])
    return {
        'ok': True,
        'attempted': len(batch),
        'ingested': outcomes.count('ingested') + outcomes.count('filed'),
        'filedPages': outcomes.count('filed'),
        'requeued': outcomes.count('requeued'),
        'failed': outcomes.count('failed'),
        'skippedActorJobs': len(skipped_actor),
        'skippedOverBudget': over_budget,
        'interruptedActorJobs': interrupted_actor,
        'remaining': remaining,
    }
