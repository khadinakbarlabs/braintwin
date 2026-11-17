"""Hybrid local ranking v2: keywords + word variants + entity boost + freshness, with a cache.

Design (learned from studying how Mem0 ranks: semantic + keyword + entity boost; we keep the
idea, write our own code, and need NO embedding service, LLM or model download):

  score = 0.55 * keyword (BM25 on lightly-stemmed words, squashed to 0..1)
        + 0.20 * fuzzy   (BM25 on word VARIANTS: typos, related words, words learned from your notes)
        + 0.50 * cover   (share of the question's words, weighted by rarity, that the memory contains)
        + 0.05 * name    (a Capitalised word from the question, such as a person's name, is present)
        + entity boost   (@handles, #tags, domains, Capitalised Names, "quoted phrases")
        + 0.10 * fresh   (exponential decay by age)

Speed: every memory is analysed ONCE (words, entities, dates) and kept in a `RankIndex`
(inverted index). A search only touches memories that share a word, variant or entity with the
query, instead of re-reading every memory.

Honest limit: variants are fuzzy spelling matches, a small hand-made synonym list (semantic.py)
and words that co-occur with the query in your own notes. That is not neural meaning search.
`Embedder` is the hook for a real embedding model later.
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol

from .semantic import related_stems

SCORER = 'hybrid-local-v3'
W_KEYWORD = 0.55
W_FUZZY = 0.20
W_FRESH = 0.10
W_COVER = 0.5  # share of the query's (rarity-weighted) words that a memory contains
KW_MID_BASE = 3.0  # keyword squash midpoint = base + per_term * number of query words
KW_MID_PER_TERM = 0.0
NAME_BOOST = 0.05  # a Capitalised query word (a name) found in the memory
ENTITY_BOOST_EACH = 0.12
ENTITY_BOOST_MAX = 0.25
FRESH_HALF_LIFE_DAYS = 180.0
MAX_DOC_CHARS = 2_500
MAX_QUERY_CHARS = 1_000

# Word-variant search
FUZZY_MIN_DICE = 0.55  # spelling similarity needed to treat two words as variants
FUZZY_MIN_WORD = 4
FUZZY_WEIGHT = 0.6
RELATED_WEIGHT = 0.5
LEARNED_WEIGHT = 0.4
LEARNED_MIN_DOCS = 20  # below this many memories, co-occurrence is noise
LEARNED_SAMPLE_DOCS = 300
LEARNED_MAX_PER_TERM = 3
LEARNED_MIN_LIFT = 3.0
EDIT1_DICE = 0.80  # score given to a word one edit away (typo, swap, missing letter)
EDIT2_DICE = 0.65  # ... two edits away, only for long words
EDIT2_MIN_WORD = 7
EDIT_MAX_LEN_DIFF = 2
# Common filler words dropped from QUERIES only (never from stored memories). Kept short on purpose.
QUERY_STOPWORDS = frozenset(
    'a an the of to in on at by for with from and or but if as is are was were be been am do does did '
    'i me my we our you your it its this that these those what when where who whom how why which '
    'before after about into over under so than then there here can could should would will shall may might '
    'not no yes also just very any some'.split()
)

_WORD_RE = re.compile(r'[a-z0-9_]+')
_ENTITY_RES = (
    re.compile(r'@[A-Za-z0-9_.]{2,40}'),
    re.compile(r'#[A-Za-z0-9_]{2,40}'),
    re.compile(r'\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|ai|dev|app|co|edu|gov)\b', re.IGNORECASE),
    re.compile(r'\b[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+)+\b'),  # Two or more Capitalised Words
    re.compile(r'"([^"]{3,60})"'),
)
_NAME_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]{2,}\b")  # Capitalised word that does not start a sentence
_SUFFIXES = ('ingly', 'edly', 'ings', 'ing', 'ies', 'ied', 'es', 'ed', 's', 'ly')


class Embedder(Protocol):
    """Future hook: plug in a real embedding model without touching the ranker."""

    def embed(self, text: str) -> list[float]: ...


@lru_cache(maxsize=100_000)
def stem(word: str) -> str:
    """Tiny suffix stripper: enough to match plurals and verb forms, never over-stems short words."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)]
            return base + 'y' if suffix in ('ies', 'ied') else base
    return word


