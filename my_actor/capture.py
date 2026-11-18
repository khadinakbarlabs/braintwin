"""Rule-based capture: pull durable facts out of raw chat text. No LLM, no network, no cost.

An agent hands over what the USER said; this finds the sentences worth keeping (preferences,
decisions, facts about the user, "remember that ..."). Everything it finds is only a PROPOSAL and
still goes through the approval policy. It is deliberately conservative: it would rather miss a
fact than invent one.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT_CHARS = 20_000
MAX_CANDIDATES = 12
MIN_SENTENCE = 18
MAX_SENTENCE = 320

_SPLIT_RE = re.compile(r'(?<=[.!?])\s+|\n+')
_WS_RE = re.compile(r'\s+')
# "I prefer X, and we decided Y" holds two facts: cut before an "and" that starts a new statement.
_CLAUSE_RE = re.compile(r",?\s+and\s+(?=(?:i|we|my|our|the team)\b)", re.IGNORECASE)
# Lead-ins that say nothing themselves.
_LEADIN_RE = re.compile(
    r"^(?:(?:for (?:future|your) reference|fyi|just so you know|by the way|btw|heads up|also|and|please)[:,]?\s+)+",
    re.IGNORECASE,
)

# (type, confidence, reason, pattern)
_RULES: tuple[tuple[str, float, str, re.Pattern[str]], ...] = tuple(
    (t, c, r, re.compile(p, re.IGNORECASE))
    for t, c, r, p in (
        ('note', 0.9, 'explicit_remember', r"\b(?:remember|keep in mind|don't forget|do not forget|note that|make a note)\b"),
        (
            'preference',
            0.8,
            'stated_preference',
            r"\b(?:i|we)\s+(?:really\s+|always\s+|usually\s+|generally\s+)?"
            r"(?:prefer|like|love|hate|dislike|avoid|enjoy|can't stand)\b",
        ),
        ('preference', 0.8, 'always_never', r"\b(?:i|we)\s+(?:always|never)\b"),
        (
            'preference',
            0.8,
            'standing_instruction',
            r"\b(?:from now on|going forward|in future|in the future|every time|whenever you|by default)\b"
            r"|\b(?:always|never)\s+(?:use|write|reply|answer|respond|call|give|show|send|include|add|ask|format|keep)\b"
            r"|\b(?:please\s+)?(?:don't|do not|stop)\s+(?:ever\s+)?(?:use|write|reply|answer|respond|call|give|show|send|include|add|ask)\w*\b.{0,60}\b(?:again|anymore|any more|ever|in (?:your|my|the) (?:answers|replies|code|emails|reports))\b"
            r"|\bcall me\s+\w+",
        ),
        (
            'preference',
            0.75,
            'want_or_need',
            r"\bi\s+(?:want|need|expect|would like|'d like)\s+(?:you|all|every|my|the|our)\b.{0,60}\b(?:always|never|every|all|by default|each time)\b",
        ),
        (
            'fact',
            0.75,
            'tools_in_use',
            r"\b(?:i|we)\s+(?:use|work with|run on|deploy (?:on|to)|build with|write in|code in)\s+(?!(?:this|that|the|it|a|an|these|those|your|you)\b)\w+",
        ),
        (
            'decision',
            0.8,
            'stated_decision',
            r"\b(?:i|we|(?:our|my|the) team)(?:'ve| have| has)?\s+(?:decided|agreed|chose|picked|switched to|will use|are going with|settled on)\b",
        ),
        (
            'fact',
            0.8,
            'fact_about_user',
            r"\bmy\s+(?:name|company|team|role|job title|timezone|time zone|city|country|stack|"
            r"project|product|manager|boss|partner|dog|cat|favou?rite\s+\w+(?:\s+\w+){0,4})\s+(?:is|are|was)\b",
        ),
        (
            'fact',
            0.75,
            'fact_about_user',
            r"\bi(?:'m| am)\s+(?:a|an|the|based in|from|working on|building|allergic to|vegetarian|vegan)\b"
            r"|\bi\s+(?:live in|work (?:at|for|as|in|on)|moved to|was born in|speak|studied|have (?:a|an|two|three|\d+)\s+\w+)\b"
            r"|\b(?:our|my)\s+(?:\w+\s+){1,3}(?:is|are)\s+(?:called|named|at|in|on|every|due)\b"
            r"|\bour\s+(?:company|team|product|app|website|site|stack|client|customers?|budget|goal|brand|office)\b.{0,40}\b(?:is|are|uses?|runs?)\b",
        ),
        (
            'fact',
            0.7,
            'date_or_deadline',
            r"\b(?:deadline|due date|launch|release|meeting)\b.{0,40}\b(?:is|on|by|at)\b",
        ),
    )
)


def _clean(sentence: str) -> str:
    text = _LEADIN_RE.sub('', _WS_RE.sub(' ', sentence).strip(' -*>\t"')).strip()
    return text[:1].upper() + text[1:]


def _clauses(sentence: str) -> list[str]:
    """One statement per item; a part too short to stand alone stays with the whole sentence."""
    parts = [p.strip() for p in _CLAUSE_RE.split(sentence) if p.strip()]
    if len(parts) < 2 or any(len(p) < MIN_SENTENCE for p in parts):
        return [sentence]
    return parts


def _title_for(sentence: str) -> str:
    words = re.sub(r"^(?:please\s+)?(?:remember|note)(?:\s+that)?[:,]?\s*", '', sentence, flags=re.IGNORECASE).split()
    title = ''
    for word in words:
        if title and len(title) + 1 + len(word) > 70:
            break
        title = f'{title} {word}'.strip()
    title = title.rstrip('.,;:!?') or sentence[:60]
    return (title[:1].upper() + title[1:])[:120]


_SPEAKER = re.compile(r"^\s*(user|human|assistant|system|tool|developer|chatgpt|claude|gemini):[ \t]*(.*)$", re.I)


def user_turns(text: str) -> list[str] | None:
    """Extract explicitly labelled user turns. Plain notes have no transcript envelope."""
    turns: list[str] = []
    speaker = None
    for line in text.splitlines():
        match = _SPEAKER.match(line)
        if match:
            speaker = match[1].casefold()
            if speaker in ('user', 'human'):
                turns.append(match[2])
        elif speaker in ('user', 'human'):
            turns[-1] += '\n' + line
    return turns if speaker is not None else None


_NO_CAPTURE = re.compile(
    r"\b(?:do not|don['’]t|never)\s+(?:save|store|remember|record|memorize|memorise)\s+"
    r"(?:this|that|anything|what I|my|our|the following)\b|\boff the record\b", re.I,
)


def capture_declined(text: str) -> bool:
    return bool(_NO_CAPTURE.search(text or ''))


def extract_candidates(text: str, *, max_items: int = MAX_CANDIDATES) -> list[dict[str, Any]]:
    """Return up to `max_items` {title, content, type, confidence, reason} found in `text`."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    limit = max(1, min(max_items, MAX_CANDIDATES))
    if capture_declined(text):
        return []
    pieces = [c for raw in _SPLIT_RE.split((text or '')[:MAX_TEXT_CHARS]) for c in _clauses(_clean(raw))]
    for raw in pieces:
        sentence = _clean(raw)
        if not (MIN_SENTENCE <= len(sentence) <= MAX_SENTENCE) or sentence.endswith('?'):
            continue
        key = sentence.lower()
        if key in seen:
            continue
        for memory_type, confidence, reason, rx in _RULES:
            if rx.search(sentence):
                seen.add(key)
                out.append(
                    {
                        'title': _title_for(sentence),
                        'content': sentence,
                        'type': memory_type,
                        'confidence': confidence,
                        'reason': reason,
                    }
                )
                break
        if len(out) >= limit:
            break
    return out


