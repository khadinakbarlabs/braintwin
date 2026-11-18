"""Hook handlers for AI coding tools (Claude Code, Codex, Cursor).

The tool runs `python -m my_actor.cli hook <event>` and pipes its hook JSON to stdin, so
saving does not depend on the AI choosing to call a tool:

  session-start  -> prints the owner's rules and facts as extra context for the session
  prompt         -> files lasting preferences, decisions and facts found in what the USER typed

Only the user's own words are captured (never the AI's reply). The built-in rules only: no AI
call and no charge. A hook must never break the host tool, so every failure becomes `{}`.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .brain import BrainStore
from .capture import extract_candidates
from .context_pack import build_context

logger = logging.getLogger(__name__)

AGENT_LABELS = {'claude-code': 'claude-code-hook', 'codex': 'codex-hook', 'cursor': 'cursor-hook'}
CONTEXT_TOKENS = 1200
MAX_PROMPT_CHARS = 8000


def _payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _situation(data: dict[str, Any]) -> str:
    cwd = str(data.get('cwd') or data.get('workspace') or '').strip()
    name = os.path.basename(cwd.rstrip('/')) if cwd else ''
    return f'Working in {name}' if name else 'Working in an AI coding tool'


async def session_start(brain: BrainStore, data: dict[str, Any]) -> dict[str, Any]:
    name = _situation(data).removeprefix('Working in ')
    result = await build_context(
        brain, f'starting a coding session in {name}', token_budget=CONTEXT_TOKENS,
        scope=None, include_followed=False, max_items=8,
    )
    text = str(result.get('context') or '').strip()
    if not text:
        return {}
    return {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}


async def prompt(brain: BrainStore, data: dict[str, Any], agent: str) -> dict[str, Any]:
    text = str(data.get('prompt') or data.get('text') or '')[:MAX_PROMPT_CHARS]
    found = extract_candidates(text) if text.strip() else []
    if not found:
        return {}
    await brain.save_captured(
        found, tags=[], agent=agent, auto=True, situation=_situation(data), source_text=text
    )
    return {}


async def run_hook(brain: BrainStore, event: str, client: str, raw_stdin: str) -> dict[str, Any]:
    """Run one hook event; never raises (a broken hook must not stop the host tool)."""
    data = _payload(raw_stdin)
    agent = AGENT_LABELS.get(client, f'{client}-hook')
    try:
        if event == 'session-start':
            return await session_start(brain, data)
        if event == 'prompt':
            return await prompt(brain, data, agent)
    except Exception as exc:  # noqa: BLE001 - hooks fail soft by design
        logger.warning('hook %s failed: %s: %s', event, type(exc).__name__, str(exc)[:200])
    return {}