def edit_distance_le(a: str, b: str, limit: int) -> bool:
    """True if `a` and `b` differ by at most `limit` edits (insert, delete, substitute, swap neighbours)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return False
    prev2: list[int] | None = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return False
        prev2, prev = prev, cur
    return prev[lb] <= limit


def stem_tokens(text: str) -> list[str]:
    return [stem(w) for w in _WORD_RE.findall((text or '').lower())]


def trigrams(text: str) -> Counter[str]:
    out: Counter[str] = Counter()
    for word in _WORD_RE.findall((text or '').lower()):
        for g in _word_trigrams(word):
            out[g] += 1
    return out


@lru_cache(maxsize=100_000)
def _word_trigrams(word: str) -> frozenset[str]:
    padded = f'  {word} '
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


def extract_entities(text: str) -> set[str]:
    """Lower-cased entity strings found in `text` (handles, tags, domains, names, quoted phrases)."""
    found: set[str] = set()
    sample = (text or '')[:MAX_DOC_CHARS]
    for rx in _ENTITY_RES:
        for m in rx.finditer(sample):
            value = (m.group(1) if m.groups() else m.group(0)).strip().lower()
            if value:
                found.add(value)
    return found


def doc_text(entry: dict[str, Any]) -> str:
    tags = ' '.join(str(t) for t in (entry.get('tags') or []))
    parts = [str(entry.get('title') or ''), str(entry.get('summary') or entry.get('content') or ''), tags]
    return ' '.join(parts)[:MAX_DOC_CHARS]


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _epoch(value: Any) -> float | None:
    parsed = _parse_ts(value)
    return parsed.timestamp() if parsed else None


def freshness(entry: dict[str, Any], now: datetime) -> float:
    ts = _parse_ts(entry.get('updatedAt')) or _parse_ts(entry.get('approvedAt'))
    if ts is None:
        return 0.5
    age_days = max((now - ts).total_seconds() / 86_400.0, 0.0)
    return 0.5 ** (age_days / FRESH_HALF_LIFE_DAYS)


def is_expired(entry: dict[str, Any], now: datetime) -> bool:
    expires = _parse_ts(entry.get('expiresAt'))
    return expires is not None and expires <= now


def _squash(x: float, mid: float = 3.0) -> float:
    return x / (x + mid) if x > 0 else 0.0


@dataclass(slots=True)
class _Feat:
    sig: int
    tf: dict[str, int]
    dl: int
    ents: frozenset[str]
    ts: float | None
    exp: float | None


class RankIndex:
    """Per-process derived search data: analyse each memory once, search many times.

    Holds, per memory: stemmed word counts, entities, dates. Plus inverted maps word->memories,
    entity->memories and trigram->words (for spelling variants). Safe to rebuild from the stored
    index at any time; it never holds anything that is not derivable from it.
    """

    def __init__(self) -> None:
        self.feats: dict[str, _Feat] = {}
        self.post: dict[str, set[str]] = {}
        self.ent_post: dict[str, set[str]] = {}
        self._tri_words: dict[str, set[str]] = {}

    def __len__(self) -> int:
        return len(self.feats)

    # --- maintenance -----------------------------------------------------------------------
    def sync(self, entries: Iterable[dict[str, Any]]) -> None:
        """Make the cache match `entries` exactly: add new, refresh changed, drop missing."""
        seen: set[str] = set()
        for entry in entries:
            mid = entry.get('id') if isinstance(entry, dict) else None
            if isinstance(mid, str) and mid:
                seen.add(mid)
                self._upsert(mid, entry)
        for gone in [m for m in self.feats if m not in seen]:
            self._remove(gone)

    def ensure(self, entry: dict[str, Any]) -> None:
        mid = entry.get('id')
        if isinstance(mid, str) and mid and mid not in self.feats:
            self._upsert(mid, entry)

    def _upsert(self, mid: str, entry: dict[str, Any]) -> None:
        text = doc_text(entry)
        tags = [str(t).lower() for t in (entry.get('tags') or [])]
        sig = hash((text, tuple(tags), entry.get('updatedAt'), entry.get('approvedAt'), entry.get('expiresAt')))
        old = self.feats.get(mid)
        if old is not None:
            if old.sig == sig:
                return
            self._remove(mid)
        tokens = stem_tokens(text)
        tf: dict[str, int] = {}
        for tok in tokens:
            key = sys.intern(tok)
            tf[key] = tf.get(key, 0) + 1
        ents = frozenset(extract_entities(text) | set(tags))
        feat = _Feat(
            sig=sig,
            tf=tf,
            dl=len(tokens) or 1,
            ents=ents,
            ts=_epoch(entry.get('updatedAt')) or _epoch(entry.get('approvedAt')),
            exp=_epoch(entry.get('expiresAt')),
        )
        self.feats[mid] = feat
        for word in tf:
            bucket = self.post.get(word)
            if bucket is None:
                self.post[word] = {mid}
                for g in _word_trigrams(word):
                    self._tri_words.setdefault(g, set()).add(word)
            else:
                bucket.add(mid)
        for ent in ents:
            self.ent_post.setdefault(ent, set()).add(mid)

    def _remove(self, mid: str) -> None:
        feat = self.feats.pop(mid, None)
        if feat is None:
            return
        for word in feat.tf:
            bucket = self.post.get(word)
            if bucket is None:
                continue
            bucket.discard(mid)
            if not bucket:
                del self.post[word]
                for g in _word_trigrams(word):
                    words = self._tri_words.get(g)
                    if words is not None:
                        words.discard(word)
                        if not words:
                            del self._tri_words[g]
        for ent in feat.ents:
            bucket = self.ent_post.get(ent)
            if bucket is not None:
                bucket.discard(mid)
                if not bucket:
                    del self.ent_post[ent]

    # --- variant lookup --------------------------------------------------------------------
    def spelling_variants(self, word: str) -> list[tuple[str, float]]:
        """Indexed words spelled like `word` (typos, forms): [(variant, dice 0..1)] best first."""
        if len(word) < FUZZY_MIN_WORD:
            return []
        grams = _word_trigrams(word)
        counts: Counter[str] = Counter()
        for g in grams:
            for other in self._tri_words.get(g, ()):
                counts[other] += 1
        out: list[tuple[str, float]] = []
        limit2 = len(word) >= EDIT2_MIN_WORD
        unknown = word not in self.post  # edit-distance only for words nobody wrote (likely typos)
        for other, shared in counts.items():
            if other == word or len(other) < FUZZY_MIN_WORD:
                continue
            dice = 2.0 * shared / (len(grams) + len(_word_trigrams(other)))
            if unknown and shared >= 2 and abs(len(other) - len(word)) <= EDIT_MAX_LEN_DIFF:
                if edit_distance_le(word, other, 1):
                    dice = max(dice, EDIT1_DICE)
                elif limit2 and len(other) >= EDIT2_MIN_WORD and edit_distance_le(word, other, 2):
                    dice = max(dice, EDIT2_DICE)
            if dice >= FUZZY_MIN_DICE:
                out.append((other, dice))
        out.sort(key=lambda p: p[1], reverse=True)
        return out[:4]

    def learned_variants(self, term: str, live: set[str], exclude: set[str]) -> list[str]:
        """Words that appear mostly together with `term` in these memories (lift), best first."""
        docs_with = self.post.get(term)
        if not docs_with:
            return []
        sample = list(docs_with & live)[:LEARNED_SAMPLE_DOCS]
        if len(sample) < 2:
            return []
        cooc: Counter[str] = Counter()
        for mid in sample:
            cooc.update(self.feats[mid].tf.keys())
        n_all = max(len(self.feats), 1)
        scored: list[tuple[float, str]] = []
        for other, count in cooc.items():
            if other in exclude or count < 2:
                continue
            share = count / len(sample)
            df_other = len(self.post.get(other, ()))
            if share < 0.3 or df_other > n_all * 0.5:
                continue
            lift = share / (df_other / n_all)
            if lift >= LEARNED_MIN_LIFT:
                scored.append((lift * math.log(count + 1), other))
        scored.sort(reverse=True)
        return [w for _, w in scored[:LEARNED_MAX_PER_TERM]]


def hybrid_rank(
    query: str,
    entries: list[dict[str, Any]],
    *,
    limit: int = 20,
    now: datetime | None = None,
    k1: float = 1.5,
    b: float = 0.75,
    rank_index: RankIndex | None = None,
) -> list[dict[str, Any]]:
    """Rank thin index entries. Returns [{entry, score, matchedTerms, signals}] best first.

    Pass a long-lived `rank_index` (already synced to the stored index) for speed; without one a
    temporary index is built from `entries` on every call (correct, but the slow path).
    """
    query = (query or '')[:MAX_QUERY_CHARS]
    q_raw = _WORD_RE.findall(query.lower())
    if not q_raw or not entries:
        return []
    content = [w for w in q_raw if w not in QUERY_STOPWORDS]
    if content:  # a query made only of filler words is searched as written
        q_raw = content
    now = now or datetime.now(UTC)
    now_ts = now.timestamp()
    idx = rank_index
    if idx is None:
        idx = RankIndex()
        idx.sync(entries)
    else:
        for entry in entries:
            idx.ensure(entry)

    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        mid = entry.get('id')
        if isinstance(mid, str) and mid in idx.feats:
            by_id[mid] = entry
    live = {m for m in by_id if not ((idx.feats[m].exp is not None) and idx.feats[m].exp <= now_ts)}  # type: ignore[operator]
    if not live:
        return []

    q_stems = [stem(w) for w in q_raw]
    q_unique = list(dict.fromkeys(q_stems))
    q_set = set(q_unique)
    q_entities = extract_entities(query)

    # Word variants: spelling neighbours, near-synonyms, and words learned from these memories.
    variants: dict[str, tuple[float, str]] = {}

    def add_variant(word: str, weight: float, kind: str) -> None:
        if word in q_set or word not in idx.post:
            return
        if word not in variants or variants[word][0] < weight:
            variants[word] = (weight, kind)

    for term in q_unique:
        for other, dice in idx.spelling_variants(term):
            add_variant(other, FUZZY_WEIGHT * dice, 'spelling')
        for other in related_stems(term):
            add_variant(other, RELATED_WEIGHT, 'related')
        if len(live) >= LEARNED_MIN_DOCS:
            for other in idx.learned_variants(term, live, q_set):
                add_variant(other, LEARNED_WEIGHT, 'learned')

    n_docs = len(live)
    avgdl = (sum(idx.feats[m].dl for m in live) / n_docs) or 1.0

    def idf_of(word: str) -> float:
        bucket = idx.post.get(word)
        df = len(live & bucket) if bucket else 0
        return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

    idf_exact = {t: idf_of(t) for t in q_unique if t in idx.post}
    idf_var = {w: idf_of(w) for w in variants}

    idf_total = sum(idf_exact.values())
    kw_mid = KW_MID_BASE + KW_MID_PER_TERM * len(idf_exact)
    q_names = {stem(w.lower()) for w in _NAME_RE.findall(query)[0:8]} & set(idf_exact)

    candidates: set[str] = set()
    for word in (*idf_exact, *variants):
        bucket = idx.post.get(word)
        if bucket:
            candidates |= bucket & live
    for ent in q_entities:
        bucket = idx.ent_post.get(ent)
        if bucket:
            candidates |= bucket & live

    scored: list[dict[str, Any]] = []
    for mid in candidates:
        feat = idx.feats[mid]
        tf = feat.tf
        norm = 1 - b + b * feat.dl / avgdl
        kw_raw = 0.0
        hit_stems: set[str] = set()
        for term, idf in idf_exact.items():
            f = tf.get(term, 0)
            if f:
                hit_stems.add(term)
                kw_raw += idf * (f * (k1 + 1)) / (f + k1 * norm)
        fz_raw = 0.0
        hit_variants: list[str] = []
        for word, (weight, _kind) in variants.items():
            f = tf.get(word, 0)
            if f:
                hit_variants.append(word)
                fz_raw += weight * idf_var[word] * (f * (k1 + 1)) / (f + k1 * norm)
        keyword = _squash(kw_raw, mid=kw_mid)
        fuzzy = _squash(fz_raw, mid=1.5)
        cover = (sum(idf_exact[t] for t in hit_stems) / idf_total) if idf_total else 0.0
        name_hit = sum(1 for nm in q_names if tf.get(nm)) / len(q_names) if q_names else 0.0
        entity_hits = sorted(q_entities & feat.ents)
        entity = min(ENTITY_BOOST_EACH * len(entity_hits), ENTITY_BOOST_MAX)
        if keyword == 0.0 and fuzzy == 0.0 and entity == 0.0:
            continue
        if feat.ts is None:
            fresh = 0.5
        else:
            fresh = 0.5 ** (max((now_ts - feat.ts) / 86_400.0, 0.0) / FRESH_HALF_LIFE_DAYS)
        score = W_KEYWORD * keyword + W_FUZZY * fuzzy + entity + W_FRESH * fresh + W_COVER * cover + NAME_BOOST * name_hit
        scored.append(
            {
                'entry': by_id[mid],
                'score': round(score, 6),
                'matchedTerms': [w for w, s in zip(q_raw, q_stems, strict=True) if s in hit_stems],
                'signals': {
                    'keyword': round(keyword, 4),
                    'fuzzy': round(fuzzy, 4),
                    'entity': round(entity, 4),
                    'fresh': round(fresh, 4),
                    'entities': entity_hits,
                    'expanded': sorted(
                        ({'word': w, 'kind': variants[w][1]} for w in hit_variants), key=lambda d: d['word']
                    ),
                },
            }
        )
    scored.sort(key=lambda r: (-r['score'], str(r['entry'].get('id'))))
    return scored[: max(1, min(limit, 100))]
