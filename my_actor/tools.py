"""MCP tool surface shared by the Standby server (main.py) and local stdio (stdio.py).

Same eleven verbs everywhere; only two things differ per host and are injected:
- `get_brain`: which storage backend (Apify Actor runtime vs local SQLite vs Apify API)
- `charge`: pay-per-event billing (real on Apify, a no-op locally — local use is free)

Billing rule: reads are free; a tool is charged only after it actually changed state.
Dry runs, duplicate no-ops and failures are never charged.
"""

from __future__ import annotations

import os
import re
import asyncio
import logging
import hashlib
from typing import Literal
from datetime import UTC, datetime, timedelta
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult

logger = logging.getLogger(__name__)
QUICK_FETCH_SECS = 15.0  # how long a tool call waits for a page before handing it to the worker

from .brand import env_value
from .brain import BrainStore, scope_tag_for
from . import builtin_ai
from .capture import capture_declined, extract_candidates, user_turns, worth_enriching
from .extractor import merge_candidates
from .policy import contains_secret, contains_sensitive_id, normalize_text
from .origin import Collection, PROPOSED, context_provenance
from .provided_source import file_provided_source
from .constants import (
    APPROVAL_AGENT,
    APPROVAL_AUTO,
    APPROVAL_HUMAN,
    ENV_APPROVAL,
    ENV_INGEST_MODE,
    INGEST_INLINE,
    INGEST_QUEUE,
    MAX_IMPORT_CHARS,
    MAX_SYNC_WRITES,
    VERSION,
)
from .context_pack import FENCE, build_context
from .exact_recall import by_id, by_phrase, evidence_line
from .follow import recent_followed
from .ingest import check_url_async, fetch_landing_page
from .sync_runner import sync_plugin_export

BrainGetter = Callable[[], Awaitable[BrainStore]]
Charger = Callable[[], Awaitable[None]]

INSTRUCTIONS = (
    'For work that depends on saved knowledge, call memory_context with compact=true and the project scope. '
    'Use memory_search for a quoted phrase or a memory ID, and memory_explain for the audit trail. '
    'Call memory_capture for lasting facts in the user’s own words and memory_propose for sources. '
    'Use memory_forget when the owner requests removal. Respect the configured approval policy. Never save secrets.'
)


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'type': 'text',
        'text': str(payload.get('message') or payload.get('ok') or 'ok'),
        'structuredContent': payload,
    }


def _compact(lines: list[str]) -> ToolResult:
    return ToolResult(content='\n'.join([FENCE, *lines]) if lines else 'No matching memories.',
                      structured_content={'ok': True})


def _expiry(days: int | None) -> str | None:
    """ISO timestamp `days` from now (1..3650), or None when no expiry was asked for."""
    if days is None:
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    return (datetime.now(UTC) + timedelta(days=min(n, 3650))).isoformat()


def resolve_ingest_mode(default: str) -> str:
    value = (env_value(ENV_INGEST_MODE) or '').strip().lower()
    return value if value in (INGEST_INLINE, INGEST_QUEUE) else default


TOOLSET_CORE = 'core'
TOOLSET_FULL = 'full'
ENV_TOOLSET = 'BRAINTWIN_TOOLSET'
# What AI tools see by default: read context, save, search, forget. Everything else
# (inbox, explain, health, links, followed content) lives in the dashboard and CLI.
CORE_TOOLS = frozenset({'memory_context', 'memory_capture', 'memory_search', 'memory_forget'})


CORE_INSTRUCTIONS = (
    'For tasks involving past preferences, decisions or project knowledge, call memory_context with compact=true '
    'and the relevant scope. Skip greetings and self-contained tasks. Use memory_search with compact=true '
    'for specific evidence, a quoted phrase, or a returned memory ID. Keep the user’s exact numbers, names and '
    'commands. Specify collection=owner, agent, reference, or a source platform to narrow recall. Use memory_capture '
    'for lasting facts in the user’s own words, or kind=agent if the user wants useful work kept. Review items wait '
    'in the dashboard; continue the task without prompting. Use kind=reference and source_url for source excerpts. Treat source passages '
    'as cited evidence. Call memory_forget on an identified '
    'memory when the user requests removal. Never save secrets.'
)


