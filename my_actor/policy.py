"""Context-based automatic approval: the server decides, using rules the owner controls.

A proposal is approved automatically ONLY when every check passes. Anything unusual stays in the
inbox for the owner. Pure functions: no storage, no network, easy to test.

IMPORTANT - what this is and is not: the text checks (secrets, card numbers, instruction-like text) are
BEST-EFFORT pattern filters; a determined attacker can paraphrase around them. `min_confidence`,
`allowed_types`, `allowed_scopes` and `trusted_agents` filter on values the calling agent itself
declares, so they stop mistakes and lazy abuse, not a hostile agent. The hard controls are: web pages,
imports and followed feeds are NEVER auto-approved, nothing replaces an existing memory, there is a
daily cap, every automatic approval is logged, and the owner can switch it off.

Checks (each can only make approval LESS likely):
  - policy mode is not "off" (mode "open" skips the next two lines and saves everything that holds no secret)
  - the source is the agent's own note, not a fetched web page / import / followed feed
  - confidence >= min_confidence, type and scope are on the owner's allow-lists
  - text is short enough and contains no secret, card/ID number or instruction aimed at an AI
  - the daily automatic-approval cap is not used up (checked by the caller)
  - the memory would not replace an existing approved one (checked by the caller)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

MODE_OFF = 'off'
MODE_SMART = 'smart'
MODE_OPEN = 'open'  # save everything by itself; only secrets and injected instructions are held back
MODES = (MODE_OFF, MODE_SMART, MODE_OPEN)
OPEN_CHECK_CHARS = 60_000  # longest text the automatic mode will check (and so the longest it will save by itself)
MAX_REPLACEMENTS_PER_DAY = 10  # automatic "X is now Y" replacements of an older memory
OPEN_CAP_FACTOR = 20  # a web page is many passages, so the automatic mode gets a higher daily cap

DEFAULT_POLICY: dict[str, Any] = {
    'mode': MODE_OPEN,
    'min_confidence': 0.75,
    'allowed_types': ['preference', 'fact', 'decision', 'note', 'project', 'profile', 'lesson'],
    'allowed_scopes': [],  # empty = any scope
    'trusted_agents': [],  # empty = any agent label
    'max_chars': 4_000,
    'daily_cap': 100,
}
POLICY_KEYS = tuple(DEFAULT_POLICY)
_LIST_KEYS = {'allowed_types', 'allowed_scopes', 'trusted_agents'}

# Provenance keys / tags that mean "not the agent's own words": never auto-approved.
_UNTRUSTED_PROVENANCE = ('untrusted', 'url', 'sourceUrl', 'sourcePlugin', 'followId', 'feed')
_UNTRUSTED_TAGS = ('followed', 'imported', 'web', 'source', 'ingest')
_UNTRUSTED_TYPES = ('source_snapshot', 'followed')

_SECRET_RES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
        r'\bsk-[A-Za-z0-9_-]{20,}',
        r'\b[sp]k_(?:live|test)_[A-Za-z0-9]{16,}',
        r'\bAKIA[0-9A-Z]{16}\b',
        r'\bgh[pousr]_[A-Za-z0-9]{30,}',
        r'\bgithub_pat_[A-Za-z0-9_]{30,}',
        r'\bxox[baprs]-[A-Za-z0-9-]{10,}',
        r'\bapify_api_[A-Za-z0-9]{16,}',
        r'\bAIza[0-9A-Za-z_-]{30,}',
        r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}',
        r'\b(?:password|passwd|pwd|secret|token|api[_ -]?key|private[_ -]?key)\b\s{0,8}(?:(?:is|are)\s{1,8})?[:=]\s{0,8}\S{6,}',
        r'\b(?:password|passcode)\s{1,8}is\s{1,8}\S{6,}',
        r'\b(?:password|passcode|passwd|pwd)\s{1,8}(?!is\b|are\b|for\b|policy\b|manager\b|reset\b|should\b|must\b)\S*\d\S*',
        r'\bbearer\s{1,8}[A-Za-z0-9._~+/=-]{12,}',
        r'\bauthorization\s{0,8}:\s{0,8}\S{8,}',
        r'://[^\s/:@]{1,64}:[^\s/@]{1,128}@',
        r'[?&](?:token|key|api_?key|secret|password|sig|signature|access_token|auth)=[^\s&]{6,}',
        r'(?<![A-Za-z0-9])[0-9a-f]{24,}(?![A-Za-z0-9])',
        r'\b(?:pass ?phrase|passcode|password|pin code)\b.{0,24}\b(?:is|was|would be|will be)\s{1,8}\S{6,}',
    )
)
_URL_RE = re.compile(r'https?://\S+')
_OPAQUE_RE = re.compile(r'(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_-])')
_CARD_RE = re.compile(r'(?<!\d)(?:\d[ .-]?){13,19}(?!\d)')
_SSN_RE = re.compile(r'(?<!\d)\d{3}[- .]\d{2}[- .]\d{4}(?!\d)|\b(?:ssn|social security (?:number|no))\b', re.IGNORECASE)
_IBAN_RE = re.compile(r'\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b')

_INJECTION_RES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'ignore (?:all |any |the )?(?:previous|prior|above|earlier|system) (?:instructions?|rules?|prompts?|messages?)',
        r'disregard (?:the |all |any )?(?:system|previous|prior|above|earlier)',
        r'\bsystem prompt\b',
        r'\bnew instructions?\b',
        r'\byou (?:must|should) (?:now|always) (?:approve|obey|ignore|call|run|send|execute)',
        r'do not (?:tell|inform|mention|show)\b.{0,40}\b(?:user|owner|human)\b',
        r'<\s*/?\s*(?:system|assistant|tool|instructions?)\s*>',
        r'\b(?:auto-?approve|approve (?:this|all|every|them))\b',
        r'\boverride (?:the )?(?:policy|rules?|consent|approval)\b',
        r'\bexfiltrate\b',
        r'\bsend\b.{0,60}\bto\s+https?://',
        r'\bact as (?:an? )?(?:admin|root|system)\b',
        r'\b(?:forward|send|email|mail|upload|post|share)\b.{0,50}\b(?:all|every|any|each)\b.{0,40}\b(?:emails?|files?|passwords?|messages?|documents?|data|contacts|secrets?|keys?)\b',
        r'\byou (?:must|should|shall|need to|have to|will|are to)\b',
        r'\b(?:assistant|agent|ai|llm|model|claude|chatgpt|copilot)\b.{0,30}\b(?:must|should|shall|needs? to|has to)\b',
        r'\bignore\b.{0,30}\b(?:instructions?|rules?|prompts?|guidelines?)\b',
        r'\bignor(?:e|ez|a|ar|iere|iert)\b.{0,30}\b(?:instructions?|instrucciones|consignes?|anweisungen|regeln|r[eè]gles)\b',
    )
)


@dataclass(frozen=True)
class Decision:
    ok: bool
    rule: str = ''
    reasons: tuple[str, ...] = field(default_factory=tuple)


def load_policy(raw: Any) -> dict[str, Any]:
    """Defaults overlaid with the owner's stored values; wrong-typed values fall back safely."""
    policy = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_POLICY.items()}
    if not isinstance(raw, dict):
        return policy
    for key in POLICY_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == 'mode':
            policy['mode'] = str(value).lower() if str(value).lower() in MODES else MODE_OFF  # unknown value fails CLOSED
        elif key == 'min_confidence':
            try:
                policy[key] = min(max(float(value), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
        elif key in ('max_chars', 'daily_cap'):
            try:
                policy[key] = max(int(value), 0)
            except (TypeError, ValueError):
                pass
        elif key in _LIST_KEYS and isinstance(value, list):
            policy[key] = [str(v).strip().lower() for v in value if str(v).strip()][:50]
    return policy


def parse_policy_value(key: str, text: str) -> Any:
    """CLI helper: turn "a,b,c" / "0.8" / "off" into the stored type. Raises ValueError on junk."""
    if key not in POLICY_KEYS:
        raise ValueError(f'unknown policy key {key!r}; use one of {", ".join(POLICY_KEYS)}')
    text = text.strip()
    if key == 'mode':
        if text.lower() not in MODES:
            raise ValueError('mode must be "off", "smart" or "open"')
        return text.lower()
    if key == 'min_confidence':
        value = float(text)
        if not 0.0 <= value <= 1.0:
            raise ValueError('min_confidence must be between 0 and 1')
        return value
    if key in ('max_chars', 'daily_cap'):
        value = int(text)
        if value < 0:
            raise ValueError(f'{key} must be 0 or more')
        return value
    return [p.strip().lower() for p in text.split(',') if p.strip()]


def normalize_text(text: str) -> str:
    """NFKC, drop zero-width / format characters, collapse all whitespace: defeats trivial evasion."""
    folded = unicodedata.normalize('NFKC', text or '')
    kept = ''.join(' ' if ch.isspace() else ch for ch in folded if unicodedata.category(ch) != 'Cf')
    return re.sub(r' {2,}', ' ', kept).strip()


def normalize_title_key(title: str) -> str:
    """Comparison key for "same title": case, width, zero-width characters and spacing ignored."""
    return normalize_text(title).casefold()


def contains_secret(text: str) -> bool:
    if any(rx.search(text) for rx in _SECRET_RES):
        return True
    bare = _URL_RE.sub(' ', text)
    for match in _OPAQUE_RE.finditer(bare):
        token = match.group(0)
        if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
            return True
    return False


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def contains_sensitive_id(text: str) -> bool:
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r'\D', '', match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return bool(_SSN_RE.search(text) or _IBAN_RE.search(text))


# Everyday speech and writing say "you should…" and "you need to…" all the time, so those broad
# patterns cannot be used on web pages and video transcripts. For pages only the patterns that
# plainly address an AI system are used.
_PAGE_INJECTION_RES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'ignore (?:all |any |the )?(?:previous|prior|above|earlier|system) (?:instructions?|rules?|prompts?|messages?)',
        r'disregard (?:the |all |any )?(?:system|previous|prior|above|earlier) (?:instructions?|rules?|prompts?|messages?)',
        r'\b(?:reveal|print|show|repeat|leak)\b.{0,30}\bsystem prompt\b',
        r'<\s*/?\s*(?:system|assistant|tool|instructions?)\s*>',
        r'\boverride (?:the )?(?:policy|consent|approval)\b',
        r'\bexfiltrate\b',
        r'\b(?:assistant|agent|ai|llm|model|claude|chatgpt|copilot)\b.{0,30}\b(?:must|should|shall)\b.{0,60}\b(?:send|forward|email|upload|post|reveal|ignore|approve|delete)\b',
        r'\b(?:forward|send|email|mail|upload|post)\b.{0,50}\b(?:all|every)\b.{0,40}\b(?:passwords?|secrets?|keys?|credentials|tokens?)\b',
    )
)


