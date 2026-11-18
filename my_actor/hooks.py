"""Local session context and rule-based user-prompt capture for coding tools."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
import logging
import os
from typing import Any

from .brain import BrainStore, scope_tag_for
from .capture import extract_candidates, user_turns
from .context_pack import FENCE, _line
from .ranking import is_expired
from .policy import contains_secret, normalize_text

logger = logging.getLogger(__name__)

CLIENTS = ('claude-code', 'codex', 'cursor')
CONTEXT_TOKENS = 1200
MAX_PROMPT_CHARS = 8000

EVENT_NAMES = {
    'claude-code': {'session-start': 'SessionStart', 'prompt': 'UserPromptSubmit'},
    'codex': {'session-start': 'SessionStart', 'prompt': 'UserPromptSubmit'},
    'cursor': {'session-start': 'sessionStart', 'prompt': 'beforeSubmitPrompt'},
}


def empty_output(event: str, client: str) -> dict[str, Any]:
    return {'continue': True} if client == 'cursor' and event == 'prompt' else {}


def _payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _workspace(data: dict[str, Any]) -> tuple[str, str]:
    cwd = data.get('cwd') or data.get('workspace')
    if not isinstance(cwd, str) or not cwd:
        roots = data.get('workspace_roots')
        cwd = roots[0] if isinstance(roots, list) and roots and isinstance(roots[0], str) else ''
    if not cwd:
        return '', ''
    path = Path(cwd).resolve()
    root = next((p for p in (path, *path.parents) if (p / '.git').exists()), path)
    fingerprint = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    return root.name, f'{root.name[:40]}-{fingerprint}'


async def _session_context(brain: BrainStore, scope: str) -> str:
    # Startup has no task query. Inject current-workspace notes and genuinely global preferences.
    now = datetime.now(UTC)
    tag = scope_tag_for(scope)
    recent = []
    for row in (await brain._current_index_entries()).values():
        if row.get('status') != 'approved' or is_expired(row, now):
            continue
        scopes = {str(t).lower() for t in row.get('tags', []) if str(t).lower().startswith('scope:')}
        local = tag in scopes
        origin = row.get('origin') or {}
        legacy_project = str(origin.get('by', '')).endswith('-hook') and origin.get('situation') != 'Working in an AI coding tool'
        if not local and (scopes or legacy_project or row.get('type') not in ('preference', 'profile')):
            continue
        recent.append((local, str(row.get('updatedAt', '')), row))
    lines = [FENCE]
    for _, _, row in sorted(recent, key=lambda item: item[:2], reverse=True)[:6]:
        line = _line(row.get('title'), row.get('summary'), row['id'], row.get('origin'))
        if sum(map(len, lines)) + len(line) + len(lines) <= CONTEXT_TOKENS * 4:
            lines.append(line)
    return '\n'.join(lines) if len(lines) > 1 else ''


async def run_hook(
    brain: BrainStore, event: str, client: str, raw_stdin: str, *, raise_on_error: bool = False,
) -> dict[str, Any]:
    """Read only the documented prompt field. A failed hook never blocks the tool."""
    fallback = empty_output(event, client)
    try:
        data = _payload(raw_stdin)
        expected = EVENT_NAMES.get(client, {}).get(event)
        if not expected or data.get('hook_event_name', expected) != expected or data.get('is_background_agent') is True:
            return fallback
        name, scope = _workspace(data)
        if event == 'session-start':
            text = await _session_context(brain, scope)
            if text:
                if client == 'cursor':
                    return {'additional_context': text}
                return {'hookSpecificOutput': {'hookEventName': expected, 'additionalContext': text}}
        else:
            text = data.get('prompt')
            if isinstance(text, str) and text.strip() and not contains_secret(normalize_text(text)):
                text = text[:MAX_PROMPT_CHARS]
                turns = user_turns(text)
                found = extract_candidates('\n'.join(turns) if turns is not None else text)
                if found:
                    await brain.save_captured(
                        found, tags=[scope_tag_for(scope)] if scope else [], agent=f'{client}-hook', auto=True,
                        situation=f'Working in {name}' if name else 'Working in an AI coding tool',
                    )
    except Exception as exc:  # noqa: BLE001 - the host must run even if memory fails
        if raise_on_error:
            raise  # the CLI rolls the transaction back before returning an empty result
        logger.warning('hook %s failed: %s', event, type(exc).__name__)
    return fallback
