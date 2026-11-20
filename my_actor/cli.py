"""Human-run CLI for the brain (local SQLite by default).

  python -m my_actor.cli health [--repair]
  python -m my_actor.cli sync --file export.json [--plugin agentmemory] [--dry-run] [--approve]
                              [--limit 200] [--full] [--scope NAME] [--agent NAME]
  python -m my_actor.cli export --out my-memory.json [--all]
  python -m my_actor.cli move --from sqlite --to apify [--dry-run]     (this computer -> cloud)
  python -m my_actor.cli move --from apify --to sqlite [--dry-run]     (cloud -> this computer)
  python -m my_actor.cli ingest enqueue-url URL [--title T]
  python -m my_actor.cli ingest enqueue-actor OWNER/NAME --input '{"k": "v"}' --max-cost 0.5
                              [--max-items 50]
  python -m my_actor.cli ingest drain [--limit 20] [--allow-actors]
  python -m my_actor.cli ingest status
  python -m my_actor.cli hook session-start|prompt [--client claude-code]   (run by AI tools as a hook)

Paid scraping (enqueue-actor / drain --allow-actors) is CLI-only on purpose: an agent can
file memories but can never start spending money. Output is JSON on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .brain import BrainStore, get_brain, scope_tag_for
from .constants import (
    BACKEND_SQLITE,
    BACKENDS,
    ENV_BACKEND,
    MAX_DRAIN_BUDGET_USD,
    MAX_SCRAPE_COST_USD,
    MAX_SCRAPE_ITEMS,
    MAX_SYNC_WRITES,
)
from .hooks import CLIENTS, empty_output, run_hook
from .policy import DEFAULT_POLICY, parse_policy_value
from .follow import follow_add, follow_list, follow_remove, follow_sync, queue_transcripts
from .ingest import fetch_text
from .ingest_worker import drain
from .owned_actors import check_owned_actor, validate_budget
from .move import export_to_file, move_memories
from .sync_runner import sync_plugin_export
from .storage import sqlite_transaction


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, default=str, ensure_ascii=False) + '\n')


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='my_actor.cli', description='Braintwin command-line tool')
    p.add_argument('--backend', choices=BACKENDS, help=f'storage backend (default: {BACKEND_SQLITE})')
    sub = p.add_subparsers(dest='command', required=True)

    h = sub.add_parser('health', help='counts, backend and queue depth')
    h.add_argument('--repair', action='store_true', help='rebuild index/manifest/inbox from stored memories')

    s = sub.add_parser('sync', help='incrementally sync an AgentMemory (or other plugin) export file')
    s.add_argument('--file', required=True, help='path to the exported JSON')
    s.add_argument('--plugin', help='source plugin name (auto-detected when omitted)')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--approve', action='store_true', help='approve NEW records directly (trusted dump only)')
    s.add_argument('--limit', type=int, default=MAX_SYNC_WRITES, help='max writes per run')
    s.add_argument('--full', action='store_true', help='ignore the saved watermark and re-check everything')
    s.add_argument('--scope', help='file everything under scope:<name>')
    s.add_argument('--agent', help='label recorded as the filing agent')

    ex = sub.add_parser('export', help='save your whole memory to one file (a backup you can move anywhere)')
    ex.add_argument('--out', required=True, help='file to write')
    ex.add_argument('--all', action='store_true', help='also include waiting, declined and forgotten items')

    mv = sub.add_parser('move', help='copy your memory between this computer (sqlite) and the cloud (apify)')
    mv.add_argument('--from', dest='source', choices=BACKENDS, required=True)
    mv.add_argument('--to', dest='target', choices=BACKENDS, required=True)
    mv.add_argument('--dry-run', action='store_true', help='show what would be copied, write nothing')

    i = sub.add_parser('ingest', help='ingest queue and workers')
    isub = i.add_subparsers(dest='ingest_command', required=True)
    eu = isub.add_parser('enqueue-url')
    eu.add_argument('url')
    eu.add_argument('--title')
    ea = isub.add_parser('enqueue-actor', help='queue a PAID run of one of your own Actors')
    ea.add_argument('actor', help='owner/name — must be owned by the allowed owner')
    ea.add_argument('--input', default='{}', help='Actor input as a JSON object')
    ea.add_argument('--max-cost', type=float, required=True, help=f'hard USD cap, <= {MAX_SCRAPE_COST_USD}')
    ea.add_argument('--max-items', type=int, default=MAX_SCRAPE_ITEMS)
    d = isub.add_parser('drain')
    d.add_argument('--limit', type=int, default=20)
    d.add_argument('--allow-actors', action='store_true', help='also run queued paid Actor jobs (needs APIFY_TOKEN)')
    d.add_argument(
        '--max-total-cost',
        type=float,
        default=MAX_DRAIN_BUDGET_USD,
        help=f'ceiling for the summed per-job caps one drain may commit to (default {MAX_DRAIN_BUDGET_USD})',
    )
    isub.add_parser('status')

    f = sub.add_parser('follow', help='owner-only: follow YouTube channels, blogs, newsletters (free RSS/Atom)')
    fsub = f.add_subparsers(dest='follow_command', required=True)
    fa = fsub.add_parser('add', help='follow a YouTube channel (URL, @handle URL, UC id) or any blog/feed URL')
    fa.add_argument('target')
    fa.add_argument('--title')
    fa.add_argument('--auto-approve', action='store_true', help='new items skip the inbox (you trust this source)')
    fsub.add_parser('list')
    fr = fsub.add_parser('remove')
    fr.add_argument('id')
    fs = fsub.add_parser('sync', help='pull new items now (free)')
    fs.add_argument('--id')
    fs.add_argument('--limit', type=int, default=20)
    fs.add_argument('--dry-run', action='store_true')
    ft = fsub.add_parser('transcripts', help='PAID, capped: queue transcripts of recent videos via your own Actor')
    ft.add_argument('id')
    ft.add_argument('--max-cost', type=float, required=True)
    ft.add_argument('--max-videos', type=int, default=5)

    hk = sub.add_parser('hook', help='for AI coding tools: run as a hook so saving does not depend on the AI (reads hook JSON on stdin)')
    hk.add_argument('event', choices=['session-start', 'prompt'])
    hk.add_argument('--client', choices=CLIENTS, default='claude-code', help='AI tool that invoked the hook')

    hk.add_argument('--installed', action='store_true', help='honor the current native connector registration')
    for name in ('connect', 'disconnect', 'connection-status'):
        setup = sub.add_parser(name, help=f'{name} a personal automatic memory connection')
        setup.add_argument('client', choices=CLIENTS)
        if name == 'connect':
            setup.add_argument('--storage', choices=('local', 'apify'), default='local')
    sub.add_parser('connector-logout', help='remove the connector authorization from the system keyring')
    serve = sub.add_parser('serve', help='start the installed MCP connection')
    serve.add_argument('--storage', choices=('local', 'apify'), required=True)
    serve.add_argument('--database')
    serve.add_argument('--account')
    serve.add_argument('--client', choices=CLIENTS)

    ib = sub.add_parser('inbox', help='owner-only review: list, approve, reject (works in BRAINTWIN_APPROVAL=human mode)')
    ibsub = ib.add_subparsers(dest='inbox_command', required=True)
    ibsub.add_parser('list')
    ia = ibsub.add_parser('approve')
    ia.add_argument('ids', nargs='+')
    ia.add_argument('--dry-run', action='store_true')
    ir = ibsub.add_parser('reject')
    ir.add_argument('id')
    ir.add_argument('--reason')

    po = sub.add_parser('policy', help='owner-only: rules for automatic approval (show / set / off / reset)')
    posub = po.add_subparsers(dest='policy_command', required=True)
    posub.add_parser('show')
    pset = posub.add_parser('set', help='e.g. policy set min_confidence 0.85   |   policy set allowed_types preference,fact')
    pset.add_argument('key')
    pset.add_argument('value')
    posub.add_parser('off', help='turn automatic approval off (everything waits in the inbox)')
    posub.add_parser('reset', help='back to the default rules')
    posub.add_parser('review', help='what was approved automatically lately')
    return p


async def _cmd_health(brain: BrainStore, args: argparse.Namespace) -> tuple[Any, int]:
    return await brain.brain_health(include_sources=False, repair_index=args.repair), 0


async def _cmd_sync(brain: BrainStore, args: argparse.Namespace) -> tuple[Any, int]:
    path = Path(args.file).expanduser()
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError as exc:
        return {'ok': False, 'error': 'FILE_ERROR', 'message': f'Cannot read {path}: {exc}'}, 2
    scope_tag = scope_tag_for(args.scope)
    result = await sync_plugin_export(
        brain,
        raw,
        source_plugin=args.plugin,
        approve=args.approve,
        dry_run=args.dry_run,
        limit=args.limit,
        use_watermark=not args.full,
        agent=args.agent,
        extra_tags=[scope_tag] if scope_tag else [],
    )
    return result, 0 if result.get('ok') else 1


async def _cmd_export(brain: BrainStore, args: argparse.Namespace) -> tuple[Any, int]:
    result = await export_to_file(brain, Path(args.out).expanduser(), everything=args.all)
    return result, 0 if result.get('ok') else 2


async def _cmd_ingest(brain: BrainStore, args: argparse.Namespace) -> tuple[Any, int]:
    cmd = args.ingest_command
    assert brain.queue is not None
    if cmd == 'enqueue-url':
        result = await brain.ingest_enqueue(args.url, title=args.title)
        return result, 0 if result.get('ok') else 1
    if cmd == 'enqueue-actor':
        actor, error = check_owned_actor(args.actor)
        if error:
            return {'ok': False, 'error': 'ACTOR_NOT_ALLOWED', 'message': error}, 2
        error = validate_budget(args.max_cost, args.max_items)
        if error:
            return {'ok': False, 'error': 'INVALID_BUDGET', 'message': error}, 2
        try:
            run_input = json.loads(args.input)
        except json.JSONDecodeError as exc:
            return {'ok': False, 'error': 'INVALID_INPUT', 'message': f'--input is not valid JSON: {exc}'}, 2
        if not isinstance(run_input, dict):
            return {'ok': False, 'error': 'INVALID_INPUT', 'message': '--input must be a JSON object'}, 2
        queued = await brain.queue.enqueue_actor(
            actor, run_input, max_cost_usd=args.max_cost, max_items=args.max_items
        )
        return {'ok': 'error' not in queued, **queued}, 0 if 'error' not in queued else 1
    if cmd == 'drain':
        if args.allow_actors and not os.environ.get('APIFY_TOKEN'):
            return {'ok': False, 'error': 'NO_TOKEN', 'message': '--allow-actors needs APIFY_TOKEN.'}, 2
        return await drain(
            brain, limit=args.limit, allow_actors=args.allow_actors, max_total_cost_usd=args.max_total_cost
        ), 0
    jobs = await brain.queue.list_jobs()
    return {
        'ok': True,
        'count': len(jobs),
        'jobs': [
            {k: j.get(k) for k in ('id', 'kind', 'status', 'attempts', 'url', 'actor', 'error', 'createdAt')}
            for j in jobs
        ],
    }, 0


async def _cmd_follow(brain: BrainStore, args: argparse.Namespace) -> tuple[Any, int]:
    cmd = args.follow_command
    if cmd == 'add':
        result = await follow_add(brain, args.target, fetch_text, title=args.title, auto_approve=args.auto_approve)
    elif cmd == 'list':
        result = await follow_list(brain)
    elif cmd == 'remove':
        result = await follow_remove(brain, args.id)
    elif cmd == 'sync':
        result = await follow_sync(brain, fetch_text, follow_id=args.id, limit=args.limit, dry_run=args.dry_run)
    else:
        error = validate_budget(args.max_cost, args.max_videos)
        if error:
            return {'ok': False, 'error': 'INVALID_BUDGET', 'message': error}, 2
        result = await queue_transcripts(brain, args.id, max_cost_usd=args.max_cost, max_videos=args.max_videos)
    return result, 0 if result.get('ok') else 1


async def _cmd_inbox(brain: BrainStore, args: argparse.Namespace) -> tuple[Any, int]:
    """The owner's approval surface — the one path that works when agents are not allowed to approve."""
    cmd = args.inbox_command
    if cmd == 'list':
        return await brain.inbox_list(), 0
    if cmd == 'approve':
        result = await brain.inbox_approve_many(list(dict.fromkeys(args.ids)), dry_run=args.dry_run)
        return result, 0 if result.get('ok') else 1
    result = await brain.inbox_reject(args.id, reason=args.reason)
    return result, 0 if result.get('ok') else 1


