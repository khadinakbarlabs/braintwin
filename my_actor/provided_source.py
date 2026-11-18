"""File text supplied with a source link without contacting the publisher or a model."""

from __future__ import annotations

from .brain import BrainStore, scope_tag_for
from .ingest import check_url_async
from .policy import contains_secret, contains_sensitive_id, normalize_text

MAX_PROVIDED_CHARS = 20_000


async def file_provided_source(
    brain: BrainStore, *, url: str, text: str, scope: str | None = None, title: str = '',
    tag: str = 'provided', dry_run: bool = False,
) -> dict:
    url = url.strip()
    text = text.strip()
    if not url or len(url) > 2000 or not 16 <= len(text) <= MAX_PROVIDED_CHARS:
        return {'ok': False, 'error': 'INVALID_INPUT', 'message': f'Add a public link and 16–{MAX_PROVIDED_CHARS} characters.'}
    normalized = normalize_text(text)
    if contains_secret(normalized) or contains_sensitive_id(normalized):
        return {'ok': False, 'error': 'SENSITIVE_TEXT', 'message': 'Remove credentials or sensitive identifiers first.'}
    if await check_url_async(url):
        return {'ok': False, 'error': 'BAD_URL', 'message': 'Only public web links can be filed.'}
    result = {'ok': True, 'saved': False, 'reviewRequired': True, 'coverage': 'provided text'}
    if dry_run:
        return {**result, 'dryRun': True, 'message': 'Would file this text as source evidence for review.'}
    project = scope_tag_for(scope)
    tags = [tag] + ([project] if project else [])
    name = title.strip()[:200] or url
    filed = await brain.source_refresh(
        url=url, title=name, tags=tags, auto=False,
        ingest={'ok': True, 'url': url, 'text': text, 'title': name, 'charCount': len(text),
                'coverage': 'provided text'},
    )
    if not filed.get('ok'):
        return {'ok': False, 'error': filed.get('error') or 'SOURCE_FAILED', 'message': 'Could not file the source.'}
    changed = bool(filed.get('filedNew'))
    return {**result, 'saved': changed, 'reviewRequired': changed, 'memoryId': filed.get('memoryId'),
            'message': 'Source added to review.' if changed else 'No new source text.'}
