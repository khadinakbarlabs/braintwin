"""Talk to the same authenticated Braintwin gateway as the dashboard."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from .connections import SetupError, put_file, state_dir
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


def serve(account):
    from fastmcp.server import create_proxy
    from .tools import CORE_INSTRUCTIONS
    create_proxy(remote_client(account), name='Braintwin', instructions=CORE_INSTRUCTIONS).run(transport='stdio', show_banner=False)


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


async def handle_prompt(client, owner, data, *, send=send_capture):
    candidates = prompt_candidates(client, data)
    if not candidates:
        return None
    _, scope = _workspace(data)
    args = {'text': '\n'.join(row['content'] for row in candidates), 'agent': client + '-hook', 'kind': 'user'}
    if scope:
        args['scope'] = scope
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
