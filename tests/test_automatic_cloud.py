"""The user's consent, destination and text boundaries for an automatic hosted connection."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from my_actor.connections import configure, current_target


class CloudInstallTests(unittest.TestCase):
    def test_automatic_cloud_tools_and_callback_bind_to_the_same_account_without_credentials_in_config(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'HOME': folder, 'BRAINTWIN_HOME': folder + '/storage'}):
            cli = Path(folder) / 'bin/braintwin-cli'
            cli.parent.mkdir()
            cli.touch()
            path = Path(folder) / '.cursor/mcp.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'mcpServers': {'braintwin': {'url': 'https://mcp.apify.com/?tools=khadinakbar/braintwin'}}}))
            configure('cursor', cli, 'apify', authorize=lambda _: 'Owner123')
            self.assertEqual(current_target('cursor', cli), ('apify', 'Owner123'))
            self.assertIn('--installed', (path.parent / 'hooks.json').read_text())
            events = json.loads((path.parent / 'hooks.json').read_text())['hooks']
            self.assertNotIn('sessionStart', events)
            seen = []
            configure('cursor', cli, 'apify', authorize=lambda owner: seen.append(owner) or owner)
            self.assertEqual(seen, ['Owner123'])
            previous = path.read_bytes()
            self.assertRaises(ValueError, configure, 'cursor', cli, 'local')
            self.assertEqual(path.read_bytes(), previous)

    def test_upgrade_from_gemini_native_http_registration_needs_no_manual_removal(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'HOME':folder, 'BRAINTWIN_HOME':folder + '/store'}):
            settings=Path(folder) / '.gemini/settings.json'
            settings.parent.mkdir()
            settings.write_text(json.dumps({'mcpServers': {'braintwin': {'httpUrl':'https://mcp.apify.com/?tools=khadinakbar/braintwin'}, 'other':{'command':'different'}}}))
            cli=Path(folder) / 'bin/braintwin-cli'
            configure('gemini-cli', cli, 'apify', authorize=lambda _: 'Owner123')
            self.assertEqual(current_target('gemini-cli', cli), ('apify', 'Owner123'))
            self.assertEqual(json.loads(settings.read_text())['mcpServers']['other'], {'command':'different'})

    def test_braintwin_dev_address_from_the_website_guide_is_replaced_without_manual_removal(self):
        for url in ('https://braintwin.dev/mcp/', 'https://braintwin.dev/mcp'):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'HOME': folder, 'BRAINTWIN_HOME': folder + '/store'}):
                config = Path(folder) / '.claude.json'
                config.write_text(json.dumps({'mcpServers': {'braintwin': {'type': 'http', 'url': url}, 'other': {'command': 'x'}}}))
                cli = Path(folder) / 'bin/braintwin-cli'
                configure('claude-code', cli, 'apify', authorize=lambda _: 'Owner123')
                self.assertEqual(current_target('claude-code', cli), ('apify', 'Owner123'))
                saved = json.loads(config.read_text())['mcpServers']
                self.assertNotIn('url', saved['braintwin'])
                self.assertEqual(saved['other'], {'command': 'x'})
                hooks = json.loads((Path(folder) / '.claude/settings.json').read_text())['hooks']
                self.assertIn('UserPromptSubmit', hooks)

    def test_settings_rewritten_by_a_running_agent_during_sign_in_are_merged_not_failed(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'HOME': folder, 'BRAINTWIN_HOME': folder + '/store'}):
            config = Path(folder) / '.claude.json'
            config.write_text(json.dumps({'mcpServers': {}, 'numStartups': 1}))
            cli = Path(folder) / 'bin/braintwin-cli'
            calls = []
            def sign_in(_):
                calls.append(1)
                if len(calls) == 1:  # the agent saves its own settings while the browser sign-in is open
                    config.write_text(json.dumps({'mcpServers': {}, 'numStartups': 2}))
                return 'Owner123'
            configure('claude-code', cli, 'apify', authorize=sign_in)
            saved = json.loads(config.read_text())
            self.assertEqual(saved['numStartups'], 2)
            self.assertEqual(current_target('claude-code', cli), ('apify', 'Owner123'))
            self.assertEqual(len(calls), 2)

    def test_a_different_user_server_named_braintwin_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'HOME': folder, 'BRAINTWIN_HOME': folder + '/store'}):
            config = Path(folder) / '.claude.json'
            config.write_text(json.dumps({'mcpServers': {'braintwin': {'type': 'http', 'url': 'https://example.com/mcp'}}}))
            before = config.read_bytes()
            self.assertRaises(ValueError, configure, 'claude-code', Path(folder) / 'bin/braintwin-cli', 'apify', authorize=lambda _: 'Owner123')
            self.assertEqual(config.read_bytes(), before)

    def test_device_login_waits_for_consent_and_checks_owner_before_using_tokens(self):
        from my_actor import connector_auth as auth
        calls = []
        grant = [{'error': 'authorization_pending'}, {'error': 'slow_down'}, {'access_token': 'test-access', 'refresh_token': 'test-refresh', 'expires_in': 3600}]
        def route(req):
            calls.append((req.url.host, req.url.path))
            if req.url.path.endswith('/devices'):
                return httpx.Response(200, json={'device_code': 'private-code', 'user_code': 'ABCD', 'verification_uri_complete': 'https://console.apify.com/device?user_code=ABCD', 'expires_in': 300, 'interval': 1})
            if req.url.path.endswith('/token'):
                self.assertIn(auth.CLIENT_ID.encode().replace(b':', b'%3A').replace(b'/', b'%2F'), req.content)
                result = grant.pop(0)
                return httpx.Response(400 if 'error' in result else 200, json=result)
            self.assertEqual(req.headers['authorization'], 'Bearer test-access')
            return httpx.Response(200, json={'data': {'id': 'Owner123'}})
        sleeps, urls = [], []
        with httpx.Client(transport=httpx.MockTransport(route)) as http:
            owner, credentials = auth.device_login(http, show=lambda url, code: urls.append((url, code)), wait=sleeps.append)
        self.assertEqual(owner, 'Owner123')
        self.assertEqual(credentials['refresh_token'], 'test-refresh')
        self.assertGreater(sleeps[-1], sleeps[0])
        self.assertEqual(len(urls), 1)
        self.assertEqual(calls[-1], ('api.apify.com', '/v2/users/me'))

    def test_reconnecting_checks_for_revoked_access_before_reusing_cached_token(self):
        from my_actor import connector_auth as auth
        class Vault:
            saved = None
            def set_password(self, service, owner, data):
                self.saved = owner
        vault = Vault()
        def route(req):
            return httpx.Response(401, json={'error':'expired'})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'BRAINTWIN_HOME':folder}), patch.object(auth, 'token_for', return_value='revoked-fixture'), patch.object(auth, '_vault', return_value=vault), patch.object(auth, '_client', side_effect=lambda: httpx.Client(transport=httpx.MockTransport(route))), patch.object(auth, 'device_login', return_value=('Owner123', {'access_token':'fresh-fixture'})):
            self.assertEqual(auth.authorize('Owner123'), 'Owner123')
        self.assertEqual(vault.saved, 'Owner123')

    def test_refresh_for_wrong_owner_never_replaces_an_existing_grant(self):
        from my_actor import connector_auth as auth
        class Vault:
            def get_password(self, service, account):
                return json.dumps({'access_token': 'old-fixture', 'refresh_token': 'fixture-refresh', 'expires_at': 0})
            def set_password(self, *args):
                self.fail_write = True
        vault = Vault()
        def route(req):
            if req.url.path.endswith('/token'):
                return httpx.Response(200, json={'access_token':'replacement-fixture','refresh_token':'rotated-fixture','expires_in':3600})
            return httpx.Response(200, json={'data': {'id':'DifferentOwner'}})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'BRAINTWIN_HOME':folder}), patch.object(auth, '_vault', return_value=vault), patch.object(auth, '_client', return_value=httpx.Client(transport=httpx.MockTransport(route))):
            with self.assertRaisesRegex(ValueError, 'different account'):
                auth.token_for('Owner123')
        self.assertFalse(hasattr(vault, 'fail_write'))

    def test_denied_grant_and_unexpected_verification_host_do_not_become_accounts(self):
        from my_actor import connector_auth as auth
        for url in ('https://console.apify.com/device', 'https://unexpected.example/device'):
            def route(req):
                if req.url.path.endswith('/devices'):
                    return httpx.Response(200, json={'device_code':'private-code','user_code':'ABCD','verification_uri':url,'expires_in':300,'interval':1})
                return httpx.Response(400, json={'error':'access_denied'})
            opened = []
            with httpx.Client(transport=httpx.MockTransport(route)) as http:
                with self.assertRaises(ValueError):
                    auth.device_login(http, show=lambda link, _: opened.append(link), wait=lambda _: None)
            self.assertEqual(bool(opened), url.startswith('https://console.apify.com/'))


class CloudEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_and_capture_share_the_authenticated_protocol_client(self):
        from fastmcp import Client
        from fastmcp.server import create_proxy
        from my_actor.cloud_connector import remote_client
        client = remote_client('Owner123')
        self.assertIsInstance(client, Client)
        self.assertEqual(str(client.transport.url), 'https://khadinakbar--braintwin.apify.actor/mcp')
        self.assertIsNotNone(create_proxy(client, name='Braintwin'))

    async def test_only_a_matching_sentence_reaches_the_account_and_status_reports_pending(self):
        from my_actor.cloud_connector import handle_prompt
        calls = []
        async def send(owner, args):
            calls.append((owner, args))
            return {'ok': True, 'saved': 1, 'autoApproved': 0, 'failed': 0}
        raw = {'hook_event_name':'UserPromptSubmit', 'prompt': 'Please open my project today. I prefer weekly status reports on Fridays.', 'cwd':'/documents/fixture'}
        receipt = await handle_prompt('codex', 'Owner123', raw, send=send)
        self.assertEqual(receipt['outcome'], 'waiting')
        self.assertEqual(calls[0][0], 'Owner123')
        self.assertIn('Fridays', calls[0][1]['text'])
        self.assertNotIn('open my project', calls[0][1]['text'])
        self.assertNotIn('/documents', json.dumps(calls))
        self.assertNotIn('Fridays', json.dumps(receipt))
        for excluded in ("I prefer e-books. Do not save this.", 'I prefer tea. My password is fake-demo-value.', 'Assistant: I prefer silly hats.', 'Thanks!'):
            self.assertIsNone(await handle_prompt('codex','Owner123',dict(raw, prompt=excluded), send=send))
        self.assertEqual(len(calls), 1)

    async def test_uncertain_write_is_reported_without_replaying_the_turn(self):
        from my_actor.cloud_connector import handle_prompt
        seen = []
        async def timeout(owner, request):
            seen.append(1)
            raise TimeoutError
        receipt = await handle_prompt('cursor', 'Owner123', {'prompt':'I prefer biking to the office.'}, send=timeout)
        self.assertEqual(receipt['outcome'], 'unconfirmed')
        self.assertEqual(len(seen), 1)


if __name__ == '__main__':
    unittest.main()
