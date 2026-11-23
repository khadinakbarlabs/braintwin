"""Keep the user informed when the Apify sign-in of an installed connector stops working.

A cancelled sign-in used to stop memory silently. Now the connector writes a small note when Apify
refuses the saved sign-in, coding-tool hooks read that note (no network) and ask the AI to tell the
user how to reconnect, and a session start re-checks the sign-in in the background.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .connections import locked, put_file, state_dir

NOTE_FILE = 'signin-problem.json'
PROMPT_REMINDER_SECONDS = 30 * 60
REFUSED = frozenset({'invalid_grant', 'invalid_client', 'unauthorized_client'})

EVENTS = {
    'claude-code': {'session-start': 'SessionStart', 'prompt': 'UserPromptSubmit'},
    'codex': {'session-start': 'SessionStart', 'prompt': 'UserPromptSubmit'},
    'gemini-cli': {'session-start': 'SessionStart', 'prompt': 'BeforeAgent'},
    'cursor': {'session-start': 'sessionStart'},
}


def _lock_dir() -> Path:
    # The keyring is per computer user, so the renewal lock must be too: not per Braintwin folder
    # and not per HOME variable. Two copies renewing at once can make Apify cancel the sign-in.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.braintwin' / 'locks'


def refresh_lock_path(account: str) -> Path:
    return _lock_dir() / ('refresh-' + hashlib.sha256(account.encode()).hexdigest()[:16] + '.lock')


@contextmanager
def refresh_lock(account: str) -> Iterator[None]:
    path = refresh_lock_path(account)
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        handle = path.open('a+b')
    except OSError:
        # A sandboxed tool may not write to the home folder. The per-folder lock still applies.
        yield
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def refused(response: Any) -> bool:
    """True only when Apify says the saved sign-in itself is no longer valid."""
    if response.status_code not in (400, 401):
        return False
    try:
        return response.json().get('error') in REFUSED
    except ValueError:
        return False


def _note() -> Path:
    return state_dir() / NOTE_FILE


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_note().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_signin_problem(account: str, reason: str) -> None:
    with locked('signin-note'):
        prior = _read()
        warned = prior.get('warned', {}) if prior.get('account') == account else {}
        note = {'account': account, 'reason': reason, 'at': time.time(), 'warned': warned}
        put_file(_note(), json.dumps(note).encode())


def clear_signin_problem(account: str) -> None:
    if not _note().exists():
        return
    with locked('signin-note'):
        if _read().get('account') == account:
            _note().unlink(missing_ok=True)


def signin_problem(account: str) -> dict[str, Any] | None:
    data = _read()
    return data if data.get('account') == account else None


def _message(client: str, cli: str) -> str:
    return (
        "Braintwin memory is disconnected: the user's Apify sign-in for Braintwin was cancelled or expired, "
        'so what they say is not being saved or recalled. Tell the user this once, in plain words, and give '
        f'them this command to run in a terminal to reconnect: {cli} connect {client} --storage apify'
    )


def signin_notice(client: str, event: str, account: str, cli: str) -> dict[str, Any] | None:
    """Hook output asking the AI to tell the user, or None when nothing is wrong."""
    name = EVENTS.get(client, {}).get(event)
    if not name or signin_problem(account) is None:
        return None
    if event == 'prompt':
        with locked('signin-note'):
            note = _read()
            if note.get('account') != account:
                return None
            warned = note.get('warned', {})
            if time.time() - float(warned.get(client, 0)) < PROMPT_REMINDER_SECONDS:
                return None
            note['warned'] = {**warned, client: time.time()}
            put_file(_note(), json.dumps(note).encode())
    text = _message(client, cli)
    if client == 'cursor':
        return {'additional_context': text}
    return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}


def dispatch_signin_check(account: str) -> None:
    """Check the saved sign-in without delaying the session; the result lands in the note."""
    try:
        process = subprocess.Popen([sys.executable, '-I', '-m', 'my_actor.signin_health'],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        with process.stdin as pipe:
            pipe.write(json.dumps(account).encode())
    except OSError:
        pass


def main() -> None:
    account = json.load(sys.stdin)
    from .connector_auth import token_for
    try:
        token_for(account)
    except Exception:  # noqa: BLE001 - token_for records a refused sign-in; nothing else to report
        pass


if __name__ == '__main__':
    main()