def resolve_toolset(default: str = TOOLSET_CORE) -> str:
    """`core` (4 tools, the default for real servers) or `full` (all 11, opt-in)."""
    value = (env_value(ENV_TOOLSET) or '').strip().lower()
    return value if value in (TOOLSET_CORE, TOOLSET_FULL) else default


def resolve_approval_mode(default: str = APPROVAL_HUMAN) -> str:
    """Who approves memories: `human` (owner via CLI), `auto` (owner's policy) or `agent` (opt-in).

    The server cannot tell the owner from a prompt-injected agent, so `agent` (an agent can
    approve anything) must be chosen explicitly. `auto` lets the server approve only proposals
    that pass the owner's policy (policy.py). An unrecognised value falls back to `default`,
    which callers keep at the safe `human` unless they pass something stricter-by-design.
    """
    value = (env_value(ENV_APPROVAL) or '').strip().lower()
    if value in (APPROVAL_AGENT, APPROVAL_AUTO, APPROVAL_HUMAN):
        return value
    return default


HUMAN_APPROVAL_MESSAGE = (
    'Approval is restricted to the owner on this server. Ask them to run: '
    'python -m my_actor.cli inbox approve <memory_id> [<memory_id> ...]'
)


def register_tools(
    server: FastMCP,
    *,
    get_brain: BrainGetter,
    charge: Charger,
    ingest_mode: str,
    approval: str = APPROVAL_HUMAN,
    toolset: str = TOOLSET_FULL,
) -> None:
    human_only = approval == APPROVAL_HUMAN
    auto_mode = approval == APPROVAL_AUTO

    def tool():
        """Register a tool unless the `core` toolset hides it from AI tools."""

        def decorate(fn):
            if toolset == TOOLSET_CORE and fn.__name__ not in CORE_TOOLS:
                return fn
            return server.tool()(fn)

        return decorate

    @tool()
    async def memory_propose(
        title: str | None = None,
        content: str | None = None,
        type: str = 'note',
        tags: list[str] | None = None,
        citations: list[dict[str, Any]] | None = None,
        confidence: float = 0.8,
        dedupe_key: str | None = None,
        approve: bool = False,
        url: str | None = None,
        fetch_content: bool = True,
        import_json: str | None = None,
        source_plugin: str | None = None,
        dry_run: bool = False,
        import_limit: int = 200,
        sync: bool = False,
        agent: str | None = None,
        scope: str | None = None,
        expires_in_days: int | None = None,
        situation: str | None = None,
        reason: str | None = None,
        source_text: str | None = None,
    ) -> dict:
        """
        Propose memory into the approval inbox (default). Modes:
        1) title+content note; 2) url: add any public web page (fetched right away and split into
        searchable passages; a slow page is queued for the background worker); 3) import_json from another agent memory plugin (AgentMemory export,
        Mem0 results, Basic Memory notes, or generic [{title,content}]).
        sync=true with import_json does an incremental sync: only new/changed records are
        written, a changed record you already approved becomes a new proposal (never a silent
        overwrite), and rejected/forgotten ones are left alone. Safe to re-run.
        agent labels who filed it; scope (e.g. a project name) lets memory_search filter by it.
        expires_in_days makes a note disappear from search after that many days (e.g. a promo).
        Always say where the note comes from, so any tool reading it later knows the context:
        situation = what you and the user were doing when this came up (one short sentence, e.g.
        "planning the March launch email"); reason = why it is worth keeping; source_text = the
        user's own words it is based on, copied exactly. These are shown back on every memory card
        under origin, with useWhen / useHow / appliesTo saying when, how and where to use it.
        By default notes and web pages are saved for good at once (result.autoApproval says yes/no
        and why); the owner can switch to reviewing first. Imports always wait for the owner.
        dry_run previews without writing.
        Charged $0.01 only when something was actually saved (never for dry runs or duplicates).
        """
        brain = await get_brain()
        brain.auto_open = brain.auto_open or auto_mode
        scope_tag = scope_tag_for(scope)
        extra_tags = [scope_tag] if scope_tag else []
        agent_label = (agent or '').strip()[:80] or None
        approval_ignored = bool(approve and human_only)
        if approval_ignored or auto_mode:
            approve = False  # human mode: propose only. auto mode: the policy decides, not the caller
        note = ' (approve=true ignored: only the owner can approve here)' if approval_ignored else ''

        def noted(result: dict[str, Any]) -> dict[str, Any]:
            if approval_ignored:
                result['approvalIgnored'] = True
                result['message'] = f"{result.get('message') or ''}{note}".strip()
            return result

        if import_json is not None and str(import_json).strip() != '':
            if len(str(import_json)) > MAX_IMPORT_CHARS:
                return _ok(
                    {
                        'ok': False,
                        'error': 'IMPORT_TOO_LARGE',
                        'message': f'import_json is larger than {MAX_IMPORT_CHARS:,} characters; split it.',
                    }
                )
            limit = min(max(int(import_limit or 200), 1), MAX_SYNC_WRITES)
            if sync:
                result = await sync_plugin_export(
                    brain,
                    import_json,
                    source_plugin=source_plugin,
                    approve=approve,
                    dry_run=dry_run,
                    limit=limit,
                    agent=agent_label,
                    extra_tags=extra_tags,
                )
                counts = result.get('counts') or {}
                wrote = sum(counts.get(k, 0) for k in ('created', 'updated', 'superseded'))
            else:
                result = await brain.import_plugin_export(
                    import_json,
                    source_plugin=source_plugin,
                    approve=approve,
                    dry_run=dry_run,
                    limit=limit,
                    extra_tags=extra_tags,
                    agent=agent_label,
                )
                wrote = int(result.get('count', 0)) - int(result.get('duplicates', 0))
            if result.get('ok') and not dry_run and wrote > 0:
                await charge()
            return _ok(noted(result))

        if url and str(url).strip():
            clean_url = url.strip()
            blocked = await check_url_async(clean_url)  # validate before registering anything
            if blocked:
                return _ok(
                    {
                        'ok': False,
                        'error': blocked,
                        'message': 'Only public http/https URLs are supported for source ingest.',
                        'url': clean_url,
                    }
                )
            if dry_run:
                return _ok(
                    {
                        'ok': True,
                        'dryRun': True,
                        'message': f'Would ingest source {clean_url}',
                        'url': clean_url,
                    }
                )
            source_tags = [*(tags or []), *extra_tags]
            ingest = None
            if ingest_mode == INGEST_QUEUE and fetch_content:
                # Hosted server: try a quick fetch now so the page is usable at once; only a slow
                # or temporarily failing page is left for the background worker.
                try:
                    async with asyncio.timeout(QUICK_FETCH_SECS):
                        ingest = await fetch_landing_page(clean_url)
                except TimeoutError:
                    ingest = None
                if ingest is None or (not ingest.get('ok') and ingest.get('error') in ('TIMEOUT', 'HTTP_ERROR')):
                    result = await brain.ingest_enqueue(clean_url, title=title, tags=source_tags)
                    if result.get('ok') and result.get('queued'):
                        result['message'] = (
                            'The page was slow to answer, so it is queued and will be fetched by itself. '
                            'You are charged only when it is actually saved.'
                        )
                    return _ok(result)
            elif fetch_content:
                ingest = await fetch_landing_page(clean_url)
            result = await brain.source_add(url=clean_url, title=title, tags=source_tags, ingest=ingest, auto=auto_mode)
            fetched = ingest is None or bool(ingest.get('ok'))
            if not result.get('ok'):
                result['message'] = result.get('message', 'failed')
            elif not fetched:
                result['message'] = (
                    f"Source registered ({result.get('source', {}).get('id')}) but the page could not be "
                    f"fetched ({ingest.get('error')}); nothing was filed. Not charged."  # type: ignore[union-attr]
                )
            else:
                src = result.get('source', {})
                if src.get('alreadyDecided'):
                    result['message'] = 'Nothing was filed: you already rejected or forgot this page. Not charged.'
                    return _ok(result)
                if ingest and src.get('filedNew') is False:
                    result['message'] = (
                        'Nothing new was filed: the page had no readable text.'
                        if not src.get('proposedMemoryId')
                        else 'Nothing new was filed: this page is already in your memory.'
                    ) + ' Not charged.'
                    return _ok(result)
                parts = src.get('passageCount')
                result['message'] = f"Page saved ({src.get('id')})" + (
                    f' with {parts} searchable passages' if parts else ''
                ) + '. It waits in the inbox; approving the page approves its passages too.' if ingest else f"Source registered ({src.get('id')})"
            if result.get('ok') and not result.get('duplicate') and fetched:
                await charge()
                if auto_mode and ingest:
                    done = result.get('source', {})
                    verdict = {'approved': bool(done.get('autoSaved')), 'reasons': done.get('autoReasons') or []}
                    result['autoApproval'] = {k: v for k, v in verdict.items() if k != 'memory'}
                    if verdict.get('approved'):
                        parts = result.get('source', {}).get('passageCount')
                        what = 'Page'
                        if ingest.get('kind') == 'youtube':
                            what = 'Video transcript' if ingest.get('hasTranscript') else 'Video title and description (no transcript was available)'
                        result['message'] = f'{what} saved and ready to search' + (f' ({parts} passages).' if parts else '.')
                    elif verdict.get('reasons') and verdict['reasons'] != ['not_automatic']:
                        result['message'] = (
                            f"Page saved, but it waits for the owner ({', '.join(verdict['reasons'])}). "
                            'It is not searchable until they approve it in the dashboard.'
                        )
            return _ok(result)

        if not title or not content:
            return _ok(
                {
                    'ok': False,
                    'error': 'INVALID_INPUT',
                    'message': (
                        'Provide title+content, url for source ingest, or import_json '
                        'from an agent memory plugin export.'
                    ),
                }
            )

        note_tags = [*(tags or []), *extra_tags]
        note_prov: dict[str, Any] = {
            **({'agent': agent_label} if agent_label else {}),
            **context_provenance(how=PROPOSED, situation=situation, reason=reason, source_text=source_text),
        }
        keep, why, rule = False, [], ''
        decided = auto_mode and not dry_run
        if decided:  # the owner's rules are applied first, so an allowed note is written once, as kept
            keep, why, rule = await brain.note_decision(
                title=str(title).strip(), content=str(content).strip(), memory_type=type or 'note',
                confidence=float(confidence), tags=note_tags, provenance=note_prov,
            )  # fmt: skip
        result = await brain.memory_save(
            title=title,
            content=content,
            memory_type=type,
            tags=note_tags,
            citations=citations,
            confidence=confidence,
            approve=approve or keep,
            dedupe_key=dedupe_key,
            dry_run=dry_run,
            provenance=({**note_prov, 'autoApproved': {'rule': rule}} if keep else note_prov) or None,
            expires_at=_expiry(expires_in_days),
        )
        mid = (result.get('memory') or {}).get('id')
        wrote = bool(result.get('ok', True)) and not dry_run and not result.get('duplicate')
        if keep:
            await brain.note_decision_settle(memory_id=mid, title=str(title), rule=rule, saved=wrote)
        result['message'] = result.get('message') or (
            f"{'Would save' if dry_run else 'Proposed'} memory {mid} "
            f"status={(result.get('memory') or {}).get('status')}"
        )
        if wrote:
            await charge()
            if decided:
                result['autoApproval'] = {'approved': keep, **({'rule': rule} if keep else {'reasons': why})}
                if keep:
                    result['message'] = f'Saved and approved automatically ({rule}): {mid}'
                else:
                    result['message'] = f'Saved {mid}; waiting for the owner ({", ".join(why)}).'
        return _ok(noted(result))

    @tool()
    async def inbox_list() -> dict:
        """
        Use when the owner asks what is waiting for their yes or no. Lists memories that were
        suggested but not kept yet. Follow with inbox_approve or inbox_reject. Free.
        """
        brain = await get_brain()
        result = await brain.inbox_list()
        result['message'] = f"{result.get('count', 0)} proposed"
        return _ok(result)

    @tool()
    async def inbox_approve(
        memory_id: str | None = None,
        memory_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Use when the owner says yes to a waiting memory. Keeps one memory_id or up to 100
        memory_ids, so every tool can find them from then on. dry_run previews without writing.
        Charged $0.01 per call that kept something.
        """
        if human_only:
            return _ok({'ok': False, 'error': 'APPROVAL_REQUIRES_HUMAN', 'message': HUMAN_APPROVAL_MESSAGE})
        brain = await get_brain()
        ids = list(memory_ids or [])
        if memory_id and str(memory_id).strip():
            ids.insert(0, str(memory_id).strip())
        uniq = list(dict.fromkeys(i for i in ids if i))  # unique, order preserved
        if not uniq:
            return _ok({'ok': False, 'error': 'INVALID_INPUT', 'message': 'memory_id or memory_ids required'})
        if auto_mode:  # the owner's policy decides each one; nothing is approved blindly
            uniq = uniq[:100]
            if dry_run:
                return _ok({'ok': True, 'dryRun': True, 'message': f'Would check {len(uniq)} against the policy'})
            approved: list[str] = []
            needs_owner: list[dict[str, Any]] = []
            for mid in uniq:
                verdict = await brain.consider_auto_approval(mid)
                if verdict.get('approved'):
                    approved.append(mid)
                else:
                    needs_owner.append({'id': mid, 'reasons': verdict.get('reasons')})
            if approved:
                await charge()
            return _ok(
                {
                    'ok': True,
                    'approved': len(approved),
                    'approvedIds': approved,
                    'needsOwner': needs_owner,
                    'message': f'{len(approved)} approved by policy, {len(needs_owner)} need the owner',
                }
            )
        if len(uniq) == 1 and not dry_run:
            result = await brain.inbox_approve(uniq[0])
            result['message'] = result.get('message') or f'Approved {uniq[0]}'
            if result.get('ok', True):
                await charge()
            return _ok(result)
        result = await brain.inbox_approve_many(uniq, dry_run=dry_run)
        if result.get('ok') and not dry_run and result.get('approved', 0) > 0:
            await charge()
        return _ok(result)

    @tool()
    async def inbox_reject(memory_id: str, reason: str | None = None) -> dict:
        """
        Use when the owner says no to a waiting memory. It leaves the waiting list and is never
        shown to AI tools. Charged $0.01.
        """
        brain = await get_brain()
        result = await brain.inbox_reject(memory_id, reason=reason)
        result['message'] = result.get('message') or f'Rejected {memory_id}'
        if result.get('ok', True):
            await charge()
        return _ok(result)

    @tool()
    async def memory_search(
        query: str,
        approved_only: bool = True,
        limit: int | None = None,
        tags: list[str] | None = None,
        statuses: list[str] | None = None,
        scope: str | None = None,
        compact: bool = False,
        offset: int = 0,
        after: str = '',
        collection: Collection | None = None,
    ) -> dict:
        """Search saved knowledge. Use compact=true for short cited facts. A quoted phrase finds exact
        wording, including inside long notes. A memory ID reads the original in pages. Exact reads
        use approved, unexpired records and optional scope. Continue with nextOffset or nextAfter.
        Ordinary queries support tag and status filters. Reads are free."""
        brain = await get_brain()
        limit = min(max(limit if limit is not None else (4 if compact else 20), 1), 50)
        needle = query.strip()
        is_id = bool(re.fullmatch(r'mem_[A-Za-z0-9_-]{1,128}', needle))
        is_phrase = len(needle) > 2 and needle.startswith('"') and needle.endswith('"')
        if (is_id or is_phrase) and not tags and not statuses:
            if is_id:
                data = await by_id(brain, needle, scope_tag_for(scope), max(0, offset), collection=collection)
            elif len(needle) > 1002:
                return _ok({'ok': False, 'error': 'QUERY_TOO_LONG', 'message': 'Use up to 1000 characters inside quotes.'})
            else:
                data = await by_phrase(brain, needle[1:-1], scope_tag_for(scope), limit, after, collection=collection)
            if not compact:
                return _ok(data)
            lines = [evidence_line(r) for r in data['results']]
            for key in ('nextOffset', 'nextAfter'):
                if key in data:
                    lines.append(f'{key}={data[key]}')
            return _compact(lines)
        if statuses:  # forgotten / rejected memories stay owner-only (CLI); agents never read them back
            statuses = [x for x in statuses if str(x).lower() not in ('decayed', 'rejected')] or None
        result = await brain.memory_search(
            query,
            approved_only=approved_only,
            statuses=statuses,
            limit=limit,
            tags=tags,
            scope=scope,
            collection=collection,
        )
        if compact:
            return _compact([evidence_line(row['memory']) for row in result['results']])
        result['message'] = f"{result.get('count', 0)} hits for {query!r}"
        return _ok(result)

    @tool()
    async def memory_explain(memory_id: str) -> dict:
        """
        Use when a card is not enough and you need the whole memory. Returns the full text,
        origin (how it was saved, by which tool, when, in which situation, why, the user's own
        words, whose words it is and how far to trust it, what it replaced, until when it holds),
        usage (when, how and where to use it) and up to three related memories on the same
        subject. Use after memory_search, memory_context or inbox_list. Free.
        """
        brain = await get_brain()
        result = await brain.memory_explain(memory_id)
        if str(((result.get('memory') or {}).get('status')) or '').lower() in ('decayed', 'rejected'):
            return _ok({'ok': False, 'error': 'OWNER_ONLY', 'message': 'This memory was forgotten or rejected; only the owner can read it (CLI).'})
        return _ok(result)

    @tool()
    async def memory_forget(memory_id: str, reason: str | None = None) -> dict:
        """
        Use when the owner says something is wrong, out of date or should be forgotten. The
        memory stops showing up for AI tools; the owner can still see it in their history.
        Charged $0.01.
        """
        brain = await get_brain()
        result = await brain.memory_forget(memory_id, reason=reason)
        result['message'] = result.get('message') or f'Decayed {memory_id}'
        if result.get('ok', True) and result.get('changed', True):
            await charge()
        return _ok(result)

    @tool()
    async def brain_health(
        include_sources: bool = True,
        remove_source_id: str | None = None,
        repair_index: bool = False,
    ) -> dict:
        """
        Use when memory seems broken or the owner asks how much is stored. Returns counts of
        kept, waiting and forgotten memories, where they are stored, pages still being read,
        and followed sources.
        remove_source_id drops a source; repair_index rebuilds the index/manifest/inbox from the
        stored memories (use if two agents wrote at once and a memory looks missing). Free.
        """
        brain = await get_brain()
        result = await brain.brain_health(
            include_sources=include_sources,
            remove_source_id=remove_source_id,
            repair_index=repair_index,
        )
        result['approvalMode'] = approval
        result['message'] = (
            f"memories={result.get('memoryTotal', 0)} inbox={result.get('inboxCount', 0)} "
            f"sources={result.get('sourceCount', 0)} queue={result.get('ingestQueueJobs', 0)} "
            f"backend={result.get('backend')} approval={approval}"
        )
        return _ok(result)

    @tool()
    async def recent_followed_content(days: int = 7, limit: int = 20, follow_id: str | None = None) -> dict:
        """
        What the owner follows (YouTube channels, blogs, newsletters), newest first: titles,
        short summaries and links of items from the last `days` days. Answers "what did I
        read or watch lately?". Use memory_search with tags=['followed'] to search inside it.
        Only the owner can add or remove followed sources (CLI). Free.
        """
        brain = await get_brain()
        result = await recent_followed(brain, days=days, limit=limit, follow_id=follow_id)
        result['message'] = f"{result.get('count', 0)} followed item(s) in the last {days} day(s)"
        return _ok(result)

    @tool()
    async def memory_context(
        query: str,
        token_budget: int | None = None,
        scope: str | None = None,
        include_followed: bool | None = None,
        max_items: int | None = None,
        compact: bool = False,
        collection: Collection | None = None,
    ) -> dict:
        """Recall facts for a task. Use compact=true for one short cited block. Set scope for a project.
        token_budget estimates the text budget. Followed feeds are optional in compact mode."""
        brain = await get_brain()
        result = await build_context(
            brain,
            query,
            token_budget=token_budget if token_budget is not None else (400 if compact else 1500),
            scope=scope,
            include_followed=(not compact if include_followed is None else include_followed),
            max_items=max_items if max_items is not None else (4 if compact else 8),
            targeted=compact,
            collection=collection,
        )
        if compact:
            return ToolResult(content=result['context'] or 'No matching memories.', structured_content={'count': result['count'], 'truncated': result['truncated']})
        result['message'] = f"{result.get('count', 0)} item(s), about {result.get('approxTokens', 0)} tokens"
        return _ok(result)

    @tool()
    async def memory_capture(
        text: str,
        scope: str | None = None,
        agent: str | None = None,
        dry_run: bool = False,
        situation: str | None = None,
        kind: Literal['user', 'agent', 'reference'] = 'user',
        source_url: str | None = None,
    ) -> dict:
        """
        Hand over what the USER said in the conversation (their own words, not your reply). Lasting
        preferences, decisions and facts about them are found by built-in rules plus an AI step
        (the server's built-in one, or the owner's own model), saved, and kept by the owner's rules.
        Safe to call after every user turn: duplicates are ignored and secrets are held back.
        situation = one short sentence on what you and the user were working on; it is stored with
        each saved fact, next to the user's own words, so a tool reading it later knows the context.
        kind=agent proposes scoped work. kind=reference files supplied excerpts with source_url. Both wait for review.
        dry_run lists what the rules alone would save. Charged $0.01 once per call that saved
        something; the built-in AI step adds its own small charge only when it found a fact.
        """
        brain = await get_brain()
        if kind == 'reference':
            result = await file_provided_source(
                brain, url=source_url or '', text=text, scope=scope, dry_run=dry_run,
            )
            if result.get('saved'):
                await charge()
            return _ok(result)
        if kind == 'agent':
            scope_tag = scope_tag_for(scope)
            note = text.strip()
            normalized = normalize_text(note[:8000])
            if not scope_tag or not 12 <= len(note) <= 4000:
                return _ok({'ok': False, 'saved': 0, 'message': 'Agent work needs a project scope and 12–4,000 characters.'})
            if capture_declined(note) or contains_secret(normalized) or contains_sensitive_id(normalized):
                return _ok({'ok': True, 'saved': 0, 'message': 'Skipped a private or sensitive work note.'})
            title = ' '.join(note.split())[:100]
            fingerprint = hashlib.sha256(f'{scope_tag}\0{note.casefold()}'.encode()).hexdigest()
            saved = await brain.memory_save(
                title=title, content=note, memory_type='agent_work', tags=[scope_tag], approve=False,
                provenance={**context_provenance(how=PROPOSED, situation=situation), 'agent': (agent or '')[:80],
                            'reviewReasons': ['agent_work']},
                dedupe_key=f'agent-work:{fingerprint}', dry_run=dry_run,
            )
            changed = bool(saved.get('ok') and not saved.get('duplicate') and not dry_run)
            if changed:
                await charge()
            mid = (saved.get('memory') or {}).get('id')
            return _ok({'ok': bool(saved.get('ok')), 'dryRun': dry_run, 'saved': int(changed), 'requiresReview': True,
                        'items': [{'id': mid, 'type': 'agent_work', 'approved': False}] if changed else [],
                        'message': 'Saved for review.' if changed else 'No new work note was filed.'})
        turns = user_turns(text)
        if turns is not None:
            text = '\n'.join(turns)
        normalized = normalize_text(text)
        sensitive = contains_secret(normalized) or contains_sensitive_id(normalized)
        if capture_declined(text):
            return _ok({'ok': True, 'found': 0, 'saved': 0, 'message': 'Skipped at the user’s request.'})
        found = [row for row in extract_candidates(text)
                 if not (contains_secret(normalize_text(row['content'])) or contains_sensitive_id(normalize_text(row['content'])))]
        enrich = not dry_run and not sensitive and worth_enriching(text, found)
        smart_used = False
        if enrich and brain.extractor is not None:
            try:  # the user's own model; the rules above stand alone if it fails or is slow
                found = merge_candidates(found, await brain.extractor.extract(text))
                smart_used = True
            except Exception as exc:  # noqa: BLE001 - any provider failure falls back to the rules
                logger.warning('smart capture failed: %s: %s', type(exc).__name__, str(exc)[:200])
        elif enrich and builtin_ai.provider is not None:
            # No key of their own: the built-in AI helps, at its own small price, only when it finds something.
            extra, ai_used = await builtin_ai.provider.find(brain, text, found)
            if ai_used:
                found = [*found, *extra]
                smart_used = True
        if not found:
            return _ok({'ok': True, 'found': 0, 'saved': 0, 'message': 'Nothing worth remembering found.'})
        if dry_run:
            return _ok({'ok': True, 'dryRun': True, 'found': len(found), 'candidates': found})
        scope_tag = scope_tag_for(scope)
        label = (agent or '').strip()[:80] or None
        result = await brain.save_captured(
            found, tags=[scope_tag] if scope_tag else [], agent=label, auto=auto_mode,
            situation=situation, source_text=None if sensitive else text,
        )
        saved: list[dict[str, Any]] = result['items']
        already, failed = result['already'], result['failed']
        if saved:
            await charge()  # one tool-call for the whole call, however many facts it saved
        auto_count = sum(1 for r in saved if r.get('approved'))
        return _ok(
            {
                'ok': True,
                'found': len(found),
                'saved': len(saved),
                'smartCapture': smart_used,
                'alreadyKnown': already,
                'failed': failed,
                'autoApproved': auto_count,
                'items': saved,
                'message': f'{len(saved)} saved, {auto_count} approved automatically, {len(saved) - auto_count} waiting'
                + (f', {failed} could not be saved' if failed else ''),
            }
        )

    @server.resource(uri='braintwin://about', name='braintwin-about')
    def about() -> str:
        """Describe this personal-memory MCP server (not official Apify MCP)."""
        if toolset == TOOLSET_CORE:
            return f'Braintwin v{VERSION}. Four tools. {CORE_INSTRUCTIONS}'
        return (
            f'Braintwin (Personal Memory) v{VERSION} — community Actor by khadinakbar. '
            'This is NOT the official Apify MCP server (mcp.apify.com). '
            'Eleven tools: memory_context (start here), memory_propose (notes|url|plugin import_json|incremental sync), '
            'memory_capture (find lasting facts in what the user said), inbox_list, inbox_approve (batch), '
            'inbox_reject, memory_search (find one specific thing), memory_explain (full text and related memories), memory_forget, '
            'brain_health, recent_followed_content (what the owner follows). Reads are free; only saves/approvals are '
            'charged on Apify. Every memory card says what it is (kind), when and how to use it (useWhen, useHow), where it applies (appliesTo) and where it came from (origin). When you save, pass situation, reason and source_text so the next tool knows the context. '
            'A memory is waiting, kept, declined or forgotten; AI tools only see kept ones.'
        )


def build_server(
    *,
    get_brain: BrainGetter,
    charge: Charger,
    ingest_mode: str,
    approval: str | None = None,
    toolset: str = TOOLSET_FULL,
) -> FastMCP:
    mode = approval or resolve_approval_mode()
    instructions = (CORE_INSTRUCTIONS if toolset == TOOLSET_CORE else INSTRUCTIONS) + (
        ' Approval is owner-only on this server: you can propose, but only the owner can approve.'
        if mode == APPROVAL_HUMAN
        else (
            " The owner's policy approves safe notes automatically; check result.autoApproval and"
            ' never retry a refused one - it waits for the owner.'
            if mode == APPROVAL_AUTO
            else ''
        )
    )
    server = FastMCP('braintwin', instructions=instructions, version=VERSION)
    register_tools(server, get_brain=get_brain, charge=charge, ingest_mode=ingest_mode, approval=mode, toolset=toolset)
    return server
