"""Durable ingest job queue on the brain's key-value store (one key per job).

Why a queue: an always-on Standby server must not fetch pages itself. The MCP tool only
records a job (cheap, instant); a separate worker run drains the queue and fetches.

One KV key per job (`ingest_job.<id>`) — never a shared blob — so enqueueing from the
server while a worker updates a different job cannot lose writes.

Callers: my_actor/brain.py (enqueue), my_actor/ingest_worker.py (claim/finish).
Single worker at a time: there is no compare-and-swap on Apify KV, so two workers could
claim the same job. The worst case is one page fetched twice (dedupe keeps one proposal).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, UTC
from typing import Any

from .constants import INGEST_JOB_PREFIX, MAX_JOB_ATTEMPTS, MAX_QUEUE_JOBS

STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_FAILED = 'failed'


def _now() -> str:
    return datetime.now(UTC).isoformat()


def job_id_for(kind: str, key: str) -> str:
    """Deterministic id so the same URL / actor request is one job, not many."""
    return 'job_' + hashlib.sha256(f'{kind}|{key}'.encode()).hexdigest()[:12]


def _job_key(job_id: str) -> str:
    return f'{INGEST_JOB_PREFIX}{job_id}'


class IngestQueue:
    def __init__(self, kv: Any) -> None:
        self._kv = kv

    async def get(self, job_id: str) -> dict[str, Any] | None:
        return await self._kv.get_value(_job_key(job_id))

    async def save(self, job: dict[str, Any]) -> None:
        job['updatedAt'] = _now()
        await self._kv.set_value(_job_key(job['id']), job)

    async def delete(self, job_id: str) -> None:
        await self._kv.delete_value(_job_key(job_id))

    async def job_ids(self) -> list[str]:
        keys = await self._kv.list_keys(INGEST_JOB_PREFIX)
        return [k[len(INGEST_JOB_PREFIX):] for k in keys]

    async def is_full(self) -> bool:
        return len(await self.job_ids()) >= MAX_QUEUE_JOBS

    async def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for job_id in await self.job_ids():
            job = await self.get(job_id)
            if job and (status is None or job.get('status') == status):
                jobs.append(job)
        jobs.sort(key=lambda j: str(j.get('createdAt') or ''))
        return jobs

    async def _enqueue(self, kind: str, key: str, fields: dict[str, Any]) -> dict[str, Any]:
        job_id = job_id_for(kind, key)
        existing = await self.get(job_id)
        if existing:
            dead = existing.get('status') == STATUS_RUNNING and int(existing.get('attempts') or 0) >= MAX_JOB_ATTEMPTS
            if existing.get('status') == STATUS_FAILED or dead:
                # Explicit re-request of a failed (or abandoned mid-run) job = retry from scratch.
                existing.update({'status': STATUS_QUEUED, 'attempts': 0, 'error': None})
                await self.save(existing)
                return {'queued': True, 'duplicate': False, 'job': existing}
            return {'queued': False, 'duplicate': True, 'job': existing}
        if len(await self.job_ids()) >= MAX_QUEUE_JOBS:
            return {
                'error': 'QUEUE_FULL',
                'message': f'Ingest queue is full ({MAX_QUEUE_JOBS} jobs). Run the worker, then retry.',
            }
        job = {
            'id': job_id,
            'kind': kind,
            'status': STATUS_QUEUED,
            'attempts': 0,
            'createdAt': _now(),
            'error': None,
            **fields,
        }
        await self.save(job)
        return {'queued': True, 'duplicate': False, 'job': job}

    async def enqueue_url(
        self,
        url: str,
        *,
        source_id: str,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._enqueue(
            'url', url, {'url': url, 'sourceId': source_id, 'title': title, 'tags': list(tags or [])}
        )

    async def enqueue_actor(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        *,
        max_cost_usd: float,
        max_items: int,
    ) -> dict[str, Any]:
        """Queue a paid owned-Actor scrape. Human-triggered (CLI) only — see owned_actors."""
        key = f'{actor_id}|{json.dumps(run_input, sort_keys=True, default=str)}'
        return await self._enqueue(
            'actor',
            key,
            {
                'actor': actor_id,
                'input': run_input,
                'maxCostUsd': max_cost_usd,
                'maxItems': max_items,
            },
        )

    @staticmethod
    def is_runnable(job: dict[str, Any]) -> bool:
        """Queued jobs always; interrupted (`running`) URL jobs resume.

        An interrupted *actor* job is never auto-resumed: the paid run may already have
        started, and resuming would pay for it twice. A human re-queues it explicitly.
        """
        if int(job.get('attempts') or 0) >= MAX_JOB_ATTEMPTS:
            return False
        status = job.get('status')
        if status == STATUS_QUEUED:
            return True
        return status == STATUS_RUNNING and job.get('kind') == 'url'
