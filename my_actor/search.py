"""Lightweight BM25-style keyword scoring for memory search (no heavy NLP deps)."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .constants import INDEX_TOKEN_CHARS, SUMMARY_CHARS
from .origin import card_fields, origin_brief
from .purpose import purpose_fields

_TOKEN_RE = re.compile(r'[a-z0-9_]+', re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or '')]


def make_summary(content: str, *, limit: int = SUMMARY_CHARS) -> str:
    """First-N chars of content for cards + index (no HTML re-parse)."""
    text = (content or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '…'


def _doc_text(record: dict[str, Any]) -> str:
    """Prefer summary over full content so index cards score without 50k bodies."""
    tags = record.get('tags') or []
    tag_str = ' '.join(str(t) for t in tags)
    body = str(record.get('summary') or record.get('content') or '')
    if len(body) > INDEX_TOKEN_CHARS:
        body = body[:INDEX_TOKEN_CHARS]
    parts = [
        str(record.get('title') or ''),
        body,
        str(record.get('type') or ''),
        str(record.get('dedupeKey') or ''),
        tag_str,
    ]
    return ' '.join(parts)


def matched_terms(query: str, record: dict[str, Any]) -> list[str]:
    """Return query tokens that appear in the record text (for retrieval receipts)."""
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    doc = set(tokenize(_doc_text(record)))
    seen: set[str] = set()
    out: list[str] = []
    for term in q_tokens:
        if term in doc and term not in seen:
            seen.add(term)
            out.append(term)
    return out


def memory_card(record: dict[str, Any], *, snippet_chars: int = 240) -> dict[str, Any]:
    """Progressive-disclosure card — keep full bodies behind memory_explain."""
    summary = str(record.get('summary') or '')
    content = str(record.get('content') or '')
    raw = summary or content
    snippet = raw[:snippet_chars] + ('…' if len(raw) > snippet_chars else '')
    return {
        'id': record.get('id'),
        'status': record.get('status'),
        'type': record.get('type'),
        'title': record.get('title'),
        'snippet': snippet,
        'summary': summary or make_summary(content, limit=snippet_chars),
        'tags': list(record.get('tags') or []),
        'confidence': record.get('confidence'),
        'citations': list(record.get('citations') or []),
        'updatedAt': record.get('updatedAt'),
        'approvedAt': record.get('approvedAt'),
        **purpose_fields(record.get('type'), record.get('tags')),
        **card_fields(record.get('type'), record.get('tags'), origin_brief(record)),
    }


def card_from_index_entry(entry: dict[str, Any], *, snippet_chars: int = 240) -> dict[str, Any]:
    """Card built only from thin index fields (no KV body hydrate)."""
    summary = str(entry.get('summary') or '')
    snippet = summary[:snippet_chars] + ('…' if len(summary) > snippet_chars else '')
    uris = list(entry.get('sourceUris') or [])
    return {
        'id': entry.get('id'),
        'status': entry.get('status'),
        'type': entry.get('type'),
        'title': entry.get('title'),
        'snippet': snippet,
        'summary': summary,
        'tags': list(entry.get('tags') or []),
        'confidence': entry.get('confidence'),
        'citations': [{'url': u} for u in uris],
        'updatedAt': entry.get('updatedAt'),
        'approvedAt': entry.get('approvedAt'),
        **purpose_fields(entry.get('type'), entry.get('tags')),
        **card_fields(entry.get('type'), entry.get('tags'), entry.get('origin')),
    }


def index_entry_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """Thin search-index row stored under __search_index."""
    content = str(record.get('content') or '')
    summary = str(record.get('summary') or '') or make_summary(content)
    return {
        'id': record.get('id'),
        'status': record.get('status'),
        'type': record.get('type') or 'note',
        'title': record.get('title') or '',
        'summary': summary,
        'tags': list(record.get('tags') or []),
        'dedupeKey': record.get('dedupeKey') or '',
        'confidence': record.get('confidence'),
        'sourceUris': citation_uris(record),
        'updatedAt': record.get('updatedAt'),
        'approvedAt': record.get('approvedAt'),
        'expiresAt': record.get('expiresAt'),
        'publishedAt': (record.get('provenance') or {}).get('published'),
        'origin': origin_brief(record),
    }


def record_from_index_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Synthetic record for BM25 — summary stands in for content."""
    return {
        'id': entry.get('id'),
        'status': entry.get('status'),
        'type': entry.get('type'),
        'title': entry.get('title'),
        'summary': entry.get('summary'),
        'content': entry.get('summary'),
        'tags': list(entry.get('tags') or []),
        'dedupeKey': entry.get('dedupeKey') or '',
        'confidence': entry.get('confidence'),
        'citations': [{'url': u} for u in (entry.get('sourceUris') or [])],
        'updatedAt': entry.get('updatedAt'),
        'approvedAt': entry.get('approvedAt'),
    }


def citation_uris(record: dict[str, Any]) -> list[str]:
    uris: list[str] = []
    for cite in record.get('citations') or []:
        if isinstance(cite, str) and cite.strip():
            uris.append(cite.strip())
            continue
        if not isinstance(cite, dict):
            continue
        for key in ('url', 'uri', 'sourceUri', 'href'):
            val = cite.get(key)
            if isinstance(val, str) and val.strip():
                uris.append(val.strip())
                break
    seen: set[str] = set()
    out: list[str] = []
    for u in uris:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def bm25_rank(
    query: str,
    records: list[dict[str, Any]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Score records with a simple BM25 variant; returns [{memory, score, matchedTerms}, ...] sorted desc."""
    q_tokens = tokenize(query)
    if not q_tokens or not records:
        return []

    docs = [(rec, tokenize(_doc_text(rec))) for rec in records]
    N = len(docs)
    avgdl = sum(len(toks) for _, toks in docs) / max(N, 1)

    df: Counter[str] = Counter()
    for _, toks in docs:
        df.update(set(toks))

    idf: dict[str, float] = {}
    for term in set(q_tokens):
        n_q = df.get(term, 0)
        idf[term] = math.log(1 + (N - n_q + 0.5) / (n_q + 0.5))

    scored: list[dict[str, Any]] = []
    for rec, toks in docs:
        if not toks:
            continue
        tf = Counter(toks)
        dl = len(toks)
        score = 0.0
        hits: list[str] = []
        for term in q_tokens:
            f = tf.get(term, 0)
            if f == 0:
                continue
            if term not in hits:
                hits.append(term)
            denom = f + k1 * (1 - b + b * dl / avgdl)
            score += idf.get(term, 0.0) * (f * (k1 + 1)) / denom
        if score > 0:
            scored.append({'memory': rec, 'score': round(score, 6), 'matchedTerms': hits})

    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored[: max(1, min(limit, 100))]
