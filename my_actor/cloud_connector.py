"""Talk to the same authenticated Braintwin gateway as the dashboard."""
from __future__ import annotations

import asyncio
import json
import hashlib
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime

from .connections import SetupError, locked, put_file, state_dir
from .hooks import _workspace, prompt_candidates

GATEWAY = 'https://khadinakbar--braintwin.apify.actor/mcp'


def remote_client(account):
    import httpx2
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server.providers.proxy import ProxyClient
    from .connector_auth import token_for

    class AccountAuth(httpx2.Auth):
        async def async_auth_flow(self, request):
            # The stable gateway is the only bearer destination. Redirects must never inherit it.
            if request.url.scheme != 'https' or request.url.host != 'khadinakbar--braintwin.apify.actor' or request.url.port not in (None, 443):
                raise SetupError('Unexpected Apify destination.')
            request.headers['Authorization'] = 'Bearer ' + await asyncio.to_thread(token_for, account)
            yield request

    def fixed_destination(**options):
        options['follow_redirects'] = False
        return httpx2.AsyncClient(**options)

    transport = StreamableHttpTransport(GATEWAY, auth=AccountAuth(), httpx_client_factory=fixed_destination)
    return ProxyClient(transport, forward_incoming_headers=False, mode='legacy', timeout=18)


def serve(account, client_name=None):
    from fastmcp.server import create_proxy
    from .tools import CORE_INSTRUCTIONS
    from .native_tools import NativeMemoryResults
    instructions = CORE_INSTRUCTIONS
    if client_name:
        instructions += (' This native installation checks submitted user messages for lasting preferences and decisions. '
                         'Let the prompt hook capture those statements. Continue the conversation quietly. '
                         'Use the capture tool for explicit requests or additional facts the hook does not handle.')
    server = create_proxy(remote_client(account), name='Braintwin', instructions=instructions)
    server.add_middleware(NativeMemoryResults(account, client_name))
    server.run(transport='stdio', show_banner=False)


async def send_capture(owner, arguments):
    async with asyncio.timeout(22):
        async with remote_client(owner) as client:
            reply = await client.call_tool('memory_capture', arguments)
    data = reply.structured_content
    if isinstance(data, dict) and isinstance(data.get('structuredContent'), dict):
        data = data['structuredContent']
    if not isinstance(data, dict) or data.get('ok') is not True or type(data.get('saved')) is not int:
        raise ValueError('No save receipt')
    return data


def capture_job(client, data):
    candidates = prompt_candidates(client, data)
    if not candidates:
        return None
    _, scope = _workspace(data)
    job = {'text': '\n'.join(row['content'] for row in candidates), 'agent': client + '-hook', 'kind': 'user'}
    if scope:
        job['scope'] = scope
    return job


def _fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _marker(client, owner, scope):
    return state_dir() / ('turn-' + _fingerprint(json.dumps([client, owner, scope])) + '.json')


def mark_automatic(client, owner, job, outcome, turn=None):
    scope = job.get('scope')
    if not scope:
        return True
    path = _marker(client, owner, scope)
    now = time.time()
    with locked('capture-status'):
        recent = json.loads(path.read_text()) if path.exists() else {}
        recent = {digest: row for digest, row in recent.items() if 0 <= now - row['time'] < 90}
        prior = recent.get(_fingerprint(job['text'].strip()), {})
        if outcome == 'pending' and prior.get('outcome') in {'pending','saved','waiting','already-known','unconfirmed'}:
            return False
        for sentence in [job['text'], *job['text'].splitlines()]:
            digest = _fingerprint(sentence.strip())
            if outcome == 'pending' or recent.get(digest, {}).get('turn') == turn:
                recent[digest] = {'turn': turn, 'time': now, 'outcome': outcome}
        put_file(path, json.dumps(recent).encode())
    return True


def automatic_status(client, owner, scope, text):
    if not client or not scope or not isinstance(text, str):
        return None
    try:
        digest = _fingerprint(text.strip())
        row = json.loads(_marker(client, owner, scope).read_text())[digest]
        if (0 <= time.time() - row['time'] < 90
                and row['outcome'] in {'pending','saved','waiting','already-known','unconfirmed'}):
            return row['outcome']
    except (OSError, ValueError, KeyError):
        pass
    return None


def dispatch_prompt(client, owner, data, cli):
    job = capture_job(client, data)
    if not job:
        return
    turn = uuid.uuid4().hex
    packet = json.dumps([client, owner, job, turn, str(cli)]).encode()
    if not mark_automatic(client, owner, job, 'pending', turn):
        return
    try:
        process = subprocess.Popen([sys.executable, '-I', '-m', 'my_actor.capture_worker'],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        with process.stdin as pipe:
            pipe.write(packet)
    except OSError:
        mark_automatic(client, owner, job, 'failed', turn)
        record_result(client, owner, {'outcome':'unconfirmed'})


async def handle_prompt(client, owner, data, *, send=send_capture):
    args = capture_job(client, data)
    return await deliver(owner, args, send) if args else None


async def deliver(owner, args, send=send_capture):
    try:
        result = await send(owner, args)
        amount, approved = result['saved'], result.get('autoApproved', 0)
        outcome = ('partial' if result.get('failed') else 'waiting' if amount > approved else
                   'saved' if amount else 'already-known' if result.get('alreadyKnown') else 'skipped')
        return {'outcome': outcome, 'saved': amount, 'approved': approved}
    except Exception:
        # A timeout can follow a committed write. Never replay a possibly charged save.
        return {'outcome': 'unconfirmed'}


def record_result(client, owner, receipt):
    path = state_dir() / (client + '-last-save.json')
    put_file(path, json.dumps({**receipt, 'account': owner, 'at': datetime.now(UTC).isoformat()}).encode())


def last_result(client, owner):
    path = state_dir() / (client + '-last-save.json')
    if path.is_file():
        result = json.loads(path.read_text())
        if result.get('account') == owner:
            return {k: v for k, v in result.items() if k != 'account'}
    return None
