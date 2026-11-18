"""Verbatim lookup. Long notes carry a small term filter so most misses cost no body reads."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from .origin import Collection, collection_fields, matches_collection
from .ranking import is_expired

_WORD = re.compile(r'\w+')
FILTER_BITS = 2048
READ_LIMIT = 48
PAGE_CHARS = 4000


def _bits(term: str) -> int:
    digest = hashlib.blake2s(term.encode(), digest_size=4).digest()
    a, b = int.from_bytes(digest[:2], 'little'), int.from_bytes(digest[2:], 'little')
    return (1 << (a % FILTER_BITS)) | (1 << (b % FILTER_BITS))


def term_filter(content: str) -> str:
    mask = 0
    for word in set(_WORD.findall(content.casefold())):
        mask |= _bits(word)
    return format(mask, 'x')


def visible(record, scope_tag, now):
    return bool(record and record.get('status') == 'approved' and not is_expired(record, now)
                and (not scope_tag or scope_tag in {str(t).lower() for t in record.get('tags', [])}))


def evidence_line(row):
    """Give the agent one copy of the evidence, its lookup ID and its first source."""
    body = row.get('text') or row.get('summary') or ''
    title = row.get('title') or ''
    citation = next(iter(row.get('citations') or []), None)
    url = citation
    if isinstance(citation, dict):
        url = citation.get('url') or citation.get('uri') or citation.get('path')
        if not url and citation.get('type') == 'chat':
            url = f"chat {citation.get('conversation', '')}, message {citation.get('message', '')}"
    url = url or row.get('url')
    source = f" (source: {url})" if url else ''
    prefix = f'{title}: ' if title and not body.casefold().startswith(title.casefold()) else ''
    limit_note = ' (source input limit reached)' if row.get('sourceLimited') else ''
    label = ''
    if row.get('collection') == 'agent':
        label = 'Agent work · '
    elif row.get('collection') == 'reference':
        coverage = row.get('sourceCoverage')
        label = f"Reference ({row.get('sourcePlatform') or 'source'}{', ' + coverage if coverage else ''}) · "
    return f"[{row['id']}] {label}{prefix}{body}{source}{limit_note}"


async def by_id(brain, mid, scope_tag, offset, *, collection: Collection | None = None):
    rec = await brain.get_memory(mid)
    if not visible(rec, scope_tag, datetime.now(UTC)) or not matches_collection(rec, collection):
        return {'ok': True, 'count': 0, 'results': []}
    body = rec.get('content') or ''
    end = offset + PAGE_CHARS
    row = {'id': mid, 'title': rec.get('title'), 'text': body[offset:end],
           'citations': rec.get('citations') or [], **collection_fields(rec)}
    if (rec.get('provenance') or {}).get('inputLimitReached'):
        row['sourceLimited'] = True
    return {'ok': True, 'count': 1, 'results': [row], **({'nextOffset': end} if end < len(body) else {})}


async def by_phrase(brain, phrase, scope_tag, limit, after, *, collection: Collection | None = None):
    # Whole word edges make the term filter sound even for punctuation-heavy commands.
    rx = re.compile((r'(?<!\w)' if phrase[0].isalnum() or phrase[0] == '_' else '')
                    + re.escape(phrase)
                    + (r'(?!\w)' if phrase[-1].isalnum() or phrase[-1] == '_' else ''), re.IGNORECASE)
    # Python's Unicode IGNORECASE has equivalences beyond casefold; prune only ASCII-only records and queries.
    required = int(term_filter(phrase), 16) if phrase.isascii() else 0
    entries = await brain._current_index_entries()
    now = datetime.now(UTC)
    found = []
    reads = 0
    last = ''
    for mid in sorted(entries):
        if mid <= after:
            continue
        entry = entries[mid]
        if not visible(entry, scope_tag, now) or not matches_collection(entry, collection):
            continue
        title = entry.get('title') or ''
        summary = entry.get('summary') or ''
        match = rx.search(summary)
        if match and summary.endswith('…') and match.end() >= len(summary) - 1:
            match = None  # the truncation marker may sit inside a longer word
        if match:
            body = summary
        elif rx.search(title):
            body = title
            match = rx.search(title)
        else:
            bloom = entry.get('bodyFilter')
            if bloom is None and not summary.endswith('…'):
                continue
            if required and bloom and int(bloom, 16) & required != required:
                continue
            if reads == READ_LIMIT:
                break
            reads += 1
            rec = await brain.get_memory(mid)
            if not visible(rec, scope_tag, now) or not matches_collection(rec, collection):
                last = mid
                continue
            body = rec.get('content') or ''
            match = rx.search(body)
        last = mid
        if match:
            excerpt = body[max(0, match.start() - 75):match.end() + 100].strip()
            found.append({
                'id': mid, 'title': title, 'text': excerpt, **collection_fields(entry),
                'citations': entry.get('sourceUris') or ([entry['sourceRef']] if entry.get('sourceRef') else []),
            })
            if len(found) == limit:
                break
    else:
        last = ''
    return {'ok': True, 'count': len(found), 'results': found, **({'nextAfter': last} if last else {})}
