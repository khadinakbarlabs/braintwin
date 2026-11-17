"""Owned-Actor scrape bridge: allowlist, budget guard, dataset-to-memory mapping.

Callers: my_actor/ingest_worker.py, my_actor/cli.py, tests/test_owned_actors.py.

Policy (Apify Store integrity): scraping jobs may only run Actors owned by the configured
owner (default `khadinakbar`). Other creators' Actors are refused outright, never wrapped.
Paid runs are human-triggered through the CLI — deliberately *not* an MCP tool, so an
agent can never start spending money on its own. Every run carries a hard cost cap.

Data shape in (Apify dataset item, synthetic): {"url": "https://example.com/p/1",
"title": "Post", "text": "body", "scrapedAt": "2026-11-09T12:00:00Z"}.
Data shape out: memory_save kwargs with status always `proposed` (never auto-approved).
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .constants import (
    DEFAULT_OWNED_ACTOR_OWNER,
    ENV_OWNED_ACTOR_OWNER,
    MAX_SCRAPE_COST_USD,
    MAX_SCRAPE_ITEMS,
)

_ACTOR_RE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9_.-]*)[/~]([A-Za-z0-9][A-Za-z0-9_.-]*)$')
_TEXT_KEYS = ('text', 'content', 'description', 'caption', 'body', 'selftext', 'summary', 'bio')
_TITLE_KEYS = ('title', 'name', 'headline', 'fullName')
_URL_KEYS = ('url', 'postUrl', 'link', 'profileUrl', 'permalink')
RUN_TIMEOUT = timedelta(minutes=15)


def owned_owner() -> str:
    return (os.environ.get(ENV_OWNED_ACTOR_OWNER) or DEFAULT_OWNED_ACTOR_OWNER).strip().lower()


def check_owned_actor(raw: Any) -> tuple[str | None, str | None]:
    """Return (canonical 'owner/name', None) or (None, error message)."""
    match = _ACTOR_RE.match(str(raw or '').strip())
    if not match:
        return None, 'Actor id must look like owner/name.'
    owner, name = match.group(1), match.group(2)
    # Exact owner equality — a prefix match would let `khadinakbar-evil/x` through.
    if owner.lower() != owned_owner():
        return None, f'Only Actors owned by {owned_owner()!r} may be run (refused {owner}/{name}).'
    return f'{owner}/{name}', None


def validate_budget(max_cost_usd: Any, max_items: Any) -> str | None:
    try:
        cost = float(max_cost_usd)
        items = int(max_items)
    except (TypeError, ValueError):
        return 'max_cost_usd and max_items must be numbers.'
    if not 0 < cost <= MAX_SCRAPE_COST_USD:
        return f'max_cost_usd must be > 0 and <= {MAX_SCRAPE_COST_USD}.'
    if not 1 <= items <= MAX_SCRAPE_ITEMS:
        return f'max_items must be between 1 and {MAX_SCRAPE_ITEMS}.'
    return None


def _first_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ''


def item_to_memory(item: Any, actor_id: str, run_id: str | None = None) -> dict[str, Any] | None:
    """Map one dataset item to memory_save kwargs; None when it has no usable text."""
    if not isinstance(item, dict):
        return None
    content = _first_text(item, _TEXT_KEYS)
    if not content:
        return None
    title = _first_text(item, _TITLE_KEYS) or (content[:80] + ('…' if len(content) > 80 else ''))
    url = _first_text(item, _URL_KEYS)
    digest = hashlib.sha256((url or content).encode('utf-8')).hexdigest()[:16]
    return {
        'title': title[:500],
        'content': content[:50_000],
        'memory_type': 'note',
        'tags': ['scrape', f'actor:{actor_id.split("/")[-1]}'],
        'citations': [{'url': url, 'sourceActor': actor_id}] if url else [],
        'confidence': 0.5,
        'dedupe_key': f'scrape:{actor_id}:{digest}',
        'provenance': {'sourceActor': actor_id, 'runId': run_id},
    }


def items_to_memories(items: list[Any], actor_id: str, *, limit: int, run_id: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        mem = item_to_memory(item, actor_id, run_id)
        if mem:
            out.append(mem)
        if len(out) >= limit:
            break
    return out


async def run_owned_actor(
    actor_id: str,
    run_input: dict[str, Any],
    *,
    max_cost_usd: float,
    max_items: int,
    token: str | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Run one owned Actor with a hard cost cap and return its dataset items.

    Re-validates the allowlist and budget here so no caller can bypass them.
    """
    canonical, error = check_owned_actor(actor_id)
    if error:
        return {'ok': False, 'error': 'ACTOR_NOT_ALLOWED', 'message': error}
    budget_error = validate_budget(max_cost_usd, max_items)
    if budget_error:
        return {'ok': False, 'error': 'INVALID_BUDGET', 'message': budget_error}
    if client is None:
        token = token or os.environ.get('APIFY_TOKEN')
        if not token:
            return {'ok': False, 'error': 'NO_TOKEN', 'message': 'APIFY_TOKEN is required to run an Actor.'}
        from apify_client import ApifyClientAsync  # lazy

        client = ApifyClientAsync(token)

    run = await client.actor(canonical).call(
        run_input=run_input,
        max_total_charge_usd=Decimal(str(max_cost_usd)),
        max_items=int(max_items),
        run_timeout=RUN_TIMEOUT,
    )
    if run is None:
        return {'ok': False, 'error': 'NO_RUN', 'message': 'Actor call returned no run.'}
    status = str(getattr(run, 'status', None) or '')
    run_id = getattr(run, 'id', None)
    cost = getattr(run, 'usage_total_usd', None)
    if 'SUCCEEDED' not in status.upper():
        return {
            'ok': False,
            'error': 'RUN_NOT_SUCCEEDED',
            'message': f'Actor run finished with status {status or "unknown"}.',
            'runId': run_id,
            'costUsd': float(cost) if cost is not None else None,
        }
    page = await client.dataset(run.default_dataset_id).list_items(limit=int(max_items))
    return {
        'ok': True,
        'runId': run_id,
        'status': status,
        'costUsd': float(cost) if cost is not None else None,
        'items': list(getattr(page, 'items', None) or []),
    }
