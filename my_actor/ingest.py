"""Lightweight in-Actor HTTP fetch for custom source URLs (no third-party Actors).

Callers: my_actor/brain.py, my_actor/tools.py, my_actor/ingest_worker.py.

Hardening (the fetched page is untrusted and the caller may be a prompt-injected agent):
- Every redirect hop is checked BEFORE a request is sent (never follow-then-check).
- Addresses must be globally routable: private, loopback, link-local, CGNAT/Tailscale
  (100.64.0.0/10), site-local, multicast and IPv4-mapped forms are all refused.
- DNS runs off the event loop, so a slow resolver cannot stall the server.
- A total deadline bounds the whole fetch (httpx timeouts are per read: a slow-drip server
  could otherwise hold a worker forever).
- The body is read as raw bytes and decompressed with a size-bounded decoder, so a small
  gzip bomb cannot expand past the cap in memory.
- HTML is stripped with the stdlib parser (linear), not regexes (quadratic on hostile input).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import zlib
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 1_500_000
MAX_PARSE_CHARS = 400_000  # parse at most this much markup, however big the download
MAX_TEXT_CHARS = 50_000
TIMEOUT_SECS = 25.0  # per read
TOTAL_TIMEOUT_SECS = 60.0  # whole fetch, all hops
DNS_TIMEOUT_SECS = 5.0
MAX_REDIRECTS = 5
USER_AGENT = (
    'Braintwin/0.1 (+https://apify.com/khadinakbar/braintwin; personal-memory ingest)'
)
_SKIP_TAGS = frozenset({'script', 'style', 'noscript'})


class _UnsupportedEncoding(Exception):
    pass


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is loopback, however it is spelled
    # is_global alone misses deprecated IPv6 site-local (fec0::/10); is_multicast is explicit.
    return (not ip.is_global) or ip.is_multicast or bool(getattr(ip, 'is_site_local', False))


def _host_is_blocked(hostname: str) -> bool:
    """Block localhost, internal names and anything that resolves to a non-global address."""
    host = (hostname or '').strip().lower().rstrip('.')
    if not host:
        return True
    if host in {'localhost', 'metadata.google.internal', 'metadata'} or host.endswith(
        ('.localhost', '.internal')
    ):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return True  # unresolvable or malformed host: refuse rather than guess
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split('%')[0])
        except ValueError:
            return True
        if _ip_is_blocked(ip):
            return True
    return False


def assert_public_http_url(url: str) -> str | None:
    """Return an error code if URL is not a safe public http(s) target (blocking DNS)."""
    if not _is_http_url(url):
        return 'INVALID_URL'
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        return 'CREDENTIALS_IN_URL'
    if _host_is_blocked(parsed.hostname or ''):
        return 'BLOCKED_HOST'
    return None


async def check_url_async(url: str) -> str | None:
    """assert_public_http_url without blocking the event loop (DNS runs in a thread)."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(assert_public_http_url, url), DNS_TIMEOUT_SECS)
    except TimeoutError:
        return 'BLOCKED_HOST'


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ''
        self.open_graph: dict[str, str] = {}
        self.parts: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == 'title':
            self._in_title = True
        elif tag == 'meta' and not self._skip:
            values = dict(attrs)
            key, content = values.get('property'), values.get('content')
            if key in ('og:title', 'og:description') and content:
                self.open_graph.setdefault(key, ' '.join(content.split()))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == 'title':
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def _extract_html(html: str) -> _TextExtractor:
    extractor = _TextExtractor()
    extractor.feed((html or '')[:MAX_PARSE_CHARS])
    extractor.close()
    return extractor


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, plain_text) from HTML. Linear-time stdlib parser; input is capped."""
    extractor = _extract_html(html)
    title = ' '.join(extractor.title.split())
    text = ' '.join(' '.join(extractor.parts).split())
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + '…'
    return title, text


async def _read_body(response: httpx.Response) -> bytes:
    """Read at most MAX_BYTES of DECODED body; never buffer more, even from a gzip bomb."""
    if response.is_stream_consumed:
        # Already read AND decoded by httpx (only happens for pre-buffered test doubles; a real
        # network response streams). The bytes are in memory already, so just cap them.
        return response.content[:MAX_BYTES]
    encoding = (response.headers.get('content-encoding') or 'identity').strip().lower()
    if encoding in ('', 'identity'):
        decoder = None
    elif encoding in ('gzip', 'x-gzip', 'deflate'):
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 32)  # auto-detects gzip / zlib headers
    else:
        raise _UnsupportedEncoding(encoding)

    out = bytearray()
    async for chunk in response.aiter_raw():
        if decoder is None:
            out += chunk
        else:
            data = chunk
            while data and len(out) < MAX_BYTES:
                out += decoder.decompress(data, MAX_BYTES - len(out))
                data = decoder.unconsumed_tail
        if len(out) >= MAX_BYTES:
            break
    return bytes(out[:MAX_BYTES])


async def _fetch(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(
        # Redirects are followed by hand so EVERY hop is checked before a request is sent.
        follow_redirects=False,
        timeout=TIMEOUT_SECS,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
        },
    ) as client:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            if hop:
                hop_block = await check_url_async(current)
                if hop_block:
                    return {
                        'ok': False,
                        'error': hop_block,
                        'message': 'Redirect target is not a public http/https URL.',
                        'url': url,
                        'finalUrl': current,
                    }
            async with client.stream('GET', current) as response:
                location = response.headers.get('location')
                if response.is_redirect and location:
                    current = urljoin(current, location)
                    continue
                body = await _read_body(response)
                return {
                    '_body': body,
                    '_final_url': current,
                    '_status': response.status_code,
                    '_ok': response.is_success,
                    '_content_type': (response.headers.get('content-type') or '').lower(),
                    '_encoding': response.encoding or 'utf-8',
                }
    return {
        'ok': False,
        'error': 'TOO_MANY_REDIRECTS',
        'message': f'Exceeded {MAX_REDIRECTS} redirects',
        'url': url,
    }


async def fetch_landing_page(url: str) -> dict[str, Any]:
    """
    Fetch a public landing page over HTTP(S) with httpx.

    Does not call Apify Store Actors. Returns structured ingest payload or error.
    """
    blocked = await check_url_async(url)
    if blocked:
        return {
            'ok': False,
            'error': blocked,
            'message': 'Only public http/https URLs are supported for source ingest.',
            'url': url,
        }

    from .youtube import fetch_video, video_id  # a video link: save what was said, not the page shell

    if video_id(url):
        video = await fetch_video(url)
        if video is not None:
            return video
        return {'ok': False, 'error': 'SOURCE_TEXT_UNAVAILABLE', 'url': url,
                'message': 'Video content could not be read. You can add its transcript with the link.'}

    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECS):
            raw = await _fetch(url)
    except (TimeoutError, httpx.TimeoutException):
        return {'ok': False, 'error': 'TIMEOUT', 'message': f'Timed out after {TOTAL_TIMEOUT_SECS:.0f}s', 'url': url}
    except _UnsupportedEncoding as exc:
        return {'ok': False, 'error': 'UNSUPPORTED_ENCODING', 'message': f'Unsupported content-encoding {exc}', 'url': url}
    except zlib.error:
        return {'ok': False, 'error': 'BAD_ENCODING', 'message': 'Response body could not be decompressed.', 'url': url}
    except httpx.HTTPError as exc:
        return {'ok': False, 'error': 'HTTP_ERROR', 'message': str(exc)[:300], 'url': url}

    if '_body' not in raw:  # redirect problem: already an error payload
        return raw

    body: bytes = raw['_body']
    final_url: str = raw['_final_url']
    content_type: str = raw['_content_type']
    try:
        text_body = body.decode(raw['_encoding'], errors='replace')
    except LookupError:  # server announced a charset Python does not know
        text_body = body.decode('utf-8', errors='replace')

    from .origin import source_platform

    requested_platform = source_platform(url)
    final_platform = source_platform(final_url)
    is_html = 'html' in content_type or '<html' in text_body[:500].lower()
    meta_platforms = ('facebook', 'instagram')
    if (requested_platform in meta_platforms or final_platform in meta_platforms) and raw['_ok']:
        same_platform = requested_platform not in meta_platforms or final_platform == requested_platform
        graph = _extract_html(text_body).open_graph if is_html and same_platform else {}
        preview = graph.get('og:description', '')
        post_path = urlparse(final_url).path.lower()
        if (len(preview) < 16 or any(phrase in preview.casefold() for phrase in
            ('log in to', 'sign up to', 'join facebook', 'create an account', 'javascript to run', 'see photos and videos'))
                or any(post_path.startswith(p) for p in ('/login', '/accounts', '/checkpoint'))):
            return {'ok': False, 'error': 'SOURCE_TEXT_UNAVAILABLE', 'url': url,
                    'message': 'No readable public post text. Paste a caption or transcript you can share with this link.'}
        return {
            'ok': True, 'statusCode': raw['_status'], 'url': final_url, 'requestedUrl': url,
            'contentType': 'text/plain', 'title': (graph.get('og:title') or final_platform.title())[:300],
            'text': preview[:MAX_TEXT_CHARS], 'charCount': min(len(preview), MAX_TEXT_CHARS),
            'coverage': 'public preview', 'inputLimitReached': len(preview) > MAX_TEXT_CHARS, 'error': None,
        }
    title = ''
    plain = text_body
    if is_html:
        title, plain = html_to_text(text_body)
    elif len(plain) > MAX_TEXT_CHARS:
        plain = plain[:MAX_TEXT_CHARS] + '…'

    if not title:
        title = urlparse(final_url).path.rsplit('/', 1)[-1] or url

    return {
        'ok': raw['_ok'],
        'statusCode': raw['_status'],
        'url': final_url,
        'requestedUrl': url,
        'contentType': content_type,
        'title': title[:300],
        'text': plain,
        'charCount': len(plain),
        'inputLimitReached': (len(body) >= MAX_BYTES or len(plain) > MAX_TEXT_CHARS
                              or (len(text_body) > MAX_PARSE_CHARS and ('html' in content_type or '<html' in text_body[:500].lower()))),
        'error': None if raw['_ok'] else 'HTTP_STATUS',
        'message': None if raw['_ok'] else f"Upstream returned HTTP {raw['_status']}",
    }


async def fetch_text(url: str) -> dict[str, Any]:
    """Fetch raw text (feeds, XML) through the same hardened path: public hosts only, capped size."""
    blocked = await check_url_async(url)
    if blocked:
        return {'ok': False, 'error': blocked, 'message': 'Only public http/https URLs are supported.', 'url': url}
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECS):
            raw = await _fetch(url)
    except (TimeoutError, httpx.TimeoutException):
        return {'ok': False, 'error': 'TIMEOUT', 'message': f'Timed out after {TOTAL_TIMEOUT_SECS:.0f}s', 'url': url}
    except _UnsupportedEncoding as exc:
        return {'ok': False, 'error': 'UNSUPPORTED_ENCODING', 'message': f'Unsupported content-encoding {exc}', 'url': url}
    except zlib.error:
        return {'ok': False, 'error': 'BAD_ENCODING', 'message': 'Response body could not be decompressed.', 'url': url}
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return {'ok': False, 'error': 'HTTP_ERROR', 'message': str(exc)[:300], 'url': url}
    if '_body' not in raw:
        return raw
    try:
        text = raw['_body'].decode(raw['_encoding'], errors='replace')
    except LookupError:
        text = raw['_body'].decode('utf-8', errors='replace')
    if not raw['_ok']:
        return {'ok': False, 'error': 'HTTP_STATUS', 'message': f"Upstream returned HTTP {raw['_status']}", 'url': url}
    return {'ok': True, 'url': raw['_final_url'], 'text': text}


PASSAGE_CHARS = 480  # fits the search summary, so every word of a passage is searchable
MAX_PASSAGES = 16
MIN_PASSAGE_CHARS = 80
_SENTENCE_END = ('. ', '! ', '? ', '\n')


def split_passages(text: str, *, size: int = PASSAGE_CHARS, limit: int = MAX_PASSAGES) -> list[str]:
    """Cut page text into short passages at paragraph or sentence ends. Linear time, no regex."""
    out: list[str] = []
    buf = ''
    for para in (text or '').split('\n'):
        para = ' '.join(para.split())
        while para:
            room = size - len(buf) - (1 if buf else 0)
            if len(para) <= room:
                buf = f'{buf} {para}'.strip()
                para = ''
                continue
            cut = -1
            if room > 0:
                window = para[:room]
                cut = max(window.rfind(end) for end in _SENTENCE_END[:3])
                if cut < 0 and not buf:
                    cut = window.rfind(' ')
                    cut = cut if cut > 0 else room - 1
            if cut >= 0:
                buf = f'{buf} {para[: cut + 1].strip()}'.strip()
                para = para[cut + 1 :].strip()
            if buf:
                out.append(buf)
                buf = ''
                if len(out) >= limit:
                    return out
    if len(buf) >= MIN_PASSAGE_CHARS or (buf and not out):
        out.append(buf)
    return [p for p in out if len(p) >= MIN_PASSAGE_CHARS][:limit]