async def _cmd_policy(brain: BrainStore, args: argparse.Namespace) -> tuple[Any, int]:
    """Owner-only: the rules the server uses to approve safe notes without asking."""
    cmd = args.policy_command
    if cmd == 'show':
        return {'ok': True, 'policy': await brain.get_policy()}, 0
    if cmd == 'review':
        listing = await brain.inbox_list()
        return {'ok': True, 'recentAutoApproved': listing.get('recentAutoApproved', [])}, 0
    if cmd == 'off':
        return {'ok': True, 'policy': await brain.set_policy({'mode': 'off'})}, 0
    if cmd == 'reset':
        return {'ok': True, 'policy': await brain.set_policy(dict(DEFAULT_POLICY))}, 0
    try:
        value = parse_policy_value(args.key, args.value)
    except ValueError as exc:
        return {'ok': False, 'error': 'INVALID_INPUT', 'message': str(exc)}, 2
    return {'ok': True, 'policy': await brain.set_policy({args.key: value})}, 0


async def _cmd_hook(args: argparse.Namespace) -> dict[str, Any]:
    try:
        raw = '' if sys.stdin.isatty() else sys.stdin.read(200_001)
        if len(raw) > 200_000:
            return empty_output(args.event, args.client)
        if args.installed:
            from .connections import binary, current_target
            target = current_target(args.client, binary())
            if target is None:
                return empty_output(args.event, args.client)
            storage, value = target
            if storage == 'apify':
                if args.event == 'prompt':
                    from .cloud_connector import dispatch_prompt
                    from .hooks import _payload
                    dispatch_prompt(args.client, value, _payload(raw), binary())
                return empty_output(args.event, args.client)
            os.environ['BRAINTWIN_SQLITE'] = value
        with sqlite_transaction() as (kv, audit):
            brain = BrainStore(kv=kv, audit=audit)
            await brain.init()
            brain.use_embedder(None)
            return await run_hook(brain, args.event, args.client, raw, raise_on_error=True)
    except Exception:  # noqa: BLE001 - even an unavailable database must not block a prompt
        return empty_output(args.event, args.client)


