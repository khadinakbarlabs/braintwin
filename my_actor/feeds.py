"""Safe RSS / Atom / YouTube-channel feed parsing (no third-party Actors, no API keys).

YouTube publishes a public feed per channel (youtube.com/feeds/videos.xml?channel_id=UC...),
so following a channel needs no scraper. Blogs and newsletters (Substack, Beehiiv, WordPress,
Ghost, Medium) expose RSS/Atom the same way.

Hardening: DOCTYPE/ENTITY declarations are refused (no entity-expansion tricks), input is
size-capped, item counts and field lengths are bounded, only http(s) item links are kept.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_FEED_CHARS = 1_500_000
MAX_ITEMS = 50
MAX_TITLE = 300
MAX_BODY = 4_000

_CHANNEL_ID_RE = re.compile(r'^UC[A-Za-z0-9_-]{22}$')
_PAGE_CHANNEL_RE = re.compile(r'(?:"channelId"\s*:\s*"|channel_id=|/channel/)(UC[A-Za-z0-9_-]{22})')
_TAG_RE = re.compile(r'<[^<>]{0,500}>')  # bounded: linear time even without any closing >
YOUTUBE_FEED = 'https://www.youtube.com/feeds/videos.xml?channel_id={cid}'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _clean(text: str | None, limit: int) -> str:
    plain = _TAG_RE.sub(' ', (text or '')[: limit * 4])
    plain = ' '.join(plain.replace('&nbsp;', ' ').split())
    return plain[:limit]


def _http_url(value: str | None) -> str | None:
    value = (value or '').strip()
    return value if urlparse(value).scheme in ('http', 'https') and len(value) <= 2_000 else None


def youtube_channel_id(source: str) -> str | None:
    """Channel id from a bare UC… id or a /channel/UC… URL. @handle URLs need a page lookup."""
    source = (source or '').strip()
    if _CHANNEL_ID_RE.match(source):
        return source
    parsed = urlparse(source)
    host = (parsed.hostname or '').lower()
    if host != 'youtube.com' and not host.endswith('.youtube.com'):
        return None
    m = re.search(r'/channel/(UC[A-Za-z0-9_-]{22})', parsed.path)
    if m:
        return m.group(1)
    qs = parse_qs(parsed.query).get('channel_id') or []
    return qs[0] if qs and _CHANNEL_ID_RE.match(qs[0]) else None


_CANON_RE = re.compile(r'rel=["\']canonical["\'][^>]*?/channel/(UC[A-Za-z0-9_-]{22})')
_EXTERNAL_RE = re.compile(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"')


def channel_id_from_page(html: str) -> str | None:
    head = (html or '')[:MAX_FEED_CHARS]
    for rx in (_CANON_RE, _EXTERNAL_RE):  # the page's OWN id, before any featured/related channel
        found = rx.search(head)
        if found:
            return found.group(1)
    m = _PAGE_CHANNEL_RE.search(head)
    return m.group(1) if m else None


def _truncate_feed(text: str) -> str:
    """Cut an oversized feed at the last complete item/entry and close the document."""
    cut = max(text.rfind('</item>'), text.rfind('</entry>'))
    if cut == -1:
        return text
    is_atom = text.rfind('</entry>') >= text.rfind('</item>')
    end = cut + (8 if is_atom else 7)
    return text[:end] + ('</feed>' if is_atom else '</channel></rss>')


def parse_feed(xml_text: str) -> dict[str, Any]:
    """Return {'ok', 'kind', 'title', 'items': [{id,title,url,published,author,summary,videoId}]}."""
    text = (xml_text or '').lstrip('﻿ \r\n\t')
    if len(text) > MAX_FEED_CHARS:
        text = _truncate_feed(text[:MAX_FEED_CHARS])
    head = text[:4_000].upper()
    if '<!DOCTYPE' in head or '<!ENTITY' in head or '<!ENTITY' in text.upper():
        return {'ok': False, 'error': 'UNSAFE_XML', 'message': 'Feed declares DOCTYPE/ENTITY; refused.'}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {'ok': False, 'error': 'NOT_A_FEED', 'message': f'XML parse error: {exc}'[:200]}

    root_name = _local(root.tag)
    if root_name == 'feed':
        entries = [e for e in root if _local(e.tag) == 'entry']
        kind = 'atom'
        feed_title = next((c.text for c in root if _local(c.tag) == 'title'), '') or ''
    elif root_name == 'rss' or root_name == 'RDF':
        channel = next((c for c in root if _local(c.tag) == 'channel'), root)
        entries = [e for e in list(channel) + list(root) if _local(e.tag) == 'item']
        kind = 'rss'
        feed_title = next((c.text for c in channel if _local(c.tag) == 'title'), '') or ''
    else:
        return {'ok': False, 'error': 'NOT_A_FEED', 'message': f'Unexpected root element <{root_name}>.'}

    items: list[dict[str, Any]] = []
    for entry in entries[:MAX_ITEMS]:
        fields: dict[str, str] = {}
        link = None
        for child in entry.iter():
            name = _local(child.tag)
            if name == 'link':
                href = child.get('href') or child.text
                rel = child.get('rel')
                if href and (link is None or rel in (None, 'alternate')):
                    link = href
            elif name in ('title', 'id', 'guid', 'published', 'updated', 'pubDate', 'date', 'name', 'creator', 'videoId'):
                fields.setdefault(name, (child.text or '').strip())
            elif name in ('description', 'summary', 'encoded', 'content') and child.text:
                fields.setdefault('body', child.text)
        url = _http_url(link)
        title = _clean(fields.get('title'), MAX_TITLE)
        if not url or not title:
            continue
        video_id = fields.get('videoId') or None
        items.append(
            {
                'id': (fields.get('id') or fields.get('guid') or url)[:300],
                'title': title,
                'url': url,
                'published': (fields.get('published') or fields.get('pubDate') or fields.get('date') or fields.get('updated') or '')[:60],
                'author': _clean(fields.get('name') or fields.get('creator'), 120),
                'summary': _clean(fields.get('body'), MAX_BODY),
                'videoId': video_id,
            }
        )
    return {'ok': True, 'kind': kind, 'title': _clean(feed_title, MAX_TITLE), 'items': items}
