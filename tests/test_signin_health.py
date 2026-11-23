"""A cancelled Apify sign-in must be visible to the user, and copies must not race each other."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


class Vault:
    """In-memory keyring."""

    def __init__(self, creds):
        self.creds, self.writes = creds, []

    def get_password(self, service, account):
        return json.dumps(self.creds) if self.creds else None

    def set_password(self, service, account, data):
        self.writes.append(account)
        self.creds = json.loads(data)


def _route(status, body, on_token=None):
    def route(req):
        if req.url.path.endswith('/token'):
            if on_token:
                on_token()
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={'data': {'id': 'Owner123'}})
    return route


class SignInHealthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.locks = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'BRAINTWIN_HOME': self.folder.name})
        self.env.start()
        from my_actor import signin_health
        self.lockdir = patch.object(signin_health, '_lock_dir', return_value=Path(self.locks.name))
        self.lockdir.start()

    def tearDown(self):
        self.lockdir.stop()
        self.env.stop()
        self.folder.cleanup()
        self.locks.cleanup()

    def _token(self, vault, route):
        from my_actor import connector_auth as auth
        with patch.object(auth, '_vault', return_value=vault), \
                patch.object(auth, '_client', side_effect=lambda: httpx.Client(transport=httpx.MockTransport(route))):
            return auth.token_for('Owner123')

    def test_refused_renewal_marks_the_connection_disconnected(self):
        from my_actor.connections import SetupError
        from my_actor.signin_health import signin_problem
        vault = Vault({'access_token': 'old', 'refresh_token': 'r1', 'expires_at': 0})
        with self.assertRaises(SetupError):
            self._token(vault, _route(400, {'error': 'invalid_grant'}))
        self.assertEqual(signin_problem('Owner123')['reason'], 'expired')
        self.assertIsNone(signin_problem('SomeoneElse'))

    def test_network_trouble_is_not_reported_as_a_cancelled_sign_in(self):
        from my_actor.signin_health import signin_problem
        vault = Vault({'access_token': 'old', 'refresh_token': 'r1', 'expires_at': 0})
        def route(req):
            raise httpx.ConnectError('offline')
        with self.assertRaises(httpx.ConnectError):
            self._token(vault, route)
        with self.assertRaises(Exception):
            self._token(vault, _route(503, {'error': 'unavailable'}))
        self.assertIsNone(signin_problem('Owner123'))

    def test_successful_renewal_clears_an_old_disconnected_note(self):
        from my_actor.signin_health import mark_signin_problem, signin_problem
        mark_signin_problem('Owner123', 'expired')
        vault = Vault({'access_token': 'old', 'refresh_token': 'r1', 'expires_at': 0})
        token = self._token(vault, _route(200, {'access_token': 'new', 'refresh_token': 'r2', 'expires_in': 3600}))
        self.assertEqual(token, 'new')
        self.assertIsNone(signin_problem('Owner123'))

    def test_renewal_by_another_copy_is_used_instead_of_reporting_failure(self):
        from my_actor.signin_health import signin_problem
        vault = Vault({'access_token': 'old', 'refresh_token': 'r1', 'expires_at': 0})
        def other_copy_renewed():
            vault.creds = {'access_token': 'from-other-copy', 'refresh_token': 'r2', 'expires_at': time.time() + 3600}
        token = self._token(vault, _route(400, {'error': 'invalid_grant'}, on_token=other_copy_renewed))
        self.assertEqual(token, 'from-other-copy')
        self.assertIsNone(signin_problem('Owner123'))

    def test_missing_sign_in_is_reported(self):
        from my_actor.connections import SetupError
        from my_actor.signin_health import signin_problem
        with self.assertRaises(SetupError):
            self._token(Vault(None), _route(200, {}))
        self.assertEqual(signin_problem('Owner123')['reason'], 'missing')

    def test_new_sign_in_clears_the_note(self):
        from my_actor import connector_auth as auth
        from my_actor.signin_health import mark_signin_problem, signin_problem
        mark_signin_problem('Owner123', 'expired')
        vault = Vault(None)
        with patch.object(auth, '_vault', return_value=vault), \
                patch.object(auth, 'device_login', return_value=('Owner123', {'access_token': 'fresh'})):
            self.assertEqual(auth.authorize('Owner123'), 'Owner123')
        self.assertIsNone(signin_problem('Owner123'))

    def test_sign_in_still_works_when_the_shared_lock_folder_cannot_be_written(self):
        from my_actor import signin_health
        blocked = Path(self.locks.name) / 'file'
        blocked.write_text('not a folder')
        vault = Vault({'access_token': 'valid', 'refresh_token': 'r1', 'expires_at': time.time() + 3600})
        with patch.object(signin_health, '_lock_dir', return_value=blocked / 'locks'):
            self.assertEqual(self._token(vault, _route(200, {})), 'valid')

    def test_renewal_lock_is_shared_by_every_braintwin_folder_of_this_computer_user(self):
        from my_actor import signin_health
        self.lockdir.stop()
        try:
            first = signin_health.refresh_lock_path('Owner123')
            with patch.dict(os.environ, {'BRAINTWIN_HOME': tempfile.gettempdir(), 'HOME': tempfile.gettempdir()}):
                second = signin_health.refresh_lock_path('Owner123')
            other = signin_health.refresh_lock_path('Other456')
        finally:
            self.lockdir.start()
        self.assertEqual(first, second)
        self.assertNotIn('Owner123', str(first))
        self.assertNotEqual(first, other)


class SignInNoticeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'BRAINTWIN_HOME': self.folder.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_no_notice_while_connected(self):
        from my_actor.signin_health import signin_notice
        self.assertIsNone(signin_notice('claude-code', 'session-start', 'Owner123', '/bin/braintwin-cli'))

    def test_coding_tools_are_told_how_to_reconnect(self):
        from my_actor.signin_health import mark_signin_problem, signin_notice
        mark_signin_problem('Owner123', 'expired')
        for client, event, name in (('claude-code', 'session-start', 'SessionStart'), ('codex', 'prompt', 'UserPromptSubmit'),
                                    ('gemini-cli', 'prompt', 'BeforeAgent')):
            out = signin_notice(client, event, 'Owner123', '/opt/bt/braintwin-cli')
            text = out['hookSpecificOutput']['additionalContext']
            self.assertEqual(out['hookSpecificOutput']['hookEventName'], name)
            self.assertIn('/opt/bt/braintwin-cli connect ' + client + ' --storage apify', text)
            self.assertIn('not being saved', text)
        cursor = signin_notice('cursor', 'session-start', 'Owner123', '/opt/bt/braintwin-cli')
        self.assertIn('connect cursor', cursor['additional_context'])

    def test_prompt_reminder_is_not_repeated_every_message(self):
        from my_actor.signin_health import mark_signin_problem, signin_notice
        mark_signin_problem('Owner123', 'expired')
        self.assertIsNotNone(signin_notice('claude-code', 'prompt', 'Owner123', '/bin/braintwin-cli'))
        self.assertIsNone(signin_notice('claude-code', 'prompt', 'Owner123', '/bin/braintwin-cli'))
        self.assertIsNotNone(signin_notice('codex', 'prompt', 'Owner123', '/bin/braintwin-cli'))
        self.assertIsNotNone(signin_notice('claude-code', 'session-start', 'Owner123', '/bin/braintwin-cli'))
        # Cursor cannot add context to a prompt; the caller keeps its plain "continue" answer.
        self.assertIsNone(signin_notice('cursor', 'prompt', 'Owner123', '/bin/braintwin-cli'))

    def test_session_start_checks_the_sign_in_in_the_background(self):
        from my_actor import signin_health
        launched = []
        class Proc:
            def __init__(self, args, **kw):
                launched.append(args)
                self.stdin = open(os.devnull, 'wb')
        with patch.object(signin_health.subprocess, 'Popen', Proc):
            signin_health.dispatch_signin_check('Owner123')
        self.assertEqual(launched[0][-2:], ['-m', 'my_actor.signin_health'])

    def test_installed_hook_shows_the_notice_for_cloud_storage(self):
        import argparse
        import asyncio
        import io
        from my_actor import cli, connections, signin_health
        signin_health.mark_signin_problem('Owner123', 'expired')
        args = argparse.Namespace(event='session-start', client='claude-code', installed=True)
        with patch.object(connections, 'current_target', return_value=('apify', 'Owner123')), \
                patch.object(connections, 'binary', return_value=Path('/opt/bt/braintwin-cli')), \
                patch.object(signin_health, 'dispatch_signin_check') as check, \
                patch('sys.stdin', io.StringIO('{"hook_event_name":"SessionStart","cwd":"/tmp"}')):
            out = asyncio.run(cli._cmd_hook(args))
        check.assert_called_once_with('Owner123')
        self.assertIn('connect claude-code', out['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
