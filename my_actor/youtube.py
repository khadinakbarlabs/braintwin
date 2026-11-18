"""YouTube video links: save what was SAID in the video, not the empty page shell.

Own fetching only (no third-party Actors, no API key): the video's public player data gives the
title, channel, description and caption tracks; the caption track gives the transcript.
YouTube sometimes refuses caption requests from cloud addresses. Then the title, channel and
description are saved on their own and the result says the transcript was not available.

Safety: requests go only to fixed YouTube hosts (the caption address is checked to be one),
every response is size-capped and the whole job has one deadline. The text is untrusted web
content like any other page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger('apify.braintwin.youtube')  # under the SDK's logger so it shows in the run log
PLAYER_URL = 'https://www.youtube.com/youtubei/v1/player?prettyPrint=false'
MAX_RESPONSE_BYTES = 3_000_000
MAX_TRANSCRIPT_CHARS = 50_000
MAX_DESCRIPTION_CHARS = 3_000
TOTAL_TIMEOUT_S = 14.0  # stays under the 15 s a tool call waits for a page
STEP_TIMEOUT_S = 5.0
_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
_YT_HOSTS = ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com')
# Public app clients whose caption addresses work without a browser session.
_CLIENTS: tuple[tuple[dict[str, Any], str, str], ...] = (
    (
        {'clientName': 'IOS', 'clientVersion': '20.10.4', 'deviceModel': 'iPhone16,2', 'hl': 'en'},
        'com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)',
        '5',
    ),
    (
        {'clientName': 'ANDROID', 'clientVersion': '20.10.38', 'androidSdkVersion': 30, 'hl': 'en'},
        'com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip',
        '3',
    ),
)


def video_id(url: str) -> str | None:
    """The 11-character id when `url` is a YouTube video (watch, youtu.be, shorts, live, embed)."""
    try:
        parsed = urlparse((url or '').strip())
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https'):
        return None
    host = (parsed.hostname or '').lower()
    candidate: str | None = None
    if host == 'youtu.be':
        candidate = parsed.path.strip('/').split('/')[0]
    elif host in _YT_HOSTS:
        parts = [p for p in parsed.path.split('/') if p]
        if parsed.path == '/watch':
            candidate = (parse_qs(parsed.query).get('v') or [''])[0]
        elif len(parts) >= 2 and parts[0] in ('shorts', 'live', 'embed', 'v'):
            candidate = parts[1]
    return candidate if candidate and _ID_RE.match(candidate) else None


def _is_youtube_host(url: str) -> bool:
    host = (urlparse(url).hostname or '').lower()
    return urlparse(url).scheme == 'https' and (host == 'youtube.com' or host.endswith('.youtube.com'))


def pick_track(tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer captions a person wrote, in English; then auto-made English; then whatever exists."""
    usable = [t for t in tracks if isinstance(t, dict) and isinstance(t.get('baseUrl'), str)]
    if not usable:
        return None

    def rank(track: dict[str, Any]) -> tuple[int, int]:
        english = str(track.get('languageCode') or '').lower().startswith('en')
        auto = track.get('kind') == 'asr'
        return (0 if english else 1, 1 if auto else 0)

    return sorted(usable, key=rank)[0]


def transcript_text(raw: str) -> str:
    """Plain text from YouTube's json3 caption format. Empty string when it is not that format."""
    try:
        data = json.loads(raw)
    except ValueError:
        return ''
    out: list[str] = []
    for event in data.get('events') or [] if isinstance(data, dict) else []:
        segs = event.get('segs') if isinstance(event, dict) else None
        if not isinstance(segs, list):
            continue
        line = ''.join(str(s.get('utf8') or '') for s in segs if isinstance(s, dict)).strip()
        if line:
            out.append(line)
    text = ' '.join(' '.join(out).split())
    return text[:MAX_TRANSCRIPT_CHARS]


async def _bounded(resp: httpx.Response) -> bytes:
    body = b''
    async for chunk in resp.aiter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError('response too large')
    return body


async def _player(client: httpx.AsyncClient, vid: str) -> tuple[dict[str, Any], str]:
    """First player answer that is playable; ({} , '') when YouTube refuses every client."""
    for context, agent, client_id in _CLIENTS:
        headers = {
            'User-Agent': agent,
            'X-Youtube-Client-Name': client_id,
            'X-Youtube-Client-Version': str(context['clientVersion']),
            'Content-Type': 'application/json',
        }
        try:
            async with client.stream('POST', PLAYER_URL, json={'context': {'client': context}, 'videoId': vid}, headers=headers) as resp:
                if resp.status_code != 200:
                    continue
                data = json.loads(await _bounded(resp))
        except (httpx.HTTPError, ValueError):
            continue
        if isinstance(data, dict) and (data.get('playabilityStatus') or {}).get('status') == 'OK':
            return data, agent
    return {}, ''


