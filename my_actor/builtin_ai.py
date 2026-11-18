"""Built-in AI note-taking: the publisher's own low-cost AI model finds facts the rules miss.

Why it exists: the rules in capture.py only catch clear phrasings. Rival products use an AI model
for this by default. Here the hosted server can do the same without the user bringing a key, and
the user pays a small, separate price for it (event `smart-capture`), charged ONLY when the AI
step actually found at least one lasting fact in what was said.

Guard rails:
  * off unless the server has a model key AND the `smart-capture` price is active for this run
    (so it can never be given away, or run where it was not priced);
  * each user can switch it off in the dashboard (`config.ai`), and a user who set up their own
    AI key is never sent through the built-in one;
  * it is asked only when the text could hold a personal fact (first-person wording, long enough);
  * a daily limit per user; text is capped; the same "only words the user said" check applies;
  * the model call comes first and the charge second, so a failed AI call costs the user nothing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

KEY_AI = 'config.ai'
AI_EVENT = 'smart-capture'
DAILY_LIMIT = 300  # AI calls per user per day
MIN_CHARS = 40
_PERSONAL_RE = re.compile(r"\b(?:i|i'm|i've|i'd|my|mine|me|we|we're|we've|our|ours|us)\b", re.IGNORECASE)

logger = logging.getLogger('apify.braintwin.ai')


_REQUEST_RE = re.compile(
    r"^(?:please\s+|hey\s+|ok(?:ay)?[,\s]+|so[,\s]+)*(?:can|could|would|will)\s+you\b|"
    r"^(?:please\s+)?(?:write|create|make|build|fix|show|give|explain|tell|list|find|generate|summari[sz]e|translate|"
    r"draft|review|check|help|add|remove|update|convert|rewrite|run|search|open|compare|describe|plan)\b",
    re.IGNORECASE,
)


def worth_asking(text: str) -> bool:
    """Cheap test before spending an AI call: could this text hold a lasting fact?

    Asked: statements about the user or their world ("the Oslo warehouse ships on Wednesdays").
    Not asked: very short text, questions, and plain requests ("write a function…") that say
    nothing personal. A request that does mention the user ("fix my…, we always…") is still asked.
    """
    text = (text or '').strip()
    if len(text) < MIN_CHARS:
        return False
    parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+', text) if p.strip()]
    if all(p.endswith('?') for p in parts):
        return False
    if _PERSONAL_RE.search(text):
        return True
    return any(not p.endswith('?') and not _REQUEST_RE.search(p) for p in parts)


async def ai_setting(brain: Any) -> dict[str, Any]:
    raw = await brain.kv.get_value(KEY_AI)
    raw = raw if isinstance(raw, dict) else {}
    return {'builtin': raw.get('builtin') is not False, 'date': raw.get('date'), 'count': int(raw.get('count') or 0)}


async def set_builtin(brain: Any, on: bool) -> dict[str, Any]:
    state = await ai_setting(brain)
    state['builtin'] = bool(on)
    await brain.kv.set_value(KEY_AI, state)
    return state


class BuiltinAI:
    def __init__(
        self,
        extractor: Any,
        charge: Callable[[], Awaitable[bool]],
        priced: Callable[[], bool],
        price_usd: float | None = None,
    ) -> None:
        self._extractor = extractor
        self._charge = charge
        self._priced = priced
        self.price_usd = float(price_usd) if price_usd is not None else None
        self.model = getattr(extractor, 'model', '')

    def available(self) -> bool:
        try:
            return bool(self._priced())
        except Exception:  # noqa: BLE001 - if the price list cannot be read, stay off
            return False

    async def status(self, brain: Any) -> dict[str, Any]:
        state = await ai_setting(brain)
        return {'available': self.available(), 'on': self.available() and state['builtin'], 'priceUsd': self.price_usd}

    async def find(self, brain: Any, text: str, known: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], bool]:
        """(facts the rules did not already find, whether the AI step was charged). Never raises."""
        if not self.available() or not worth_asking(text):
            return [], False
        state = await ai_setting(brain)
        today = datetime.now(UTC).date().isoformat()
        count = state['count'] if state['date'] == today else 0
        if not state['builtin'] or count >= DAILY_LIMIT:
            return [], False
        try:
            items = await self._extractor.extract(text)
        except Exception as exc:  # noqa: BLE001 - the rules stand alone when the model fails
            logger.warning('built-in AI failed: %s: %s', type(exc).__name__, str(exc)[:200])
            return [], False
        await brain.kv.set_value(KEY_AI, {'builtin': True, 'date': today, 'count': count + 1})
        from .extractor import merge_candidates

        known = list(known or [])
        items = merge_candidates(known, items)[len(known):]  # drop what the free rules already caught
        if not items:
            return [], False  # nothing new found: the user pays nothing for this call
        try:
            paid = await self._charge()
        except Exception as exc:  # noqa: BLE001
            logger.warning('built-in AI charge failed: %s', type(exc).__name__)
            paid = False
        if not paid:
            return [], False  # spending limit reached: do not hand out unpaid results
        for item in items:
            item['reason'] = 'builtin_ai'
        return items, True


# Set by the hosted server at start. None everywhere else (local use, tests).
provider: BuiltinAI | None = None