def contains_injection(text: str, *, page: bool = False) -> bool:
    return any(rx.search(text) for rx in (_PAGE_INJECTION_RES if page else _INJECTION_RES))


def is_untrusted_source(provenance: dict[str, Any] | None, tags: list[str] | None) -> bool:
    prov = provenance or {}
    if any(prov.get(k) for k in _UNTRUSTED_PROVENANCE):
        return True
    return any(str(t).lower() in _UNTRUSTED_TAGS or str(t).lower().startswith('follow:') for t in (tags or []))


def evaluate(
    *,
    title: str,
    content: str,
    memory_type: str,
    confidence: float,
    tags: list[str] | None,
    provenance: dict[str, Any] | None,
    policy: dict[str, Any],
    scope_tag: str | list[str] | None = None,
) -> Decision:
    """Decide from the proposal's own context. `ok=False` always lists WHY, for the owner."""
    mode = policy.get('mode')
    if mode == MODE_OPEN:
        # Automatic: everything is saved. Held back only: secrets, card/ID numbers, and text from
        # the web that reads like an instruction to an AI. The whole text is checked.
        text = normalize_text(f'{title}\n{content}'[:OPEN_CHECK_CHARS])  # the whole page, not just its opening
        held: list[str] = []
        untrusted = is_untrusted_source(provenance, tags) or (memory_type or '').lower() in _UNTRUSTED_TYPES
        if untrusted:
            # A public page: what it holds is already public (hashes and long numbers are common
            # there and are not the owner's secrets). The risk is text written to steer an AI.
            if contains_injection(text, page=True):
                held.append('instruction_like_text')
        else:
            if contains_secret(text):
                held.append('secret_detected')
            if contains_sensitive_id(text):
                held.append('sensitive_id_detected')
        if len(title) + len(content) + 1 > OPEN_CHECK_CHARS:
            held.append('too_long')
        if held:
            return Decision(False, reasons=tuple(held))
        return Decision(True, rule=f'open:{(memory_type or "note").lower()}')
    if mode != MODE_SMART:
        return Decision(False, reasons=('policy_off',))
    reasons: list[str] = []
    if is_untrusted_source(provenance, tags) or (memory_type or '').lower() in _UNTRUSTED_TYPES:
        reasons.append('untrusted_source')
    if confidence < float(policy['min_confidence']):
        reasons.append('low_confidence')
    allowed_types = policy.get('allowed_types') or []
    if allowed_types and (memory_type or 'note').lower() not in allowed_types:
        reasons.append('type_not_allowed')
    allowed_scopes = policy.get('allowed_scopes') or []
    if allowed_scopes:
        scopes = [scope_tag] if isinstance(scope_tag, str) else list(scope_tag or [])
        names = [str(t).lower().removeprefix('scope:') for t in scopes] or ['']
        if any(n not in allowed_scopes for n in names):
            reasons.append('scope_not_allowed')
    trusted = policy.get('trusted_agents') or []
    agent = str((provenance or {}).get('agent') or '').lower()
    if trusted and agent not in trusted:
        reasons.append('agent_not_trusted')
    if len(title) + len(content) + 1 > int(policy['max_chars']):
        # Too long: stop here, never run the regex checks over unbounded text.
        reasons.append('too_long')
        return Decision(False, reasons=tuple(reasons))
    text = normalize_text(f'{title}\n{content}')
    if contains_secret(text):
        reasons.append('secret_detected')
    if contains_sensitive_id(text):
        reasons.append('sensitive_id_detected')
    if contains_injection(text):
        reasons.append('instruction_like_text')
    if reasons:
        return Decision(False, reasons=tuple(reasons))
    return Decision(True, rule=f'smart:{(memory_type or "note").lower()}')