async def _transcript(client: httpx.AsyncClient, track: dict[str, Any], agent: str) -> str:
    base = str(track['baseUrl'])
    if not _is_youtube_host(base):
        return ''
    try:
        async with client.stream('GET', base + '&fmt=json3', headers={'User-Agent': agent}) as resp:
            if resp.status_code != 200:
                return ''
            return transcript_text((await _bounded(resp)).decode('utf-8', errors='replace'))
    except (httpx.HTTPError, ValueError):
        return ''


# Set by the hosted server: returns proxy addresses to try, in order, when YouTube refuses the
# server's own address. Empty off the Apify platform (a home address is normally not refused).
ProxyProvider = Callable[[], Awaitable[list[str]]]
proxy_provider: ProxyProvider | None = None
OEMBED_URL = 'https://www.youtube.com/oembed'


async def _read(client: httpx.AsyncClient, vid: str) -> dict[str, Any] | None:
    """Title, channel, description and transcript through one client; None when YouTube refuses it."""
    data, agent = await _player(client, vid)
    details = data.get('videoDetails') if isinstance(data.get('videoDetails'), dict) else {}
    title = str(details.get('title') or '').strip()
    if not title:
        return None
    tracks = ((data.get('captions') or {}).get('playerCaptionsTracklistRenderer') or {}).get('captionTracks') or []
    track = pick_track(tracks if isinstance(tracks, list) else [])
    spoken = await _transcript(client, track, agent) if track else ''
    return {
        'title': title,
        'channel': str(details.get('author') or '').strip(),
        'description': str(details.get('shortDescription') or '').strip()[:MAX_DESCRIPTION_CHARS],
        'spoken': spoken,
        'language': (track or {}).get('languageCode') if spoken else None,
    }


async def _oembed(client: httpx.AsyncClient, vid: str) -> dict[str, Any] | None:
    """Last resort: YouTube's public embed card gives at least the title and the channel."""
    try:
        resp = await client.get(OEMBED_URL, params={'url': f'https://www.youtube.com/watch?v={vid}', 'format': 'json'})
        if resp.status_code != 200 or len(resp.content) > 100_000:
            return None
        card = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    title = str(card.get('title') or '').strip() if isinstance(card, dict) else ''
    if not title:
        return None
    return {'title': title, 'channel': str(card.get('author_name') or '').strip(), 'description': '', 'spoken': '', 'language': None}


async def fetch_video(url: str, client: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """Ingest payload for a YouTube video, or None when nothing useful could be read
    (the caller then falls back to fetching the page like any other link)."""
    vid = video_id(url)
    if not vid:
        return None
    found: dict[str, Any] | None = None
    route = 'direct'
    trail: list[str] = []
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            if client is not None:
                found = await _read(client, vid)
            else:
                async with httpx.AsyncClient(timeout=STEP_TIMEOUT_S, follow_redirects=False) as direct:
                    found = await _read(direct, vid)
                    trail.append(f"direct:{'refused' if found is None else ('transcript' if found['spoken'] else 'no-captions')}")
                    if found is None or not found['spoken']:
                        for n, proxy in enumerate(await proxy_provider() if proxy_provider else []):
                            async with httpx.AsyncClient(timeout=STEP_TIMEOUT_S, follow_redirects=False, proxy=proxy) as via:
                                better = await _read(via, vid)
                            trail.append(f"proxy{n + 1}:{'refused' if better is None else ('transcript' if better['spoken'] else 'no-captions')}")
                            if better is not None and (found is None or better['spoken']):
                                found, route = better, f'proxy{n + 1}'
                            if found is not None and found['spoken']:
                                break
                    if found is None:
                        found, route = await _oembed(direct, vid), 'card'
    except (TimeoutError, httpx.HTTPError) as exc:
        logger.info('youtube read stopped early: %s', type(exc).__name__)
    logger.info('youtube %s: route=%s transcript=%s tried=%s', vid, route if found else 'none', bool(found and found['spoken']), ','.join(trail))
    if found is None:
        return None
    title, channel, description, spoken = found['title'], found['channel'], found['description'], found['spoken']
    parts = [f'YouTube video by {channel}.' if channel else 'YouTube video.', f'Title: {title}.']
    if spoken:
        parts.append(f'Transcript: {spoken}')
    if description:
        parts.append(f'Description: {description}')
    text = '\n'.join(parts)
    return {
        'ok': True,
        'statusCode': 200,
        'url': f'https://www.youtube.com/watch?v={vid}',
        'requestedUrl': url,
        'contentType': 'text/plain',
        'title': f'{title} ({channel} on YouTube)'[:300] if channel else title[:300],
        'text': text,
        'charCount': len(text),
        'kind': 'youtube',
        'hasTranscript': bool(spoken),
        'transcriptLanguage': found['language'],
        'error': None,
        'message': None,
    }
