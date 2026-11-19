"""Exercise the connector as installed, including the event commands read by each host."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'claude-code': ('.claude.json', '.claude/settings.json', 'UserPromptSubmit', 'SessionStart'),
    'codex': ('.codex/config.toml', '.codex/hooks.json', 'UserPromptSubmit', 'SessionStart'),
    'cursor': ('.cursor/mcp.json', '.cursor/hooks.json', 'beforeSubmitPrompt', 'sessionStart'),
    'gemini-cli': ('.gemini/settings.json', '.gemini/settings.json', 'BeforeAgent', 'SessionStart'),
}


class InstalledConnectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='braintwin home ')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.bin = self.home / 'test bin'
        self.bin.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('BRAINTWIN_', 'SECOND_BRAIN_', 'CLAUDE_', 'CODEX_', 'GEMINI_', 'XDG_'))}
        self.env.update(PATH=str(self.bin) + os.pathsep + os.environ['PATH'], HOME=str(self.home), PYTHONPATH=str(ROOT), BRAINTWIN_SQLITE=str(self.home / 'brain.sqlite'))
        for name, module in [('braintwin', 'stdio'), ('braintwin-cli', 'cli')]:
            entry = self.bin / name
            entry.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -m my_actor.' + module + ' "$@"\n')
            entry.chmod(0o700)

    def cli(self, *args):
        return subprocess.run([str(self.bin / 'braintwin-cli'), *args], env=self.env, text=True, capture_output=True, timeout=15)

    def connect(self, client):
        p = self.cli('connect', client, '--storage', 'local')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def command(self, client, event):
        hooks = json.loads((self.home / CASES[client][1]).read_text())['hooks'][event]
        if client == 'cursor':
            commands = hooks
        else:
            commands = [c for group in hooks for c in group['hooks']]
        ours = [x['command'] for x in commands if 'braintwin-cli' in x['command']]
        self.assertEqual(len(ours), 1)
        return ours[0]

    def fire(self, command, event, **fields):
        p = subprocess.run(shlex.split(command), env=self.env, input=json.dumps({'hook_event_name': event, 'cwd': '/fixtures/garden', **fields}), capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_one_install_registers_tools_saves_plain_preference_and_recalls_in_a_new_process(self):
        for client, (config, _, prompt, start) in CASES.items():
            with self.subTest(client=client):
                self.connect(client)
                self.connect(client)
                data = (self.home / config).read_text()
                spec = tomllib.loads(data)['mcp_servers']['braintwin'] if client == 'codex' else json.loads(data)['mcpServers']['braintwin']
                self.assertTrue(Path(spec['command']).is_file())
                self.fire(self.command(client, prompt), prompt, prompt='I prefer weekly status reports on Fridays.')
                self.fire(self.command(client, prompt), prompt, prompt='I prefer weekly status reports on Fridays.')
                context = self.fire(self.command(client, start), start)
                self.assertIn('Fridays', json.dumps(context))
        self.assertEqual(json.loads(self.cli('health').stdout)['memoryTotal'], 1)

    def test_removing_native_connector_stops_future_callbacks_and_disconnect_preserves_others(self):
        root = self.home / '.cursor'
        root.mkdir()
        (root / 'mcp.json').write_text(json.dumps({'mcpServers': {'other': {'command': 'other-server'}}}))
        (root / 'hooks.json').write_text(json.dumps({'version': 1, 'hooks': {'beforeSubmitPrompt': [{'command': 'other-hook'}]}}))
        self.connect('cursor')
        command = self.command('cursor', 'beforeSubmitPrompt')
        cfg = json.loads((root / 'mcp.json').read_text())
        del cfg['mcpServers']['braintwin']
        (root / 'mcp.json').write_text(json.dumps(cfg))
        self.fire(command, 'beforeSubmitPrompt', prompt='I prefer oranges over lemons.')
        self.assertEqual(json.loads(self.cli('health').stdout)['memoryTotal'], 0)
        self.assertEqual(self.cli('disconnect', 'cursor').returncode, 0)
        self.assertDictEqual(json.loads((root / 'hooks.json').read_text())['hooks'], {'beforeSubmitPrompt': [{'command': 'other-hook'}]})
        self.assertEqual(json.loads((root / 'mcp.json').read_text()), {'mcpServers': {'other': {'command': 'other-server'}}})
        self.assertEqual(self.cli('disconnect', 'cursor').returncode, 0)

    def test_broken_hooks_or_conflicting_server_leave_existing_setup_alone(self):
        root = self.home / '.cursor'
        root.mkdir()
        config = root / 'mcp.json'
        hooks = root / 'hooks.json'
        config.write_text('{"mcpServers":{"personal":{"command":"mine"}}}')
        hooks.write_text('unfinished {')
        before = config.read_bytes()
        self.assertNotEqual(self.cli('connect', 'cursor').returncode, 0)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(hooks.read_text(), 'unfinished {')
        hooks.unlink()
        config.write_text('{"mcpServers":{"braintwin":{"command":"my-custom-command"}}}')
        self.assertNotEqual(self.cli('connect', 'cursor').returncode, 0)
        self.assertFalse(hooks.exists())
        self.assertIn('my-custom-command', config.read_text())

    def test_opt_out_secret_and_assistant_only_input_do_not_become_memories(self):
        self.connect('codex')
        command = self.command('codex', 'UserPromptSubmit')
        for turn in ("I prefer tea with cinnamon. " + (" " * 8100) + "Don't remember this.",
                     'I prefer blue folders. ' + (' ' * 8100) + 'My password is fake-test-password.',
                     'Assistant: I prefer weekly carousel posts.', 'Could you list the files?'):
            self.fire(command, 'UserPromptSubmit', prompt=turn)
        self.assertEqual(json.loads(self.cli('health').stdout)['memoryTotal'], 0)

    def test_human_approval_mode_is_also_honored_by_installed_hooks(self):
        self.env['BRAINTWIN_APPROVAL'] = 'human'
        self.connect('cursor')
        self.fire(self.command('cursor', 'beforeSubmitPrompt'), 'beforeSubmitPrompt', prompt='I prefer reviews every Thursday.')
        start = self.fire(self.command('cursor', 'sessionStart'), 'sessionStart')
        self.assertNotIn('Thursday', json.dumps(start))
        self.assertIn('Thursday', self.cli('inbox', 'list').stdout)

    def test_codex_reconfigure_preserves_disabled_server_preferences_and_toml_comments(self):
        root = self.home / '.codex'
        root.mkdir()
        config = root / 'config.toml'
        config.write_text('# my personal defaults\nmodel = "example"\n')
        self.connect('codex')
        config.write_text(config.read_text() + '\n[mcp_servers.braintwin.env]\nMY_SETTING = "kept"\n')
        self.connect('codex')
        data = tomllib.loads(config.read_text())
        self.assertEqual(data['mcp_servers']['braintwin']['env']['MY_SETTING'], 'kept')
        self.assertIn('# my personal defaults', config.read_text())

    def test_ask_first_keeps_new_preference_out_of_recall(self):
        self.connect('claude-code')
        self.assertEqual(self.cli('policy', 'off').returncode, 0)
        self.fire(self.command('claude-code', 'UserPromptSubmit'), 'UserPromptSubmit', prompt='I prefer hiking on Fridays.')
        self.assertNotIn('Fridays', json.dumps(self.fire(self.command('claude-code', 'SessionStart'), 'SessionStart')))
        self.assertIn('Fridays', self.cli('inbox', 'list').stdout)


if __name__ == '__main__':
    unittest.main()