async def _run(args: argparse.Namespace) -> int:
    if args.command == 'hook':
        _emit(await _cmd_hook(args))
        return 0
    if args.command == 'move':  # two stores at once, so it does not use the shared one
        payload = await move_memories(args.source, args.target, dry_run=args.dry_run)
        _emit(payload)
        return 0 if payload.get('ok') else 2
    if args.backend:
        os.environ[ENV_BACKEND] = args.backend
    brain = await get_brain(default_backend=BACKEND_SQLITE)
    handlers = {'health': _cmd_health, 'export': _cmd_export, 'sync': _cmd_sync, 'ingest': _cmd_ingest, 'follow': _cmd_follow, 'inbox': _cmd_inbox, 'policy': _cmd_policy}
    payload, code = await handlers[args.command](brain, args)
    _emit(payload)
    return code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command in {'connect', 'disconnect', 'connection-status', 'serve', 'connector-logout'}:
        from .connections import SetupError, binary, configure, current_target
        from .connector_auth import authorize, logout
        try:
            if args.command == 'serve':
                if args.storage == 'apify' and args.account:
                    from .cloud_connector import serve
                    serve(args.account, args.client)
                elif args.storage == 'local' and args.database:
                    os.environ[ENV_BACKEND] = BACKEND_SQLITE
                    os.environ['BRAINTWIN_SQLITE'] = args.database
                    from .stdio import main as serve_local
                    serve_local()
                else:
                    raise SetupError('The connector has an incomplete storage configuration.')
                return 0
            if args.command == 'connector-logout':
                logout()
                result = {'ok': True, 'signedOut': True}
            elif args.command == 'connection-status':
                target = current_target(args.client, binary())
                result = {'client': args.client, 'configured': bool(target), 'storage': target[0] if target else None}
                if target and target[0] == 'apify':
                    from .cloud_connector import last_result
                    result['lastSave'] = last_result(args.client, target[1])
            else:
                result = configure(args.client, binary(), getattr(args, 'storage', 'local'),
                                   remove=args.command == 'disconnect', authorize=authorize)
            _emit(result)
            return 0
        except (SetupError, OSError) as exc:
            print(str(exc), file=sys.stderr)
        except Exception:
            print('Connection setup could not finish. Check your network, personal settings and system keyring.', file=sys.stderr)
        return 2
    return asyncio.run(_run(args))


if __name__ == '__main__':
    sys.exit(main())