# --- does a new statement replace an older one? ------------------------------------------------
_SUBJECT_RE = re.compile(r"^(.{3,80}?)\s+(?:is|are)\s+(?:now\s+)?(.{2,})$", re.IGNORECASE)
_CHANGE_RE = re.compile(
    r"\b(?:now|no longer|not anymore|not any more|instead of|switched (?:from|to)|changed (?:from|to)|moved (?:from|to)|"
    r"used to|as of (?:today|now)|from now on)\b",
    re.IGNORECASE,
)
_PREFERENCE_PAIR = re.compile(
    r"^(i|we) (?:really )?prefer (.+?) over (.+?)( (?:for|when|at|during|in|on) .+)?$", re.I,
)
_TOKEN_RE = re.compile(r"[a-z0-9']+")
_FILLER = frozenset('a an the of to in on at by for with and or is are was now really very just'.split())
REPLACE_CUE_OVERLAP = 0.5  # with a change word ("now", "no longer"), this much shared wording is enough


def _toks(text: str) -> list[str]:
    return [w for w in _TOKEN_RE.findall(text.lower()) if w not in _FILLER]


def same_fact(left: str, right: str) -> bool:
    """Allow superficial restatements while preserving word order, negation, symbols and numbers."""
    def normalized(text: str) -> str:
        text = re.sub(r"^(i|we) really (?=prefer\b)", r"\1 ", text.strip(), flags=re.I)
        return ' '.join(text.casefold().split()).rstrip('.!')

    return normalized(left) == normalized(right)


def replaces(new: str, old: str) -> bool:
    """True when `new` restates the subject of `old` with a different value.

    Two careful rules, no AI: (1) both read "<subject> is/are <value>" with the same subject and
    different values ("My favourite editor is Vim" -> "My favourite editor is Neovim");
    (2) `new` carries a change word ("now", "no longer", "switched to") and shares at least half
    its wording with `old`. Anything else is treated as a separate fact.
    """
    pair = [_PREFERENCE_PAIR.fullmatch(' '.join(value.casefold().split()).rstrip('.!')) for value in (new, old)]
    if all(pair):
        left, right = (m.groups() for m in pair)
        return left == (right[0], right[2], right[1], right[3])
    a, b = _SUBJECT_RE.match(new.strip().rstrip('.!')), _SUBJECT_RE.match(old.strip().rstrip('.!'))
    if a and b:
        sa, sb = _toks(a.group(1)), _toks(b.group(1))
        va, vb = set(_toks(a.group(2))), set(_toks(b.group(2)))
        single_value = re.search(r"\b(?:favou?rite|timezone|time zone|deadline|budget|editor|currency|"
                                 r"manager|name|city|country|price|limit|standup|default)\b", a.group(1), re.I)
        if sa and sa == sb:
            return bool(single_value and va and vb and va != vb)
    if _CHANGE_RE.search(new):
        wa, wb = set(_toks(_CHANGE_RE.sub(' ', new))), set(_toks(old))
        if len(wa) >= 3 and len(wb) >= 3 and len(wa & wb) / len(wa | wb) >= REPLACE_CUE_OVERLAP and wa != wb:
            return True
    return False
