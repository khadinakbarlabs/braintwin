"""Register one personal MCP connection and its lifecycle callbacks together."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .hooks import EVENT_NAMES
from .storage import default_sqlite_path

MANAGED_URL = 'https://mcp.apify.com/?tools=khadinakbar/braintwin'


class SetupError(ValueError):
    """An actionable setup problem that is safe to display."""


def binary() -> Path:
    entry = Path(sys.argv[0]).absolute()
    if entry.name == 'braintwin-cli' and entry.is_file():
        return entry
    found = shutil.which('braintwin-cli')
    if found:
        return Path(found).absolute()
    raise SetupError('Run the Braintwin installer for this agent first.')


def paths(client: str) -> tuple[Path, Path]:
    home = Path.home()
    if client == 'codex':
        folder = Path(os.environ.get('CODEX_HOME') or home / '.codex')
        return folder / 'config.toml', folder / 'hooks.json'
    if client == 'claude-code':
        custom = os.environ.get('CLAUDE_CONFIG_DIR')
        folder = Path(custom) if custom else home / '.claude'
        return (folder / '.claude.json' if custom else home / '.claude.json'), folder / 'settings.json'
    if client == 'cursor':
        return home / '.cursor/mcp.json', home / '.cursor/hooks.json'
    file = home / '.gemini/settings.json'
    return file, file


def state_dir() -> Path:
    return Path(os.environ.get('BRAINTWIN_HOME') or Path.home() / '.braintwin') / 'connections'


@contextmanager
def locked(name: str):
    import fcntl

    folder = state_dir()
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (folder / (name + '.lock')).open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def put_file(path: Path, content: bytes, *, mode: int = 0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        swap = Path(temp.name)
        try:
            os.fchmod(temp.fileno(), mode)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(swap, path)
        finally:
            swap.unlink(missing_ok=True)


class Document:
    def __init__(self, path: Path):
        self.path = path
        self.original = path.read_bytes() if path.exists() else None
        self.toml = path.suffix == '.toml'
        try:
            source = (self.original or b'').decode('utf-8')
            if self.toml:
                import tomlkit
                self.data = tomlkit.parse(source)
            else:
                self.data = json.loads(source) if self.original is not None else {}
            if not isinstance(self.data, dict):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise SetupError(f'Check the format of {path}. Braintwin needs valid JSON or TOML to merge this file.') from exc
        self.initial = self.render()

    def render(self):
        if self.toml:
            import tomlkit
            return tomlkit.dumps(self.data).encode()
        return (json.dumps(self.data, indent=2, ensure_ascii=False) + '\n').encode()

    def write(self, content):
        # Keep symlinked dotfiles usable, including configuration managed with a dotfiles repo.
        target = self.path.resolve()
        if content is None:
            target.unlink(missing_ok=True)
        else:
            mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
            put_file(target, content, mode=mode)


def _commit(docs):
    changed = [d for d in docs if d.render() != d.initial]
    applied = []
    try:
        for d in changed:
            current = d.path.read_bytes() if d.path.exists() else None
            if current != d.original:
                raise SetupError(f'{d.path} changed during setup. Run the command again.')
            blob = d.render()
            d.write(blob)
            applied.append((d, blob))
    except BaseException:
        for d, blob in reversed(applied):
            if d.path.exists() and d.path.read_bytes() == blob:
                d.write(d.original)
        raise


def _mapping(data, key, path):
    result = data.setdefault(key, {})
    if not isinstance(result, dict):
        raise SetupError(f'Check the {key} section in {path} before connecting.')
    return result


def _target(entry, cli):
    if not isinstance(entry, dict) or entry.get('command') != str(cli):
        return None
    args = list(entry.get('args') or [])
    if len(args) == 7 and args[5] == '--client' and args[6] in EVENT_NAMES:
        args = args[:5]
    if len(args) == 5 and args[:2] == ['serve', '--storage']:
        mode, flag, value = args[2:]
        if isinstance(value, str) and value and (mode, flag) in {('local', '--database'), ('apify', '--account')}:
            return mode, value
    return None


def current_target(client, cli):
    document = Document(paths(client)[0])
    group = document.data.get('mcp_servers' if client == 'codex' else 'mcpServers', {})
    if not isinstance(group, dict):
        return None
    entry = group.get('braintwin')
    if isinstance(entry, dict) and not any(entry.get(k) is False for k in ('enabled',)) and not entry.get('disabled'):
        return _target(entry, cli)
    return None


def _own_hook(row, client, event, cli):
    if not isinstance(row, dict) or not isinstance(row.get('command'), str):
        return False
    try:
        words = shlex.split(row['command'])
    except ValueError:
        return False
    old = [str(cli), 'hook', event, '--client', client]
    return words == old or words == old + ['--installed']


def _update_hooks(doc, client, cli, *, remove, mode):
    if remove and 'hooks' not in doc.data:
        return
    if client == 'cursor' and not remove:
        if doc.data.get('version', 1) != 1:
            raise SetupError('This version of the Cursor hooks file is unsupported.')
        doc.data['version'] = 1
    hooks = _mapping(doc.data, 'hooks', doc.path)
    for event, name in EVENT_NAMES[client].items():
        groups = hooks.get(name, [])
        if not isinstance(groups, list):
            raise SetupError(f'Check {name} in {doc.path}. Expected a list of hooks.')
        updated = []
        for group in groups:
            if client == 'cursor':
                if not _own_hook(group, client, event, cli):
                    updated.append(group)
            elif isinstance(group, dict) and isinstance(group.get('hooks'), list):
                others = [h for h in group['hooks'] if not _own_hook(h, client, event, cli)]
                if len(others) == len(group['hooks']):
                    updated.append(group)
                elif others:
                    updated.append({**group, 'hooks': others})
            else:
                updated.append(group)
        if not remove and (mode == 'local' or event == 'prompt'):
            command = shlex.join([str(cli), 'hook', event, '--client', client, '--installed'])
            task = {'command': command}
            if client == 'cursor':
                updated.append(task)
            else:
                if client == 'gemini-cli':
                    task['name'] = 'braintwin-' + event
                updated.append({'hooks': [{'type': 'command', **task}]})
        if updated:
            hooks[name] = updated
        else:
            hooks.pop(name, None)


def configure(client, cli, mode='local', *, remove=False, authorize=None):
    """Validate both files before signing in or writing either. Leave unrelated settings intact."""
    server_file, hook_file = paths(client)
    with locked('setup'):
        server = Document(server_file)
        hook = server if hook_file == server_file else Document(hook_file)
        group_key = 'mcp_servers' if client == 'codex' else 'mcpServers'
        group = _mapping(server.data, group_key, server.path) if not remove or group_key in server.data else {}
        prior = group.get('braintwin')
        ours = _target(prior, cli)
        _update_hooks(hook, client, cli, remove=remove, mode=mode)
        if remove:
            if ours:
                del group['braintwin']
        else:
            remote_key = 'httpUrl' if client == 'gemini-cli' and isinstance(prior, dict) and 'httpUrl' in prior else 'url'
            remote = (isinstance(prior, dict) and prior.get(remote_key) == MANAGED_URL
                      and set(prior) <= {remote_key, 'type', 'enabled', 'disabled'})
            local = isinstance(prior, dict) and prior.get('command') == str(cli.with_name('braintwin')) and set(prior) <= {'command', 'args'} and not prior.get('args')
            if prior is not None and not (ours or (remote and mode == 'apify') or (local and mode == 'local')):
                raise SetupError(f'A different Braintwin connection exists in {server_file}. Remove it in your agent before switching this setup.')
            if ours and ours[0] != mode:
                raise SetupError(f'Run braintwin-cli disconnect {client} before changing where memories are saved.')
            value = (authorize(ours[1] if ours else None) if mode == 'apify' else
                     (ours[1] if ours else str(default_sqlite_path().absolute())))
            flag = '--account' if mode == 'apify' else '--database'
            entry = prior if ours else {k: prior[k] for k in ('enabled', 'disabled') if remote and k in prior}
            entry.update({'command': str(cli), 'args': ['serve', '--storage', mode, flag, value]})
            if mode == 'apify':
                entry['args'] += ['--client', client]
            if not ours:
                group['braintwin'] = entry
        _commit([server] if server is hook else [server, hook])
    return {'ok': True, 'client': client, 'connected': not remove, **({} if remove else {'storage': mode})}
