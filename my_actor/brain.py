"""Second-brain lifecycle on a pluggable store (Apify KV+Dataset, Apify REST API, or SQLite)."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import time
import uuid
import zlib
from datetime import datetime, UTC
from typing import Any

from .constants import (
    AUDIT_DATASET_NAME,
    BACKEND_ACTOR,
    INGEST_JOB_PREFIX,
    KEY_AUTO_STATS,
    KEY_INBOX,
    KEY_INDEX_STAMP,
    KEY_MANIFEST,
    KEY_POLICY,
    KEY_PROFILE,
    KEY_SEARCH_INDEX,
    KEY_SOURCES,
    KEY_SYNC_STATE,
    KV_STORE_NAME,
    MAX_CITATION_VALUE_CHARS,
    MAX_CITATIONS,
    MAX_SOURCES,
    MAX_TAG_CHARS,
    MAX_TAGS,
    MAX_URL_CHARS,
    MEMORY_PREFIX,
    SEARCH_INDEX_VERSION,
    STATUSES,
    VERSION,
)
from .embeddings import (
    EMBED_BREAKER_S,
    MAX_BATCH,
    AsyncEmbedder,
    HttpEmbedder,
    VectorStore,
    cosines_async,
    embedding_config,
    fuse,
    memory_text,
    text_hash,
)
from .ingest import check_url_async
from .ingest_queue import IngestQueue, job_id_for
from .ranking import MAX_QUERY_CHARS, QUERY_STOPWORDS, SCORER, RankIndex, hybrid_rank, is_expired, stem_tokens
from .import_memories import normalize_plugin_export
from .policy import evaluate as evaluate_policy
from .policy import MAX_REPLACEMENTS_PER_DAY, MODE_OPEN, OPEN_CAP_FACTOR, load_policy, normalize_title_key
from .storage import open_storage
from .search import (
    bm25_rank,
    card_from_index_entry,
    citation_uris,
    index_entry_from_record,
    make_summary,
    matched_terms,
    memory_card,
    record_from_index_entry,
)


# True only inside the task that opened a batch. A plain "is a batch open?" flag is shared by
# every task, which let other tool calls slip into the buffer without the lock.
_in_batch: contextvars.ContextVar[bool] = contextvars.ContextVar('second_brain_in_batch', default=False)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _memory_key(memory_id: str) -> str:
    return f'{MEMORY_PREFIX}{memory_id}'


def _new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:12]}'


# Only the owner-run follow sync may use the reserved `followed` / `follow:*` tags and `follow:`
# dedupe keys. Agents cannot forge followed content or pre-empt a real item's dedupe key.
allow_reserved: contextvars.ContextVar[bool] = contextvars.ContextVar('second_brain_allow_reserved', default=False)


def _is_reserved_tag(tag: str) -> bool:
    low = tag.strip().lower()
    return low == 'followed' or low.startswith('follow:')


def _dedupe_key(title: str, content: str, explicit: str | None) -> str:
    if explicit and explicit.strip().lower().startswith('follow:') and not allow_reserved.get():
        explicit = None
    if explicit:
        return explicit.strip()[:200]
    raw = f'{title.strip().lower()}|{content.strip().lower()[:500]}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def _clamp_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.7


def _empty_by_status() -> dict[str, list[str]]:
    return {s: [] for s in STATUSES}


PASSAGE_MIN_PAGE_CHARS = 1_200  # shorter pages are fully covered by their own summary


NEAR_DUP_JACCARD = 0.8  # share of words two notes must have in common to count as the same note
NEAR_DUP_MIN_WORDS = 4
LONG_SOURCE_PASSAGES = 40  # a video transcript is long: make all of its stored text findable
MAX_HITS_PER_PAGE = 2  # one saved web page may fill at most this many search results


def _cap_per_page(ranked: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep the best hits but stop one long web page from crowding everything else out."""
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for row in ranked:
        entry = row.get('entry') or {}
        if entry.get('type') in ('source_snapshot', 'source_chunk'):
            uris = entry.get('sourceUris') or []
            page = str(uris[0]) if uris else ''
            if page:
                seen[page] = seen.get(page, 0) + 1
                if seen[page] > MAX_HITS_PER_PAGE:
                    continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _is_passage(entry: dict[str, Any]) -> bool:
    """A passage of a saved web page: it follows its page through the inbox, so it is not listed."""
    return entry.get('type') == 'source_chunk' and 'passage' in (entry.get('tags') or [])


def _clean_tags(tags: Any) -> list[str]:
    """Bound agent-supplied tags: at most MAX_TAGS strings of MAX_TAG_CHARS, de-duplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags if isinstance(tags, (list, tuple)) else []:
        text = str(tag).strip()[:MAX_TAG_CHARS]
        if text and _is_reserved_tag(text) and not allow_reserved.get():
            continue
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
        if len(out) >= MAX_TAGS:
            break
    return out


def _clean_citations(citations: Any) -> list[Any]:
    """Bound agent-supplied citations: MAX_CITATIONS entries, scalar values trimmed."""
    out: list[Any] = []
    for cite in citations if isinstance(citations, (list, tuple)) else []:
        if isinstance(cite, str) and cite.strip():
            out.append(cite.strip()[:MAX_CITATION_VALUE_CHARS])
        elif isinstance(cite, dict):
            slim = {
                str(k)[:80]: (v[:MAX_CITATION_VALUE_CHARS] if isinstance(v, str) else v)
                for k, v in cite.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
            if slim:
                out.append(slim)
        if len(out) >= MAX_CITATIONS:
            break
    return out


def scope_tag_for(scope: str | None) -> str | None:
    """Scopes (project / workspace / persona) are plain tags `scope:<name>`."""
    name = (scope or '').strip().lower()
    return f'scope:{name[:60]}' if name else None


INDEX_BUCKETS = 16  # the search index is stored in this many records; a save rewrites only the one it touched
INDEX_FORMAT = 'sharded'


def _bucket_of(memory_id: str) -> int:
    return zlib.crc32(memory_id.encode('utf-8')) % INDEX_BUCKETS


def _bucket_key(bucket: int) -> str:
    return f'{KEY_SEARCH_INDEX}.{bucket:02d}'


def _merge_manifest(theirs: dict[str, Any], start: dict[str, Any], ours: dict[str, Any]) -> dict[str, Any]:
    """Another process saved while we worked: keep its changes and lay ours (start -> ours) on top."""
    merged = _normalize_manifest(theirs)
    ids = list(merged['ids'])
    have = set(ids)
    for mid in ours.get('ids') or []:
        if mid not in have:
            ids.append(mid)
            have.add(mid)
    merged['ids'] = ids

    def status_of(man: dict[str, Any]) -> dict[str, str]:
        return {mid: st for st, mids in (man.get('byStatus') or {}).items() for mid in mids}

    before, after = status_of(start), status_of(ours)
    for mid, st in after.items():
        if before.get(mid) != st:
            _set_status_membership(merged, mid, st)
    start_dedupe = start.get('byDedupe') or {}
    for key, mid in (ours.get('byDedupe') or {}).items():
        if start_dedupe.get(key) != mid:
            merged['byDedupe'][key] = mid
    merged['indexVersion'] = max(int(merged.get('indexVersion') or 0), int(ours.get('indexVersion') or 0))
    return merged


def _merge_inbox(theirs: list[str], start: list[str], ours: list[str]) -> list[str]:
    added = [i for i in ours if i not in set(start)]
    removed = set(start) - set(ours)
    out = [i for i in theirs if i not in removed]
    return out + [i for i in added if i not in set(out)]


def _normalize_manifest(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    ids = list(data.get('ids') or [])
    by_dedupe = dict(data.get('byDedupe') or {})
    by_status = _empty_by_status()
    existing = data.get('byStatus') or {}
    if isinstance(existing, dict):
        for st in STATUSES:
            by_status[st] = [str(i) for i in (existing.get(st) or []) if i]
    return {
        'ids': ids,
        'byDedupe': by_dedupe,
        'byStatus': by_status,
        'indexVersion': int(data.get('indexVersion') or 0),
        'updatedAt': data.get('updatedAt'),
    }


def _set_status_membership(manifest: dict[str, Any], memory_id: str, status: str | None) -> None:
    by_status = manifest.setdefault('byStatus', _empty_by_status())
    for st in list(by_status.keys()):
        by_status[st] = [i for i in (by_status.get(st) or []) if i != memory_id]
    if status and status in by_status:
        bucket = list(by_status.get(status) or [])
        if memory_id not in bucket:
            bucket.append(memory_id)
        by_status[status] = bucket


class BrainStore:
    """Named KV + audit dataset backed personal memory store."""

    def __init__(
        self,
        kv: Any = None,
        audit: Any = None,
        *,
        backend: str | None = None,
        default_backend: str = BACKEND_ACTOR,
    ) -> None:
        """Pass `kv`/`audit` to inject storage (tests); otherwise `init()` opens `backend`."""
        self._kv = kv
        self._audit = audit
        self._backend_arg = backend
        self._default_backend = default_backend
        self.backend_name = 'injected' if kv is not None else ''
        self.queue: IngestQueue | None = None
        # Batch buffer: defer manifest/inbox/index KV sets until end_batch().
        self._batch: dict[str, Any] | None = None
        # Serialize meta readers/writers on warm Standby (one BrainStore singleton).
        self._meta_lock = asyncio.Lock()
        self._auto_lock = asyncio.Lock()  # serialises clash check + approve for automatic approvals
        # Serialize source-list read-modify-write (worker runs fetches concurrently).
        # Lock order is always sources → meta; never take sources while holding meta.
        self._sources_lock = asyncio.Lock()
        # Warm-server search cache: analysed memories + the stamp of the index they came from.
        self._rank = RankIndex()
        self._idx_entries: dict[str, Any] | None = None
        self._idx_stamp: str | None = None
        self._idx_sharded = False
        self._idx_version = 0
        # Optional meaning search (off unless the owner configures an embeddings API).
        self._embedder: AsyncEmbedder | None = None
        self._extractor: Any = None
        self.auto_open = False  # set by the server when its approval mode lets the policy approve
        self._vectors: VectorStore | None = None
        self._embed_down_until = 0.0  # circuit breaker: skip the provider for a while after a failure

    def use_embedder(self, embedder: AsyncEmbedder | None) -> None:
        """Turn meaning search on (or off with None). Needs the storage to be open."""
        self._embedder = embedder
        self._vectors = VectorStore(self._kv, embedder.model, getattr(embedder, 'dims', None)) if embedder is not None and self._kv is not None else None

    @property
    def kv(self) -> Any:
        return self._kv

    def meaning_status(self) -> dict[str, Any]:
        """What the dashboard may show about meaning search. Never includes the API key."""
        if self._embedder is None:
            return {'on': False}
        return {'on': True, 'model': getattr(self._embedder, 'model', ''), 'dims': getattr(self._embedder, 'dims', None)}

    def capture_status(self) -> dict[str, Any]:
        """What the dashboard may show about smart capture. Never includes the API key."""
        ex = getattr(self, '_extractor', None)
        return {'on': True, 'model': getattr(ex, 'model', '')} if ex is not None else {'on': False}

    @property
    def extractor(self) -> Any:
        return getattr(self, '_extractor', None)

    def use_extractor(self, extractor: Any) -> None:
        self._extractor = extractor

    async def set_smart_capture(self, record: dict[str, Any] | None) -> bool:
        """Save the user's own smart-capture settings (None turns it off). False = settings not valid."""
        from .extractor import HttpExtractor, extractor_config
        from .settings import KEY_EXTRACTOR, address_is_internal

        assert self._kv is not None
        if record is None:
            await self._kv.set_value(KEY_EXTRACTOR, None)
            self.use_extractor(None)
            return True
        cfg = extractor_config(record)
        if cfg is None or await address_is_internal(cfg.url):
            return False
        await self._kv.set_value(KEY_EXTRACTOR, {'url': cfg.url, 'model': cfg.model, 'key': cfg.key})
        self.use_extractor(HttpExtractor(cfg))
        return True

    async def set_meaning_search(self, record: dict[str, Any] | None) -> bool:
        """Save the user's own meaning-search settings (None turns it off). False = settings not valid."""
        from .settings import KEY_EMBEDDINGS as KEY_USER_EMBEDDINGS
        from .settings import address_is_internal, embeddings_from_record

        assert self._kv is not None
        if record is None:
            await self._kv.set_value(KEY_USER_EMBEDDINGS, None)
            self.use_embedder(None)
            return True
        cfg = embeddings_from_record(record)
        if cfg is None or await address_is_internal(cfg.url):
            return False
        await self._kv.set_value(
            KEY_USER_EMBEDDINGS, {'url': cfg.url, 'model': cfg.model, 'key': cfg.key, 'dims': cfg.dims}
        )
        self.use_embedder(HttpEmbedder(cfg))
        self._embed_down_until = 0.0
        return True

    async def init(self) -> None:
        if self._kv is None or self._audit is None:
            self._kv, self._audit, self.backend_name = await open_storage(
                self._backend_arg, default=self._default_backend
            )
        self.queue = IngestQueue(self._kv)
        if self._embedder is None:
            from .settings import load_embeddings  # local import: settings imports embeddings too

            cfg = await load_embeddings(self._kv)
            if cfg is not None:
                self.use_embedder(HttpEmbedder(cfg))
        if getattr(self, '_extractor', None) is None:
            from .extractor import HttpExtractor
            from .settings import load_extractor

            ex_cfg = await load_extractor(self._kv)
            if ex_cfg is not None:
                self.use_extractor(HttpExtractor(ex_cfg))
        # One round of parallel reads instead of five in a row (shortens the wake-up).
        have_sources, have_inbox, have_manifest, have_profile = await asyncio.gather(
            self._kv.get_value(KEY_SOURCES),
            self._kv.get_value(KEY_INBOX),
            self._kv.get_value(KEY_MANIFEST),
            self._kv.get_value(KEY_PROFILE),
        )
        if have_sources is None:
            await self._kv.set_value(KEY_SOURCES, {'sources': []})
        if have_inbox is None:
            await self._kv.set_value(KEY_INBOX, {'ids': []})
        if have_manifest is None:
            await self._kv.set_value(
                KEY_MANIFEST,
                {
                    'ids': [],
                    'byDedupe': {},
                    'byStatus': _empty_by_status(),
                    'indexVersion': SEARCH_INDEX_VERSION,
                    'updatedAt': _now(),
                },
            )
        if have_profile is None:
            await self._kv.set_value(
                KEY_PROFILE,
                {
                    'name': 'default',
                    'createdAt': _now(),
                    'updatedAt': _now(),
                    'notes': 'Personal second-brain profile (not official Apify MCP).',
                },
            )
        await self._ensure_search_index()
        index = await self._get_search_index()
        if not self._idx_sharded or not isinstance(await self._kv.get_value(KEY_INDEX_STAMP), dict):
            await self._persist_search_index(index)  # older store: move to the bucketed form and stamp it

    async def begin_batch(self) -> None:
        """Load meta into memory; flush once via end_batch (per-memory keys still written).

        The calling TASK owns the batch (ContextVar) and holds `_meta_lock` for the whole
        window, so every other task's reads and writes WAIT instead of slipping into the
        buffer unlocked. Nesting inside the owning task is a no-op. Do not spawn tasks
        inside a batch: they would inherit ownership.
        """
        if self._holds_batch():
            self._batch['depth'] = int(self._batch.get('depth') or 1) + 1  # type: ignore[index]
            return
        await self._meta_lock.acquire()
        try:
            # Load from KV directly (batch not set yet).
            assert self._kv is not None
            manifest = _normalize_manifest(await self._kv.get_value(KEY_MANIFEST))
            inbox_data = await self._kv.get_value(KEY_INBOX) or {'ids': []}
            index = await self._get_search_index()  # cached unless another writer moved the stamp
            index_base, index_base_stamp = self._idx_entries, self._idx_stamp
            self._batch = {
                'manifest': manifest,
                'inbox': list(inbox_data.get('ids') or []),
                'index': index,
                'index_base': index_base,
                'index_base_stamp': index_base_stamp,
                'manifest_start': _normalize_manifest(manifest),
                'manifest_stamp': manifest.get('updatedAt'),
                'inbox_start': list(inbox_data.get('ids') or []),
                'inbox_stamp': inbox_data.get('updatedAt'),
                'audit': [],
                'dirty_manifest': False,
                'dirty_inbox': False,
                'dirty_index': False,
                'depth': 1,
            }
            self._batch['token'] = _in_batch.set(True)
        except Exception:
            self._batch = None
            self._meta_lock.release()
            raise

    def _holds_batch(self) -> bool:
        """True only for the task that opened the current batch."""
        return self._batch is not None and _in_batch.get()

    async def end_batch(self) -> None:
        if not self._holds_batch():
            return
        depth = int(self._batch.get('depth') or 1) - 1  # type: ignore[union-attr]
        if depth > 0:
            self._batch['depth'] = depth  # type: ignore[index]
            return
        token = self._batch.get('token')  # type: ignore[union-attr]
        try:
            # Persist index before manifest so readers that see new ids can rebuild if needed.
            batch = self._batch
            if batch.get('dirty_index'):
                await self._persist_search_index(
                    batch['index'], base=batch.get('index_base'), base_stamp=batch.get('index_base_stamp')
                )
            if batch.get('dirty_manifest'):
                manifest = batch['manifest']
                stored = await self._kv.get_value(KEY_MANIFEST)  # type: ignore[union-attr]
                if isinstance(stored, dict) and stored.get('updatedAt') != batch.get('manifest_stamp'):
                    manifest = _merge_manifest(stored, batch['manifest_start'], manifest)  # another writer got in
                await self._persist_manifest(manifest)
            if batch.get('dirty_inbox'):
                inbox = batch['inbox']
                stored = await self._kv.get_value(KEY_INBOX)  # type: ignore[union-attr]
                if isinstance(stored, dict) and stored.get('updatedAt') != batch.get('inbox_stamp'):
                    inbox = _merge_inbox(list(stored.get('ids') or []), batch['inbox_start'], inbox)
                await self._persist_inbox(inbox)
            if batch.get('audit'):  # history rows do not depend on each other
                await asyncio.gather(*(self._persist_audit(**event) for event in batch['audit']))
        finally:
            self._batch = None
            if token is not None:
                try:
                    _in_batch.reset(token)
                except ValueError:  # reset from a different context: just clear the flag
                    _in_batch.set(False)
            if self._meta_lock.locked():
                self._meta_lock.release()

    async def audit(
        self,
        event_type: str,
        *,
        memory_id: str | None = None,
        source_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        payload = {
            'event_type': event_type,
            'memory_id': memory_id,
            'source_id': source_id,
            'detail': detail,
        }
        if self._batch is not None:
            self._batch['audit'].append(payload)
            return
        await self._persist_audit(**payload)

    async def _persist_audit(
        self,
        *,
        event_type: str,
        memory_id: str | None = None,
        source_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        assert self._audit is not None
        await self._audit.push_data(
            {
                'eventType': event_type,
                'memoryId': memory_id,
                'sourceId': source_id,
                'detail': (detail or '')[:1000],
                'at': _now(),
            }
        )

    async def _get_sources(self) -> list[dict[str, Any]]:
        assert self._kv is not None
        data = await self._kv.get_value(KEY_SOURCES) or {'sources': []}
        return list(data.get('sources') or [])

    async def _set_sources(self, sources: list[dict[str, Any]]) -> None:
        assert self._kv is not None
        await self._kv.set_value(KEY_SOURCES, {'sources': sources, 'updatedAt': _now()})

    async def _get_inbox(self) -> list[str]:
        if self._batch is not None:
            return list(self._batch['inbox'])
        assert self._kv is not None
        data = await self._kv.get_value(KEY_INBOX) or {'ids': []}
        return list(data.get('ids') or [])

    async def _set_inbox(self, ids: list[str]) -> None:
        seen: set[str] = set()
        unique: list[str] = []
        for mid in ids:
            if mid not in seen:
                seen.add(mid)
                unique.append(mid)
        if self._batch is not None:
            self._batch['inbox'] = unique
            self._batch['dirty_inbox'] = True
            return
        await self._persist_inbox(unique)

    async def _persist_inbox(self, ids: list[str]) -> None:
        assert self._kv is not None
        await self._kv.set_value(KEY_INBOX, {'ids': ids, 'updatedAt': _now()})

    async def _get_manifest(self) -> dict[str, Any]:
        if self._batch is not None:
            return self._batch['manifest']
        assert self._kv is not None
        return _normalize_manifest(await self._kv.get_value(KEY_MANIFEST))

    async def _set_manifest(self, manifest: dict[str, Any]) -> None:
        normalized = _normalize_manifest(manifest)
        if self._batch is not None:
            self._batch['manifest'] = normalized
            self._batch['dirty_manifest'] = True
            return
        await self._persist_manifest(normalized)

    async def _persist_manifest(self, manifest: dict[str, Any]) -> None:
        assert self._kv is not None
        payload = _normalize_manifest(manifest)
        payload['indexVersion'] = SEARCH_INDEX_VERSION
        payload['updatedAt'] = _now()
        await self._kv.set_value(KEY_MANIFEST, payload)

    async def _stamp_token(self) -> str | None:
        assert self._kv is not None
        stamp = await self._kv.get_value(KEY_INDEX_STAMP)
        return stamp.get('stamp') if isinstance(stamp, dict) else None

    async def _read_index_from_store(self) -> tuple[dict[str, Any], int, bool]:
        """(entries, version, is_sharded) straight from storage. Understands the old one-record form."""
        assert self._kv is not None
        meta = await self._kv.get_value(KEY_SEARCH_INDEX)
        if not isinstance(meta, dict):
            return {}, 0, False
        version = int(meta.get('version') or 0)
        if meta.get('format') != INDEX_FORMAT:
            entries = meta.get('entries')
            return (dict(entries) if isinstance(entries, dict) else {}), version, False
        parts = await asyncio.gather(*(self._kv.get_value(_bucket_key(i)) for i in range(INDEX_BUCKETS)))
        merged: dict[str, Any] = {}
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get('entries'), dict):
                merged.update(part['entries'])
        return merged, version, True

    async def _get_search_index(self) -> dict[str, Any]:
        if self._batch is not None:
            return self._batch['index']
        token = await self._stamp_token()
        if not (token and token == self._idx_stamp and self._idx_entries is not None):
            entries, version, sharded = await self._read_index_from_store()
            self._idx_sharded = sharded
            self._idx_version = version
            self._adopt_index(entries, token)
        return {
            'version': self._idx_version or SEARCH_INDEX_VERSION,
            'entries': dict(self._idx_entries or {}),
            'updatedAt': _now(),
        }

    async def _set_search_index(self, index: dict[str, Any]) -> None:
        if self._batch is not None:
            self._batch['index'] = index
            self._batch['dirty_index'] = True
            return
        await self._persist_search_index(index)

    async def _persist_search_index(
        self, index: dict[str, Any], *, base: dict[str, Any] | None = None, base_stamp: str | None = None
    ) -> None:
        """Write the index. Only the buckets that changed since `base` are rewritten.

        `base` is the index as this process last read it (default: its cached copy). If another
        process has written since then (the stamp moved), each bucket we are about to write is
        re-read first and only OUR changes are laid on top, so neither side loses a save.
        """
        assert self._kv is not None
        entries = dict(index.get('entries') or {})
        if base is None:
            base, base_stamp = self._idx_entries, self._idx_stamp
        full = base is None or not self._idx_sharded or base_stamp is None
        foreign = False
        if full:
            dirty = set(range(INDEX_BUCKETS))
            changed: set[str] = set()
        else:
            changed = {mid for mid, entry in entries.items() if base.get(mid) != entry}  # type: ignore[union-attr]
            changed |= {mid for mid in base if mid not in entries}  # type: ignore[union-attr]
            dirty = {_bucket_of(mid) for mid in changed}
            foreign = bool(dirty) and (await self._stamp_token()) != base_stamp
        buckets: dict[int, dict[str, Any]] = {b: {} for b in dirty}
        for mid, entry in entries.items():
            b = _bucket_of(mid)
            if b in buckets:
                buckets[b][mid] = entry
        if foreign:
            for b in dirty:
                stored = await self._kv.get_value(_bucket_key(b))
                theirs = dict(stored.get('entries') or {}) if isinstance(stored, dict) else {}
                for mid in changed:
                    if _bucket_of(mid) != b:
                        continue
                    if mid in entries:
                        theirs[mid] = entries[mid]
                    else:
                        theirs.pop(mid, None)
                buckets[b] = theirs
        if buckets:
            await asyncio.gather(
                *(self._kv.set_value(_bucket_key(b), {'entries': part, 'updatedAt': _now()}) for b, part in buckets.items())
            )
        if full:
            await self._kv.set_value(
                KEY_SEARCH_INDEX,
                {'version': SEARCH_INDEX_VERSION, 'format': INDEX_FORMAT, 'buckets': INDEX_BUCKETS, 'updatedAt': _now()},
            )
            self._idx_sharded = True
            self._idx_version = SEARCH_INDEX_VERSION
        if not buckets and not full:
            return  # nothing changed: keep the stamp, so other servers do not reload for nothing
        # Stamp AFTER the index: a reader that sees the new stamp is guaranteed the new index.
        stamp = uuid.uuid4().hex
        await self._kv.set_value(KEY_INDEX_STAMP, {'stamp': stamp, 'count': len(entries), 'updatedAt': _now()})
        if foreign:
            # Our in-memory copy lacks the other writer's changes in buckets we did not touch.
            self._idx_entries, self._idx_stamp = None, None
            self._rank.sync(entries.values())
        else:
            self._adopt_index(entries, stamp)

    def _adopt_index(self, entries: dict[str, Any], stamp: str | None) -> None:
        """Remember an index (and analyse its memories once) so searches can skip the download."""
        self._rank.sync(entries.values())
        self._idx_entries = entries
        self._idx_stamp = stamp

    async def _current_index_entries(self) -> dict[str, Any]:
        """Search-index entries, re-downloaded only when another writer changed them.

        Fast path = one tiny read (the stamp) and no lock. A mismatch reloads under the lock and,
        once per reload, checks the index against the manifest (rebuilds if they drifted apart).
        """
        assert self._kv is not None
        stamp = await self._kv.get_value(KEY_INDEX_STAMP)
        token = stamp.get('stamp') if isinstance(stamp, dict) else None
        if token and token == self._idx_stamp and self._idx_entries is not None:
            return self._idx_entries
        async with self._meta_lock:
            stamp = await self._kv.get_value(KEY_INDEX_STAMP)
            token = stamp.get('stamp') if isinstance(stamp, dict) else None
            if token and token == self._idx_stamp and self._idx_entries is not None:
                return self._idx_entries
            index = await self._get_search_index()  # reloads and adopts when the stamp moved
            entries = self._idx_entries if self._idx_entries is not None else dict(index.get('entries') or {})
            manifest = await self._get_manifest()
            if manifest.get('ids') and len(entries) != len(manifest['ids']):
                index = await self._rebuild_search_index()  # persists and adopts the fresh index
                return dict(index.get('entries') or {})
            return entries

    async def _index_entries_fast(self) -> dict[str, Any]:
        """Index entries for read paths. MUST be called before taking `_meta_lock` (it may take it itself)."""
        if self._batch is not None:
            return dict(self._batch['index'].get('entries') or {})
        return await self._current_index_entries()

    async def _ensure_search_index(self) -> None:
        """Migrate older stores: build thin index + byStatus once if missing/stale."""
        assert self._kv is not None
        manifest = _normalize_manifest(await self._kv.get_value(KEY_MANIFEST))
        index = await self._kv.get_value(KEY_SEARCH_INDEX)
        has_valid_index = (
            isinstance(index, dict)
            and int(index.get('version') or 0) >= SEARCH_INDEX_VERSION
            and (index.get('format') == INDEX_FORMAT or isinstance(index.get('entries'), dict))
        )
        status_count = sum(len(manifest['byStatus'].get(s) or []) for s in STATUSES)
        needs_rebuild = (
            not has_valid_index
            or int(manifest.get('indexVersion') or 0) < SEARCH_INDEX_VERSION
            or (bool(manifest.get('ids')) and status_count == 0)
        )
        if needs_rebuild:
            await self._rebuild_search_index(persist_manifest=True)

    async def _rebuild_search_index(self, *, persist_manifest: bool = True) -> dict[str, Any]:
        manifest = await self._get_manifest()
        entries: dict[str, Any] = {}
        by_status = _empty_by_status()
        live_ids: list[str] = []
        by_dedupe = dict(manifest.get('byDedupe') or {})
        for mid in list(manifest.get('ids') or []):
            rec = await self.get_memory(mid)
            if not rec:
                # Drop orphan id from dedupe map
                by_dedupe = {k: v for k, v in by_dedupe.items() if v != mid}
                continue
            if not rec.get('summary'):
                rec['summary'] = make_summary(str(rec.get('content') or ''))
                assert self._kv is not None
                await self._kv.set_value(_memory_key(mid), rec)
            live_ids.append(mid)
            entries[mid] = index_entry_from_record(rec)
            st = rec.get('status') if rec.get('status') in by_status else 'working'
            by_status[st].append(mid)
        index = {'version': SEARCH_INDEX_VERSION, 'entries': entries, 'updatedAt': _now()}
        manifest['ids'] = live_ids
        manifest['byDedupe'] = by_dedupe
        manifest['byStatus'] = by_status
        manifest['indexVersion'] = SEARCH_INDEX_VERSION
        if self._batch is not None:
            # Never bypass the batch buffer mid-import.
            self._batch['index'] = index
            self._batch['manifest'] = _normalize_manifest(manifest)
            self._batch['dirty_index'] = True
            self._batch['dirty_manifest'] = True
            return index
        await self._persist_search_index(index)
        if persist_manifest:
            await self._persist_manifest(manifest)
        return index

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        assert self._kv is not None
        return await self._kv.get_value(_memory_key(memory_id))

    async def _put_memory(self, record: dict[str, Any]) -> None:
        assert self._kv is not None
        if not record.get('summary'):
            record['summary'] = make_summary(str(record.get('content') or ''))
        await self._kv.set_value(_memory_key(record['id']), record)

        manifest = await self._get_manifest()
        ids = list(manifest.get('ids') or [])
        if record['id'] not in ids:
            ids.append(record['id'])
        by_dedupe = dict(manifest.get('byDedupe') or {})
        if record.get('dedupeKey'):
            by_dedupe[record['dedupeKey']] = record['id']
        _set_status_membership(manifest, record['id'], record.get('status'))
        manifest['ids'] = ids
        manifest['byDedupe'] = by_dedupe
        manifest['indexVersion'] = SEARCH_INDEX_VERSION
        await self._set_manifest(manifest)

        index = await self._get_search_index()
        entries = dict(index.get('entries') or {})
        entries[record['id']] = index_entry_from_record(record)
        index['entries'] = entries
        index['version'] = SEARCH_INDEX_VERSION
        await self._set_search_index(index)

    async def _auto_decision(self, title: str, content: str, tags: list[str], provenance: dict[str, Any]) -> tuple[bool, list[str], str]:
        """Should this fetched page be kept at once? Decided BEFORE writing, so it is written only once."""
        if self._holds_batch() or not await self.open_mode():
            return False, ['not_automatic'], ''
        policy = await self.get_policy()
        decision = evaluate_policy(
            title=title, content=content, memory_type='source_snapshot', confidence=0.6, tags=tags,
            provenance=provenance, policy=policy, scope_tag=[t for t in tags if str(t).lower().startswith('scope:')],
        )  # fmt: skip
        if not decision.ok:
            return False, list(decision.reasons), ''
        if not await self._auto_budget_take(1, int(policy['daily_cap']) * OPEN_CAP_FACTOR):
            return False, ['daily_cap_reached'], ''
        return True, [], decision.rule

    async def _apply_ingest(
        self,
        source: dict[str, Any],
        ingest: dict[str, Any],
        tags: list[str] | None,
        *,
        auto: bool = False,
    ) -> None:
        """Record a successful fetch on `source` and file the page (with its passages) in one write pass.

        With `auto` the owner's rules are applied first: a page they allow is saved as kept straight
        away instead of being saved as waiting and approved in a second pass.
        """
        source['lastIngest'] = {
            'at': _now(),
            'statusCode': ingest.get('statusCode'),
            'charCount': ingest.get('charCount'),
            'title': ingest.get('title'),
        }
        source['updatedAt'] = _now()
        content = (ingest.get('text') or '')[:20_000]
        if not content.strip():
            source['filedNew'] = False  # a page with no readable text files nothing
            return
        # A source that changes over time (a Notion page) files each version under its own key.
        version = f"@{ingest['version']}" if ingest.get('version') else ''
        source['version'] = version
        all_tags = list(set((tags or source.get('tags') or []) + ['source', 'ingest']))
        provenance: dict[str, Any] = {'sourceUrl': source['url'], 'untrusted': True}
        keep, reasons, rule = (await self._auto_decision(source['title'], content, all_tags, provenance)) if auto else (False, ['not_automatic'], '')
        if keep:
            provenance['autoApproved'] = {'rule': rule, 'at': _now()}
        await self.begin_batch()
        try:
            mem = await self.memory_save(
                title=source['title'],
                content=content,
                memory_type='source_snapshot',
                tags=all_tags,
                citations=[{'url': source['url'], 'sourceId': source['id']}],
                confidence=0.6,
                approve=keep,
                dedupe_key=f"source:{source['url']}{version}",
                provenance=provenance,
            )
            source['proposedMemoryId'] = mem.get('memory', {}).get('id')
            source['filedNew'] = bool(mem.get('ok', True)) and not mem.get('duplicate')  # False: nothing new was written
            if mem.get('decided'):
                source['alreadyDecided'] = True  # the owner rejected or forgot this page before
            limit = LONG_SOURCE_PASSAGES if ingest.get('kind') == 'youtube' else None
            await self._file_passages(source, mem, ingest.get('text') or '', tags, approve=keep, limit=limit)
            await self.audit(
                'ingest',
                source_id=source['id'],
                memory_id=source.get('proposedMemoryId'),
                detail=source['url'],
            )
        finally:
            await self.end_batch()
        saved_now = keep and bool(source.get('filedNew'))
        if keep and not saved_now:
            await self._auto_budget(-1)  # nothing new was written: hand the daily slot back
        if saved_now:
            await self._auto_record(str(source.get('proposedMemoryId')), str(source['title']), rule)
        source['autoSaved'] = saved_now
        source['autoReasons'] = [] if saved_now else reasons

    async def _file_passages(
        self, source: dict[str, Any], parent: dict[str, Any], text: str, tags: list[str] | None, *, approve: bool = False, limit: int | None = None
    ) -> None:
        """Also file a long page as short passages, so search can find any part of it.

        Passages follow their page: approving, rejecting or forgetting the page does the same to
        them, and the inbox shows only the page.
        """
        from .ingest import split_passages

        parent_rec = parent.get('memory') or {}
        parent_id = parent_rec.get('id')
        if not parent_id or parent.get('duplicate') or len(text) <= PASSAGE_MIN_PAGE_CHARS:
            return
        passages = split_passages(text, limit=limit) if limit else split_passages(text)
        if len(passages) < 2:
            return
        await self.begin_batch()  # one write of the index and inbox instead of one per passage
        try:
            gate = asyncio.Semaphore(8)  # the passage records are separate files: write several at a time

            async def one(i: int, passage: str) -> str | None:
                async with gate:
                    res = await self.memory_save(
                        title=f"{source['title'][:200]} (part {i} of {len(passages)})",
                        content=passage,
                        memory_type='source_chunk',
                        tags=list(set((tags or source.get('tags') or []) + ['source', 'passage'])),
                        citations=[{'url': source['url'], 'sourceId': source['id']}],
                        confidence=0.6,
                        approve=approve,
                        dedupe_key=f"source:{source['url']}{source.get('version') or ''}#p{i}",
                        provenance={'sourceUrl': source['url'], 'untrusted': True, 'parentId': parent_id},
                    )
                cid = (res.get('memory') or {}).get('id')
                return cid if cid and not res.get('duplicate') else None

            done = await asyncio.gather(*(one(i, passage) for i, passage in enumerate(passages, 1)))
            children = [cid for cid in done if cid]
            if children:
                rec = await self.get_memory(parent_id)
                if rec:
                    rec['children'] = children
                    await self._put_memory(rec)
                source['passageCount'] = len(children)
        finally:
            await self.end_batch()

    async def _cascade_children(self, rec: dict[str, Any], status: str, now: str) -> int:
        """Give a page's passages the same fate as the page. Caller holds the meta lock."""
        done = 0
        gone: list[str] = []
        for cid in rec.get('children') or []:
            child = await self.get_memory(str(cid))
            if not child or (child.get('provenance') or {}).get('parentId') != rec.get('id'):
                continue
            current = child.get('status')
            if status == 'decayed':
                if current == 'decayed':
                    continue
                child['decayedAt'] = now
            elif current not in ('proposed', 'working'):
                continue
            child['status'] = status
            child['updatedAt'] = now
            if status == 'approved':
                child['approvedAt'] = now
            await self._put_memory(child)
            gone.append(str(cid))
            done += 1
        if gone:
            await self._set_inbox([i for i in await self._get_inbox() if i not in gone])
        return done

    async def source_add(
        self,
        *,
        url: str,
        title: str | None = None,
        tags: list[str] | None = None,
        ingest: dict[str, Any] | None = None,
        auto: bool = False,
    ) -> dict[str, Any]:
        if len(url or '') > MAX_URL_CHARS:
            return {'ok': False, 'error': 'URL_TOO_LONG', 'message': f'URL longer than {MAX_URL_CHARS} characters.'}
        tags = _clean_tags(tags)
        async with self._sources_lock:
            sources = await self._get_sources()
            for existing in sources:
                if existing.get('url') == url:
                    if existing.get('lastIngest') is None and ingest and ingest.get('ok'):
                        # Registered earlier but never fetched (the page failed then): take it now.
                        await self._apply_ingest(existing, ingest, tags or None, auto=auto)
                        await self._set_sources(sources)
                        return {'ok': True, 'duplicate': False, 'source': existing, 'ingest': ingest}
                    return {'ok': True, 'duplicate': True, 'source': existing}
            if len(sources) >= MAX_SOURCES:
                return {
                    'ok': False,
                    'error': 'TOO_MANY_SOURCES',
                    'message': f'Source list is full ({MAX_SOURCES}). Remove some with brain_health(remove_source_id=...).',
                }

            source_id = _new_id('src')
            source = {
                'id': source_id,
                'url': url,
                'title': (title or (ingest or {}).get('title') or url)[:500],
                'tags': tags,
                'createdAt': _now(),
                'updatedAt': _now(),
                'lastIngest': None,
            }
            if ingest and ingest.get('ok'):
                await self._apply_ingest(source, ingest, tags, auto=auto)

            sources.append(source)
            await self._set_sources(sources)
            if not (ingest and ingest.get('ok')):
                await self.audit('ingest', source_id=source_id, detail=f'registered:{url}')
            return {'ok': True, 'duplicate': False, 'source': source, 'ingest': ingest}

    async def source_refresh(self, *, url: str, title: str, tags: list[str], ingest: dict[str, Any]) -> dict[str, Any]:
        """Add a source, or replace its stored copy with a newer version (connectors use this).

        The new version is filed first (and approved if the owner chose automatic saving); only
        then is the previous copy retired, passages included.
        """
        async with self._sources_lock:
            sources = await self._get_sources()
            source = next((s for s in sources if s.get('url') == url), None)
            old_id = source.get('proposedMemoryId') if source else None
            if source is None:
                if len(sources) >= MAX_SOURCES:
                    return {'ok': False, 'error': 'TOO_MANY_SOURCES', 'filedNew': False}
                source = {'id': _new_id('src'), 'url': url, 'title': title[:500], 'tags': _clean_tags(tags), 'createdAt': _now(), 'updatedAt': _now(), 'lastIngest': None}
                sources.append(source)
            source['title'] = title[:500]
            await self._apply_ingest(source, ingest, tags, auto=True)
            await self._set_sources(sources)
        new_id = source.get('proposedMemoryId')
        filed = bool(source.get('filedNew'))
        if filed:
            if old_id and old_id != new_id:
                await self.memory_forget(str(old_id), reason='replaced by a newer version of the page')
        return {'ok': True, 'filedNew': filed, 'source': source, 'memoryId': new_id}

    async def source_apply_ingest(self, source_id: str, ingest: dict[str, Any]) -> dict[str, Any]:
        """Attach a fetched page to an already-registered source (used by the ingest worker)."""
        async with self._sources_lock:
            sources = await self._get_sources()
            source = next((s for s in sources if s.get('id') == source_id), None)
            if source is None:
                return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No source with id {source_id}'}
            await self._apply_ingest(source, ingest, None)
            await self._set_sources(sources)
            return {'ok': True, 'source': source}

    async def ingest_enqueue(
        self,
        url: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register a source and queue its fetch for the worker (no network I/O here)."""
        blocked = await check_url_async(url)  # DNS runs in a thread, never on the event loop
        if blocked:
            return {
                'ok': False,
                'error': blocked,
                'message': 'Only public http/https URLs are supported for source ingest.',
                'url': url,
            }
        assert self.queue is not None
        # Refuse BEFORE registering a source, so a full queue cannot grow the source list.
        if await self.queue.is_full() and not await self.queue.get(job_id_for('url', url)):
            return {'ok': False, 'error': 'QUEUE_FULL', 'message': 'Ingest queue is full. Run the worker, then retry.'}
        added = await self.source_add(url=url, title=title, tags=tags, ingest=None)
        if not added.get('ok'):
            return added
        source = added['source']
        if source.get('lastIngest'):
            return {
                'ok': True,
                'queued': False,
                'duplicate': True,
                'source': source,
                'message': 'Source already ingested; nothing queued.',
            }
        job = await self.queue.enqueue_url(url, source_id=source['id'], title=title, tags=tags)
        if job.get('error'):
            return {'ok': False, 'error': job['error'], 'message': job['message'], 'source': source}
        if job.get('queued'):
            await self.audit('ingest_queued', source_id=source['id'], detail=url)
        return {
            'ok': True,
            'queued': bool(job.get('queued')),
            'duplicate': bool(job.get('duplicate')),
            'job': job.get('job'),
            'source': source,
            'message': (
                'Queued for the ingest worker; the page will appear in the inbox once fetched.'
                if job.get('queued')
                else 'A fetch for this URL is already queued.'
            ),
        }

    async def find_near_duplicate(self, content: str) -> dict[str, Any] | None:
        """An approved or waiting memory that says almost the same thing in almost the same words.

        Used by capture so "I prefer tea" said again as "I really prefer tea." is not filed twice.
        Word-set overlap (Jaccard) on stemmed words; short texts are never matched this way.
        """
        words = set(stem_tokens(content)) - QUERY_STOPWORDS
        if len(words) < NEAR_DUP_MIN_WORDS:
            return None
        found = await self.memory_search(content[:MAX_QUERY_CHARS], statuses=['approved', 'proposed'], limit=3)
        for row in found.get('results') or []:
            card = row.get('memory') or {}
            other = set(stem_tokens(str(card.get('summary') or ''))) - QUERY_STOPWORDS
            if not other:
                continue
            if len(words & other) / len(words | other) >= NEAR_DUP_JACCARD:
                return card
        return None

    async def find_replaced(self, content: str) -> dict[str, Any] | None:
        """An APPROVED memory that this new statement updates (same subject, new value), if any."""
        from .capture import replaces

        found = await self.memory_search(content[:MAX_QUERY_CHARS], limit=5)
        for row in found.get('results') or []:
            card = row.get('memory') or {}
            summary = str(card.get('summary') or '')
            if card.get('type') in ('preference', 'decision', 'fact', 'note') and len(summary) <= 320 and replaces(content, summary):
                return card
        return None

    async def _capture_checks(self, content: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(same note already stored, approved note this one replaces) from ONE search."""
        from .capture import replaces

        words = set(stem_tokens(content)) - QUERY_STOPWORDS
        # Word overlap is what both checks need, so skip the (slower, paid) meaning search here.
        found = await self.memory_search(content[:MAX_QUERY_CHARS], statuses=['approved', 'proposed'], limit=5, use_meaning=False)
        replaced: dict[str, Any] | None = None
        for row in found.get('results') or []:
            card = row.get('memory') or {}
            summary = str(card.get('summary') or '')
            other = set(stem_tokens(summary)) - QUERY_STOPWORDS
            if len(words) >= NEAR_DUP_MIN_WORDS and other and len(words & other) / len(words | other) >= NEAR_DUP_JACCARD:
                return card, None
            if (
                replaced is None
                and card.get('status') == 'approved'
                and card.get('type') in ('preference', 'decision', 'fact', 'note')
                and len(summary) <= 320
                and replaces(content, summary)
            ):
                replaced = card
        return None, replaced

    async def _auto_budget_take(self, wanted: int, cap: int) -> int:
        """Take up to `wanted` automatic approvals for today in one write. Returns how many were granted."""
        assert self._kv is not None
        if wanted <= 0:
            return 0
        async with self._meta_lock:
            stats = await self._kv.get_value(KEY_AUTO_STATS)
            stats = dict(stats) if isinstance(stats, dict) else {}
            today = _now()[:10]
            if stats.get('date') != today:
                stats['date'], stats['count'] = today, 0
            count = int(stats.get('count') or 0)
            granted = max(0, min(wanted, cap - count))
            if granted:
                stats['count'] = count + granted
                await self._kv.set_value(KEY_AUTO_STATS, stats)
            return granted

    async def _auto_record_many(self, rows: list[tuple[str, str, str]]) -> None:
        assert self._kv is not None
        if not rows:
            return
        async with self._meta_lock:
            stats = await self._kv.get_value(KEY_AUTO_STATS)
            stats = dict(stats) if isinstance(stats, dict) else {}
            recent = list(stats.get('recent') or [])
            now = _now()
            recent.extend({'id': mid, 'title': title[:120], 'rule': rule, 'at': now} for mid, title, rule in rows)
            stats['recent'] = recent[-20:]
            await self._kv.set_value(KEY_AUTO_STATS, stats)

    async def save_captured(
        self, candidates: list[dict[str, Any]], *, tags: list[str], agent: str | None, auto: bool
    ) -> dict[str, Any]:
        """Save facts found in what the user said, in ONE pass over storage.

        The slow way (save as proposed, then approve, one note at a time) rewrote the shared
        records twice per note. Here the checks run side by side, the owner's rules are applied
        before anything is written, and every note of the call is written in a single batch.
        Same outcome as before: duplicates skipped, replaced facts retired, held items wait.
        """
        checks = await asyncio.gather(*(self._capture_checks(c['content']) for c in candidates))
        policy = await self.get_policy()
        open_mode = policy.get('mode') == MODE_OPEN
        entries = await self._current_index_entries() if (auto and not open_mode) else {}
        plan: list[dict[str, Any]] = []
        already = 0
        for cand, (same, old) in zip(candidates, checks, strict=True):
            if same is not None:
                already += 1  # said before in nearly the same words
                continue
            provenance: dict[str, Any] = {'captured': cand['reason']}
            if agent:
                provenance['agent'] = agent
            if old:
                provenance['supersedes'] = old.get('id')  # saving the new one retires the old one
            reasons: list[str] = ['not_automatic'] if not auto else []
            rule = ''
            if auto:
                decision = evaluate_policy(
                    title=cand['title'], content=cand['content'], memory_type=cand['type'],
                    confidence=float(cand['confidence']), tags=tags, provenance=provenance, policy=policy,
                    scope_tag=[t for t in tags if str(t).lower().startswith('scope:')],
                )  # fmt: skip
                reasons, rule = list(decision.reasons), decision.rule
                if decision.ok and old:
                    if not open_mode:
                        reasons.append('would_replace_existing')
                    elif not await self._replacement_budget():
                        reasons.append('replacement_cap_reached')
                if decision.ok and not open_mode and not reasons:
                    key = normalize_title_key(cand['title'])
                    if any(
                        isinstance(e, dict) and e.get('status') == 'approved' and normalize_title_key(str(e.get('title') or '')) == key
                        for e in entries.values()
                    ):
                        reasons.append('would_replace_existing')
            plan.append({'cand': cand, 'old': old, 'provenance': provenance, 'reasons': reasons, 'rule': rule})
        wanted = [p for p in plan if not p['reasons']]
        cap = int(policy['daily_cap']) * (OPEN_CAP_FACTOR if open_mode else 1)
        granted = await self._auto_budget_take(len(wanted), cap)
        for p in wanted[granted:]:
            p['reasons'] = ['daily_cap_reached']

        rows: list[dict[str, Any]] = []
        recorded: list[tuple[str, str, str]] = []
        failed = 0
        used = 0
        if plan:
            await self.begin_batch()
            try:
                for p in plan:
                    cand, approve = p['cand'], not p['reasons']
                    prov = dict(p['provenance'])
                    if approve:
                        prov['autoApproved'] = {'rule': p['rule'], 'at': _now()}
                    res = await self.memory_save(
                        title=cand['title'], content=cand['content'], memory_type=cand['type'], tags=tags,
                        confidence=cand['confidence'], provenance=prov, approve=approve,
                    )  # fmt: skip
                    rec = res.get('memory') or {}
                    if not res.get('ok'):
                        failed += 1  # tell the caller instead of reporting a quiet success
                        continue
                    if res.get('duplicate'):
                        continue
                    row: dict[str, Any] = {'id': rec.get('id'), 'title': rec.get('title'), 'type': rec.get('type'), 'approved': approve}
                    rule = p['rule']
                    if p['old']:
                        row['replaces'] = {'id': p['old'].get('id'), 'title': p['old'].get('title')}
                    if approve:
                        used += 1
                        if p['old'] and await self._retire_superseded(rec, _now()):
                            rule = f"{rule}, replaced: {str(p['old'].get('title') or '')[:80]}"
                        recorded.append((str(rec.get('id')), str(rec.get('title') or ''), rule))
                    elif auto:
                        row['waiting'] = p['reasons']
                    rows.append(row)
            finally:
                await self.end_batch()
        if granted > used:
            await self._auto_budget(used - granted)  # hand back slots that were not needed after all
        await self._auto_record_many(recorded)
        return {'items': rows, 'already': already, 'failed': failed}

    async def note_decision(
        self, *, title: str, content: str, memory_type: str, confidence: float, tags: list[str], provenance: dict[str, Any]
    ) -> tuple[bool, list[str], str]:
        """May this note be kept at once? Decided before it is written, so it is written only once."""
        policy = await self.get_policy()
        open_mode = policy.get('mode') == MODE_OPEN
        decision = evaluate_policy(
            title=title, content=content, memory_type=memory_type, confidence=confidence, tags=tags,
            provenance=provenance, policy=policy, scope_tag=[t for t in tags if str(t).lower().startswith('scope:')],
        )  # fmt: skip
        if not decision.ok:
            return False, list(decision.reasons), ''
        if not open_mode:
            key = normalize_title_key(title)
            entries = await self._current_index_entries()
            if any(
                isinstance(e, dict) and e.get('status') == 'approved' and normalize_title_key(str(e.get('title') or '')) == key
                for e in entries.values()
            ):
                return False, ['would_replace_existing'], ''
        if not await self._auto_budget_take(1, int(policy['daily_cap']) * (OPEN_CAP_FACTOR if open_mode else 1)):
            return False, ['daily_cap_reached'], ''
        return True, [], decision.rule

    async def note_decision_settle(self, *, memory_id: str | None, title: str, rule: str, saved: bool) -> None:
        """After the write: list the note as kept by itself, or hand the daily slot back if nothing was written."""
        if saved and memory_id:
            await self._auto_record(memory_id, title, rule)
        else:
            await self._auto_budget(-1)

    async def find_by_dedupe(self, dedupe_key: str) -> str | None:
        manifest = await self._get_manifest()
        return (manifest.get('byDedupe') or {}).get(dedupe_key)

    async def get_sync_state(self) -> dict[str, Any]:
        assert self._kv is not None
        return dict(await self._kv.get_value(KEY_SYNC_STATE) or {})

    async def set_sync_state(self, plugin: str, state: dict[str, Any]) -> None:
        assert self._kv is not None
        data = await self.get_sync_state()
        data[plugin] = state
        await self._kv.set_value(KEY_SYNC_STATE, data)

    async def _repair_unlocked(self) -> dict[str, Any]:
        """Rebuild manifest + index + inbox from the memory.* records (source of truth).

        Heals lost updates when several agents write the same store at once.
        """
        assert self._kv is not None
        keys = await self._kv.list_keys(MEMORY_PREFIX)
        before = await self._get_manifest()
        before_ids = set(before.get('ids') or [])
        entries: dict[str, Any] = {}
        by_status = _empty_by_status()
        by_dedupe: dict[str, str] = {}
        inbox: list[str] = []
        live: list[str] = []
        for key in keys:
            mid = key[len(MEMORY_PREFIX):]
            rec = await self.get_memory(mid)
            if not isinstance(rec, dict) or not rec.get('id'):
                continue
            live.append(mid)
            entries[mid] = index_entry_from_record(rec)
            st = rec.get('status') if rec.get('status') in by_status else 'working'
            by_status[st].append(mid)
            if rec.get('dedupeKey'):
                by_dedupe[rec['dedupeKey']] = mid
            if st == 'proposed':
                inbox.append(mid)
        # Re-point the external-id key of a retired memory at its approved replacement, as
        # _retire_superseded does; rebuilding from records alone would lose that link.
        for mid in live:
            rec = await self.get_memory(mid)
            old_id = ((rec or {}).get('provenance') or {}).get('supersedes')
            if rec and rec.get('status') == 'approved' and old_id in entries:
                old = await self.get_memory(old_id)
                if old and old.get('status') == 'decayed' and old.get('dedupeKey'):
                    by_dedupe[old['dedupeKey']] = mid
        manifest = {
            'ids': live,
            'byDedupe': by_dedupe,
            'byStatus': by_status,
            'indexVersion': SEARCH_INDEX_VERSION,
        }
        await self._persist_search_index({'entries': entries})
        await self._persist_manifest(manifest)
        await self._persist_inbox(inbox)
        await self._persist_audit(event_type='repair', detail=f'memories={len(live)} inbox={len(inbox)}')
        return {
            'repaired': True,
            'memories': len(live),
            'addedToManifest': len(set(live) - before_ids),
            'droppedFromManifest': len(before_ids - set(live)),
            'inbox': len(inbox),
        }

    async def source_list(self) -> dict[str, Any]:
        sources = await self._get_sources()
        return {'ok': True, 'count': len(sources), 'sources': sources}

    async def source_remove(self, source_id: str) -> dict[str, Any]:
        async with self._sources_lock:
            sources = await self._get_sources()
            kept = [s for s in sources if s.get('id') != source_id]
            if len(kept) == len(sources):
                return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No source with id {source_id}'}
            await self._set_sources(kept)
        await self.audit('source_remove', source_id=source_id, detail='removed')
        return {'ok': True, 'removedId': source_id, 'remaining': len(kept)}

    async def memory_save(
        self,
        *,
        title: str,
        content: str,
        memory_type: str = 'note',
        tags: list[str] | None = None,
        citations: list[Any] | None = None,
        confidence: float = 0.7,
        approve: bool = False,
        dedupe_key: str | None = None,
        memory_id: str | None = None,
        external_ids: dict[str, str] | None = None,
        provenance: dict[str, Any] | None = None,
        dry_run: bool = False,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        owns_lock = not self._holds_batch()
        if owns_lock:
            await self._meta_lock.acquire()
        try:
            return await self._memory_save_locked(
                title=title,
                content=content,
                memory_type=memory_type,
                tags=tags,
                citations=citations,
                confidence=confidence,
                approve=approve,
                dedupe_key=dedupe_key,
                memory_id=memory_id,
                external_ids=external_ids,
                provenance=provenance,
                dry_run=dry_run,
                expires_at=expires_at,
            )
        finally:
            if owns_lock and self._meta_lock.locked():
                self._meta_lock.release()

    async def _memory_save_locked(
        self,
        *,
        title: str,
        content: str,
        memory_type: str = 'note',
        tags: list[str] | None = None,
        citations: list[Any] | None = None,
        confidence: float = 0.7,
        approve: bool = False,
        dedupe_key: str | None = None,
        memory_id: str | None = None,
        external_ids: dict[str, str] | None = None,
        provenance: dict[str, Any] | None = None,
        dry_run: bool = False,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        title = (title or '').strip()
        content = (content or '').strip()
        if not title or not content:
            return {'ok': False, 'error': 'INVALID_INPUT', 'message': 'title and content are required'}

        key = _dedupe_key(title, content, dedupe_key)
        manifest = await self._get_manifest()
        existing_id = (manifest.get('byDedupe') or {}).get(key)
        now = _now()

        if existing_id and not memory_id:
            existing = await self.get_memory(existing_id)
            if existing and existing.get('status') in ('decayed', 'rejected'):
                # The owner already decided this. A re-proposal (even with approve=true) must not
                # quietly reopen it. Different text, or a different dedupe_key, is a new memory.
                return {
                    'ok': True,
                    'duplicate': True,
                    'decided': True,
                    'dryRun': dry_run,
                    'memory': existing,
                    'message': (
                        f"You already {'rejected' if existing.get('status') == 'rejected' else 'forgot'} "
                        'a matching memory, so it was not re-proposed.'
                    ),
                }
            if existing:
                return {
                    'ok': True,
                    'duplicate': True,
                    'dryRun': dry_run,
                    'memory': existing,
                    'message': 'Matching dedupeKey already present; not rewritten.',
                }

        mid = memory_id or existing_id or _new_id('mem')
        prior = await self.get_memory(mid) if memory_id or existing_id else None
        if memory_id and prior and prior.get('status') == 'approved':
            return {
                'ok': False,
                'error': 'INVALID_STATE',
                'message': 'Approved memories are never overwritten; propose the change as a new memory.',
                'memory': prior,
            }
        status = 'approved' if approve else 'proposed'
        prov = dict(provenance or {})
        if prov:
            prov['importedAt'] = prov.get('importedAt') or now
        prior_ext = dict((prior or {}).get('externalIds') or {})
        if external_ids:
            prior_ext.update({str(k): str(v) for k, v in external_ids.items() if v})
        summary = make_summary(content)
        record = {
            'id': mid,
            'status': status,
            'type': (memory_type or 'note')[:40],
            'title': title[:500],
            'summary': summary,
            'content': content[:50_000],
            'tags': _clean_tags(tags),
            'dedupeKey': key,
            'citations': _clean_citations(citations),
            'confidence': _clamp_confidence(confidence),
            'externalIds': prior_ext,
            'provenance': prov or (prior or {}).get('provenance') or {},
            'createdAt': (prior or {}).get('createdAt') or now,
            'updatedAt': now,
            'approvedAt': now if approve else (prior or {}).get('approvedAt'),
        }
        if expires_at:
            record['expiresAt'] = expires_at
        elif (prior or {}).get('expiresAt'):
            record['expiresAt'] = prior['expiresAt']
        if dry_run:
            return {
                'ok': True,
                'duplicate': False,
                'dryRun': True,
                'memory': record,
                'message': f'Would {"approve" if approve else "propose"} {mid}',
            }

        await self._put_memory(record)

        inbox = await self._get_inbox()
        if status == 'proposed':
            if mid not in inbox:
                inbox.append(mid)
                await self._set_inbox(inbox)
            await self.audit('propose', memory_id=mid, detail=title[:200])
        else:
            inbox = [i for i in inbox if i != mid]
            await self._set_inbox(inbox)
            await self.audit('approve', memory_id=mid, detail='saved_as_approved')

        return {'ok': True, 'duplicate': False, 'dryRun': False, 'memory': record}

    async def import_plugin_export(
        self,
        raw: Any,
        *,
        source_plugin: str | None = None,
        approve: bool = False,
        dry_run: bool = False,
        limit: int = 200,
        extra_tags: list[str] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Import AgentMemory/Mem0/BasicMemory/generic export → proposed (default)."""
        normalized = normalize_plugin_export(raw, source_plugin=source_plugin, limit=limit)
        if not normalized.get('ok'):
            return normalized
        for rec in normalized['records']:
            if extra_tags:
                rec['tags'] = [*dict.fromkeys([*(rec.get('tags') or []), *extra_tags])][:40]
            if agent:
                rec['provenance'] = {**(rec.get('provenance') or {}), 'agent': agent}

        imported: list[dict[str, Any]] = []
        duplicates = 0
        errors: list[dict[str, Any]] = []
        if not dry_run:
            await self.begin_batch()
        try:
            for rec in normalized['records']:
                result = await self.memory_save(
                    title=rec['title'],
                    content=rec['content'],
                    memory_type=rec.get('memory_type') or 'note',
                    tags=rec.get('tags'),
                    citations=rec.get('citations'),
                    confidence=rec.get('confidence', 0.7),
                    approve=approve,
                    dedupe_key=rec.get('dedupe_key'),
                    external_ids=rec.get('external_ids'),
                    provenance=rec.get('provenance'),
                    dry_run=dry_run,
                )
                if not result.get('ok'):
                    errors.append({'title': rec.get('title'), 'error': result.get('error')})
                    continue
                if result.get('duplicate'):
                    duplicates += 1
                imported.append(
                    {
                        'id': (result.get('memory') or {}).get('id'),
                        'status': (result.get('memory') or {}).get('status'),
                        'duplicate': bool(result.get('duplicate')),
                        'title': (result.get('memory') or {}).get('title'),
                        'externalIds': (result.get('memory') or {}).get('externalIds') or {},
                    }
                )
        finally:
            if not dry_run:
                await self.end_batch()

        await self.audit(
            'import',
            detail=(
                f"plugin={normalized.get('sourcePlugin')} format={normalized.get('format')} "
                f"imported={len(imported)} dupes={duplicates} dryRun={dry_run} approve={approve}"
            )[:1000],
        )
        return {
            'ok': True,
            'dryRun': dry_run,
            'approve': approve,
            'format': normalized.get('format'),
            'sourcePlugin': normalized.get('sourcePlugin'),
            'exportVersion': normalized.get('exportVersion'),
            'count': len(imported),
            'duplicates': duplicates,
            'skipped': normalized.get('skipped', 0),
            'truncated': normalized.get('truncated', False),
            'errors': errors,
            'items': imported,
            'message': (
                f"{'Would import' if dry_run else 'Imported'} {len(imported)} from "
                f"{normalized.get('sourcePlugin')} ({normalized.get('format')}); "
                f"{duplicates} duplicates; status={'approved' if approve else 'proposed'}"
            ),
        }

    async def inbox_approve_many(
        self,
        memory_ids: list[str],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        ids = [str(i).strip() for i in (memory_ids or []) if str(i).strip()]
        if not ids:
            return {'ok': False, 'error': 'INVALID_INPUT', 'message': 'memory_ids required'}
        if len(ids) > 100:
            ids = ids[:100]
        results: list[dict[str, Any]] = []
        if not dry_run:
            await self.begin_batch()
        try:
            for mid in ids:
                if dry_run:
                    rec = await self.get_memory(mid)
                    results.append(
                        {
                            'ok': bool(rec),
                            'memoryId': mid,
                            'dryRun': True,
                            'wouldApprove': bool(rec and rec.get('status') in ('proposed', 'working')),
                            'status': (rec or {}).get('status'),
                        }
                    )
                    continue
                out = await self.inbox_approve(mid)
                results.append(
                    {
                        'ok': bool(out.get('ok')),
                        'memoryId': mid,
                        'error': out.get('error'),
                        'status': (out.get('memory') or {}).get('status'),
                    }
                )
        finally:
            if not dry_run:
                await self.end_batch()
        ok_count = sum(1 for r in results if r.get('ok') and (r.get('wouldApprove') if dry_run else True))
        return {
            'ok': True,
            'dryRun': dry_run,
            'count': len(results),
            'approved': ok_count,
            'results': results,
            'message': f"{'Would approve' if dry_run else 'Approved'} {ok_count}/{len(results)}",
        }

    async def memory_search(
        self,
        query: str,
        *,
        approved_only: bool = True,
        statuses: list[str] | None = None,
        limit: int = 20,
        tags: list[str] | None = None,
        scope: str | None = None,
        use_meaning: bool = True,
    ) -> dict[str, Any]:
        """Ranked search over the thin index. Reads take no lock (see _current_index_entries)."""
        return await self._memory_search_unlocked(
            query,
            approved_only=approved_only,
            statuses=statuses,
            limit=limit,
            tags=tags,
            scope=scope,
            use_meaning=use_meaning,
        )

    async def _memory_search_unlocked(
        self,
        query: str,
        *,
        approved_only: bool = True,
        statuses: list[str] | None = None,
        limit: int = 20,
        tags: list[str] | None = None,
        scope: str | None = None,
        use_meaning: bool = True,
    ) -> dict[str, Any]:
        if self._holds_batch():  # inside an import batch: use its in-memory index, no cache
            entries = dict((await self._get_search_index()).get('entries') or {})
            rank_index = None
        else:
            entries = await self._current_index_entries()
            rank_index = self._rank
        allowed = (
            set(statuses)
            if statuses
            else ({'approved'} if approved_only else {'working', 'proposed', 'approved'})
        )
        tag_filter = {t.lower() for t in (tags or [])}
        scope_tag = scope_tag_for(scope)
        candidates: list[dict[str, Any]] = []
        now_dt = datetime.now(UTC)
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            if entry.get('status') not in allowed:
                continue
            if is_expired(entry, now_dt):
                continue
            if scope_tag and scope_tag not in {str(t).lower() for t in (entry.get('tags') or [])}:
                continue
            if tag_filter:
                rec_tags = {str(t).lower() for t in (entry.get('tags') or [])}
                if not tag_filter.intersection(rec_tags):
                    continue
            candidates.append(entry)

        meaning = None
        cosines: dict[str, float] = {}
        if (
use_meaning
            and self._embedder is not None
            and self._vectors is not None
            and candidates
            and not self._holds_batch()
            and time.monotonic() >= self._embed_down_until
        ):
            try:
                cosines = await asyncio.wait_for(self._meaning_scores(query, candidates, set(entries)), timeout=10)
                meaning = 'on'
            except Exception as exc:  # noqa: BLE001 - provider trouble must never break search
                meaning = 'unavailable'
                self._embed_down_until = time.monotonic() + EMBED_BREAKER_S
                logging.getLogger('second_brain.embeddings').warning('meaning search failed: %s', type(exc).__name__)
        wide = max(limit * 3, 30)  # room for the per-page cap below
        ranked = hybrid_rank(query, candidates, limit=wide, rank_index=rank_index)
        if cosines:
            ranked = fuse(ranked, cosines, {c['id']: c for c in candidates if c.get('id')}, wide)
        ranked = _cap_per_page(ranked, limit)
        retrieved_at = _now()
        results: list[dict[str, Any]] = []
        for rank, row in enumerate(ranked, start=1):
            entry = row['entry']
            mid = entry.get('id')
            terms = list(row.get('matchedTerms') or [])
            results.append(
                {
                    'rank': rank,
                    'score': row['score'],
                    'memory': card_from_index_entry(entry),
                    'receipt': {
                        'memoryId': mid,
                        'status': entry.get('status'),
                        'rank': rank,
                        'score': row['score'],
                        'query': query,
                        'matchedTerms': terms,
                        'sourceUris': list(entry.get('sourceUris') or []),
                        'citations': [{'url': u} for u in (entry.get('sourceUris') or [])],
                        'retrievedAt': retrieved_at,
                        'scorer': SCORER + ('+meaning' if meaning == 'on' else ''),
                        'signals': row.get('signals') or {},
                        'indexVersion': SEARCH_INDEX_VERSION,
                    },
                }
            )
        return {
            'ok': True,
            'query': query,
            'approvedOnly': approved_only,
            'statuses': sorted(allowed),
            'count': len(results),
            'retrievedAt': retrieved_at,
            'indexHits': len(candidates),
            'scorer': SCORER + ('+meaning' if meaning == 'on' else ''),
            **({'meaning': meaning} if meaning else {}),
            'results': results,
        }

    async def _meaning_scores(
        self, query: str, candidates: list[dict[str, Any]], live_ids: set[str]
    ) -> dict[str, float]:
        """Cosine of the query to each candidate. Fills in a small batch of missing vectors per call."""
        assert self._embedder is not None and self._vectors is not None
        store = self._vectors
        await store.load()
        store.prune(live_ids)
        wanted = [(c, memory_text(c)) for c in candidates if c.get('id')]
        missing = [(c, t) for c, t in wanted if store.get(c['id'], text_hash(t)) is None][: MAX_BATCH - 1]
        vectors = await self._embedder.embed([query] + [t for _, t in missing])
        qvec = vectors[0]
        for (c, t), vec in zip(missing, vectors[1:]):
            store.put(c['id'], text_hash(t), vec)
        await store.save()
        items = [(c['id'], v) for c, t in wanted if (v := store.get(c['id'], text_hash(t))) is not None]
        return await cosines_async(qvec, items)

    async def _recent_audit_for(self, memory_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Best-effort recent audit rows for one memory (bounded scan)."""
        assert self._audit is not None
        events: list[dict[str, Any]] = []
        try:
            # Newest first: oldest-first would hide recent events once the log passes 500.
            async for item in self._audit.iterate_items(limit=500, desc=True):
                if item.get('memoryId') == memory_id:
                    events.append(
                        {
                            'eventType': item.get('eventType'),
                            'detail': item.get('detail'),
                            'at': item.get('at'),
                            'sourceId': item.get('sourceId'),
                        }
                    )
        except Exception:  # noqa: BLE001 — audit is explanatory, never hard-fail explain
            return []
        events.sort(key=lambda e: str(e.get('at') or ''), reverse=True)
        return events[:limit]

    async def memory_explain(self, memory_id: str) -> dict[str, Any]:
        """Full memory body + provenance receipt (why it exists / what cites it)."""
        rec = await self.get_memory(memory_id)
        if not rec:
            return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No memory {memory_id}'}

        source_uris = citation_uris(rec)
        linked_sources: list[dict[str, Any]] = []
        for cite in rec.get('citations') or []:
            if not isinstance(cite, dict):
                continue
            sid = cite.get('sourceId')
            if not sid:
                continue
            for src in await self._get_sources():
                if src.get('id') == sid:
                    linked_sources.append(
                        {
                            'id': src.get('id'),
                            'url': src.get('url'),
                            'title': src.get('title'),
                        }
                    )
                    break

        audit = await self._recent_audit_for(memory_id)
        why_parts = [
            f"status={rec.get('status')}",
            f"type={rec.get('type')}",
            f"confidence={rec.get('confidence')}",
        ]
        if source_uris:
            why_parts.append(f"cited_sources={len(source_uris)}")
        if audit:
            why_parts.append(f"last_event={audit[0].get('eventType')}@{audit[0].get('at')}")

        return {
            'ok': True,
            'memory': rec,
            'card': memory_card(rec, snippet_chars=500),
            'receipt': {
                'memoryId': memory_id,
                'status': rec.get('status'),
                'type': rec.get('type'),
                'sourceUris': source_uris,
                'citations': list(rec.get('citations') or []),
                'linkedSources': linked_sources,
                'createdAt': rec.get('createdAt'),
                'updatedAt': rec.get('updatedAt'),
                'approvedAt': rec.get('approvedAt'),
                'decayedAt': rec.get('decayedAt'),
                'rejectReason': rec.get('rejectReason'),
                'decayReason': rec.get('decayReason'),
                'auditTrail': audit,
                'why': '; '.join(why_parts),
                'explainedAt': _now(),
            },
            'message': rec.get('title') or memory_id,
        }

    async def brain_health(
        self,
        *,
        include_sources: bool = True,
        remove_source_id: str | None = None,
        repair_index: bool = False,
    ) -> dict[str, Any]:
        # Plain health read: take the entry count from the cached index (before the lock).
        plain = not remove_source_id and not repair_index
        index_count = len(await self._index_entries_fast()) if plain else None
        async with self._meta_lock:
            removed = None
            if remove_source_id:
                removed = await self.source_remove(remove_source_id)
            repaired = await self._repair_unlocked() if repair_index else None
            assert self._kv is not None
            stats, job_keys, sources = await asyncio.gather(
                self._brain_stats_unlocked(index_count),
                self._kv.list_keys(INGEST_JOB_PREFIX),
                self._get_sources() if include_sources else asyncio.sleep(0, result=None),
            )
            if repaired is not None:
                stats['repair'] = repaired
            stats['backend'] = self.backend_name
            stats['ingestQueueJobs'] = len(job_keys)
            if include_sources:
                stats['sources'] = sources or []
            if removed is not None:
                stats['sourceRemove'] = removed
            stats['toolSurface'] = [
                'memory_propose',
                'inbox_list',
                'inbox_approve',
                'inbox_reject',
                'memory_search',
                'memory_explain',
                'memory_forget',
                'brain_health',
            ]
            stats['version'] = VERSION
            stats['searchIndex'] = {
                'key': KEY_SEARCH_INDEX,
                'version': SEARCH_INDEX_VERSION,
                'scorer': SCORER,
            }
            stats['importFormats'] = [
                'agentmemory',
                'mem0',
                'basic_memory',
                'generic_list',
                'generic_bundle',
                'single',
            ]
            return stats

    # --- automatic approval ------------------------------------------------------------------
    async def open_mode(self) -> bool:
        """True when the owner lets the server save everything by itself (and this server may approve)."""
        return bool(self.auto_open) and (await self.get_policy()).get('mode') == MODE_OPEN

    async def auto_file(self, memory_id: str | None) -> dict[str, Any]:
        """In automatic mode, approve a just-filed web page or feed item (its passages follow it)."""
        if not memory_id or not await self.open_mode():
            return {'approved': False, 'reasons': ['not_automatic']}
        return await self.consider_auto_approval(memory_id)

    async def get_policy(self) -> dict[str, Any]:
        assert self._kv is not None
        return load_policy(await self._kv.get_value(KEY_POLICY))

    async def set_policy(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Owner-only (CLI): merge `updates` into the stored policy and return the effective one."""
        assert self._kv is not None
        stored = dict(await self._kv.get_value(KEY_POLICY) or {})
        stored.update(updates)
        effective = load_policy(stored)
        await self._kv.set_value(KEY_POLICY, {k: effective[k] for k in effective})
        await self.audit('policy', detail=','.join(sorted(updates))[:200])
        return effective

    async def _auto_budget(self, delta: int, cap: int | None = None) -> bool:
        """Take (+1) or give back (-1) one automatic approval for today. False if the cap is hit."""
        assert self._kv is not None
        async with self._meta_lock:
            stats = await self._kv.get_value(KEY_AUTO_STATS)
            stats = dict(stats) if isinstance(stats, dict) else {}
            today = _now()[:10]
            if stats.get('date') != today:
                stats['date'], stats['count'] = today, 0
            count = int(stats.get('count') or 0)
            if delta > 0 and cap is not None and count >= cap:
                return False
            stats['count'] = max(count + delta, 0)
            await self._kv.set_value(KEY_AUTO_STATS, stats)
            return True

    async def _replacement_budget(self) -> bool:
        """Count one automatic replacement for today. False once the daily limit is used up."""
        assert self._kv is not None
        async with self._meta_lock:
            stats = await self._kv.get_value(KEY_AUTO_STATS)
            stats = dict(stats) if isinstance(stats, dict) else {}
            today = _now()[:10]
            if stats.get('replaceDate') != today:
                stats['replaceDate'], stats['replaceCount'] = today, 0
            if int(stats.get('replaceCount') or 0) >= MAX_REPLACEMENTS_PER_DAY:
                return False
            stats['replaceCount'] = int(stats.get('replaceCount') or 0) + 1
            await self._kv.set_value(KEY_AUTO_STATS, stats)
            return True

    async def _auto_record(self, memory_id: str, title: str, rule: str) -> None:
        assert self._kv is not None
        async with self._meta_lock:
            stats = await self._kv.get_value(KEY_AUTO_STATS)
            stats = dict(stats) if isinstance(stats, dict) else {}
            recent = list(stats.get('recent') or [])
            recent.append({'id': memory_id, 'title': title[:120], 'rule': rule, 'at': _now()})
            stats['recent'] = recent[-20:]
            await self._kv.set_value(KEY_AUTO_STATS, stats)

    async def consider_auto_approval(self, memory_id: str) -> dict[str, Any]:
        """Serialised: the "would replace an existing memory" check and the approval are one step."""
        async with self._auto_lock:
            return await self._consider_auto_approval_locked(memory_id)

    async def _consider_auto_approval_locked(self, memory_id: str) -> dict[str, Any]:
        """Approve a PROPOSED memory only if the owner's policy allows it. Never raises on 'no'.

        Returns {approved, rule?, reasons?}. Reasons name every failed check so the owner (or the
        agent) can see why it is waiting in the inbox.
        """
        rec = await self.get_memory(memory_id)
        if not rec:
            return {'approved': False, 'reasons': ['not_found']}
        if rec.get('status') != 'proposed':
            return {'approved': False, 'reasons': [f"status_{rec.get('status')}"]}
        policy = await self.get_policy()
        tags = list(rec.get('tags') or [])
        scope_tags = [str(t) for t in tags if str(t).lower().startswith('scope:')]
        decision = evaluate_policy(
            title=str(rec.get('title') or ''),
            content=str(rec.get('content') or ''),
            memory_type=str(rec.get('type') or 'note'),
            confidence=float(rec.get('confidence') or 0.0),
            tags=tags,
            provenance=rec.get('provenance') or {},
            policy=policy,
            scope_tag=scope_tags,
        )
        reasons = list(decision.reasons)
        open_mode = policy.get('mode') == MODE_OPEN
        replaced_id = (rec.get('provenance') or {}).get('supersedes')
        if decision.ok and replaced_id:
            # Retiring an older memory is the one automatic step that removes something, so it is
            # allowed only in the automatic mode, a few times a day, and is always listed for the owner.
            if not open_mode:
                reasons.append('would_replace_existing')
            elif not await self._replacement_budget():
                reasons.append('replacement_cap_reached')
        if decision.ok and not open_mode:
            title_key = normalize_title_key(str(rec.get('title') or ''))
            entries = await self._current_index_entries()
            clash = any(
                e.get('status') == 'approved'
                and e.get('id') != memory_id
                and normalize_title_key(str(e.get('title') or '')) == title_key
                for e in entries.values()
                if isinstance(e, dict)
            )
            if clash:
                reasons.append('would_replace_existing')
        if reasons:
            return {'approved': False, 'reasons': reasons}
        if not await self._auto_budget(+1, int(policy['daily_cap']) * (OPEN_CAP_FACTOR if open_mode else 1)):
            return {'approved': False, 'reasons': ['daily_cap_reached']}
        try:
            result = await self.inbox_approve(memory_id, auto_rule=decision.rule)
        except BaseException:
            await self._auto_budget(-1)  # give the daily slot back, then let the error surface
            raise
        if not result.get('ok'):
            await self._auto_budget(-1)
            return {'approved': False, 'reasons': [str(result.get('error') or 'approve_failed').lower()]}
        rule = decision.rule
        if result.get('retired'):
            old = await self.get_memory(str(result['retired']))
            rule = f"{rule}, replaced: {str((old or {}).get('title') or result['retired'])[:80]}"
        await self._auto_record(memory_id, str(rec.get('title') or ''), rule)
        return {'approved': True, 'rule': decision.rule, 'memory': result.get('memory')}

    async def profile_cards(self, limit: int = 5, scope: str | None = None) -> list[dict[str, Any]]:
        """Newest approved preferences / profile facts (what an agent should always know)."""
        entries = await self._current_index_entries()
        scope_tag = scope_tag_for(scope)
        now = _now()
        rows = [
            e
            for e in entries.values()
            if isinstance(e, dict)
            and e.get('status') == 'approved'
            and e.get('type') in ('preference', 'profile')
            and not (e.get('expiresAt') and str(e['expiresAt']) <= now)
            and (not scope_tag or scope_tag in {str(t).lower() for t in (e.get('tags') or [])})
        ]
        rows.sort(key=lambda e: str(e.get('updatedAt') or ''), reverse=True)
        return [card_from_index_entry(e) for e in rows[: max(limit, 0)]]

    async def memory_list(
        self,
        *,
        statuses: list[str] | None = None,
        tag: str | None = None,
        sort: str = 'newest',
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Page through memories from the thin index (owner dashboard). Lock-free read, no bodies."""
        entries = await self._current_index_entries()
        allowed = {str(x) for x in statuses} if statuses else {'approved'}
        want = (tag or '').strip().lower()
        rows = [
            e
            for e in entries.values()
            if isinstance(e, dict)
            and e.get('status') in allowed
            and (not want or want in {str(t).lower() for t in (e.get('tags') or [])})
        ]
        if sort == 'title':
            rows.sort(key=lambda e: str(e.get('title') or '').lower())
        else:
            rows.sort(key=lambda e: str(e.get('updatedAt') or e.get('approvedAt') or ''), reverse=(sort != 'oldest'))
        limit = min(max(int(limit or 50), 1), 100)
        offset = max(int(offset or 0), 0)
        return {
            'ok': True,
            'total': len(rows),
            'offset': offset,
            'items': [card_from_index_entry(e) for e in rows[offset : offset + limit]],
        }

    async def inbox_list(self) -> dict[str, Any]:
        """Return progressive cards (not full bodies) for proposed inbox items."""
        entries = await self._index_entries_fast()  # cached; one tiny read when nothing changed
        async with self._meta_lock:
            ids, stats = await asyncio.gather(self._get_inbox(), self._kv.get_value(KEY_AUTO_STATS))  # type: ignore[union-attr]
            items: list[dict[str, Any]] = []
            hidden = 0
            for mid in ids:
                entry = entries.get(mid)
                if entry and entry.get('status') == 'proposed':
                    if _is_passage(entry):
                        hidden += 1
                        continue
                    items.append(card_from_index_entry(entry))
                    continue
                rec = await self.get_memory(mid)
                if rec and rec.get('status') == 'proposed':
                    if _is_passage(rec):
                        hidden += 1
                        continue
                    items.append(memory_card(rec))
            recent = list((stats or {}).get('recent') or []) if isinstance(stats, dict) else []
            return {
                'ok': True,
                'count': len(items),
                'items': items,
                'cardOnly': True,
                'passagesFollowingTheirPage': hidden,
                'recentAutoApproved': recent[-10:],
            }

    async def inbox_approve(self, memory_id: str, *, auto_rule: str | None = None) -> dict[str, Any]:
        if not self._holds_batch():  # one write of the index however many passages follow the page
            await self.begin_batch()
            try:
                return await self.inbox_approve(memory_id, auto_rule=auto_rule)
            finally:
                await self.end_batch()
        owns_lock = not self._holds_batch()
        if owns_lock:
            await self._meta_lock.acquire()
        try:
            rec = await self.get_memory(memory_id)
            if not rec:
                return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No memory {memory_id}'}
            if rec.get('status') not in ('proposed', 'working'):
                return {
                    'ok': False,
                    'error': 'INVALID_STATE',
                    'message': f"Cannot approve status={rec.get('status')}",
                    'memory': rec,
                }
            now = _now()
            rec['status'] = 'approved'
            rec['approvedAt'] = now
            rec['updatedAt'] = now
            if auto_rule:
                rec['provenance'] = {**(rec.get('provenance') or {}), 'autoApproved': {'rule': auto_rule, 'at': now}}
            if not rec.get('summary'):
                rec['summary'] = make_summary(str(rec.get('content') or ''))
            await self._put_memory(rec)
            inbox = [i for i in await self._get_inbox() if i != memory_id]
            await self._set_inbox(inbox)
            await self.audit(
                'approve',
                memory_id=memory_id,
                detail=f'auto:{auto_rule}' if auto_rule else rec.get('title'),
            )
            retired = await self._retire_superseded(rec, now)
            passages = await self._cascade_children(rec, 'approved', now)
            return {'ok': True, 'memory': rec, **({'retired': retired} if retired else {}), **({'passages': passages} if passages else {})}
        finally:
            if owns_lock and self._meta_lock.locked():
                self._meta_lock.release()

    async def _retire_superseded(self, rec: dict[str, Any], now: str) -> str | None:
        """Approving a proposal that `supersedes` an approved memory retires the old one.

        Without this, search would return both the old and the new version as approved canon.
        The external-id dedupe key is re-pointed at the new memory so later syncs keep tracking
        the chain instead of dead-ending on the retired record.
        """
        old_id = (rec.get('provenance') or {}).get('supersedes')
        if not old_id:
            return None
        old = await self.get_memory(old_id)
        if not old or old.get('status') != 'approved':
            return None
        old.update(
            status='decayed',
            updatedAt=now,
            decayedAt=now,
            decayReason=f"superseded by {rec['id']}",
        )
        await self._put_memory(old)
        if old.get('dedupeKey'):
            manifest = await self._get_manifest()
            by_dedupe = dict(manifest.get('byDedupe') or {})
            by_dedupe[old['dedupeKey']] = rec['id']
            manifest['byDedupe'] = by_dedupe
            await self._set_manifest(manifest)
        await self.audit('decay', memory_id=old_id, detail=f"superseded by {rec['id']}")
        return old_id

    async def inbox_reject(self, memory_id: str, reason: str | None = None) -> dict[str, Any]:
        if not self._holds_batch():  # one write of the index however many passages follow the page
            await self.begin_batch()
            try:
                return await self.inbox_reject(memory_id, reason)
            finally:
                await self.end_batch()
        owns_lock = not self._holds_batch()
        if owns_lock:
            await self._meta_lock.acquire()
        try:
            rec = await self.get_memory(memory_id)
            if not rec:
                return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No memory {memory_id}'}
            if rec.get('status') not in ('proposed', 'working'):
                return {
                    'ok': False,
                    'error': 'INVALID_STATE',
                    'message': (
                        f"Cannot reject status={rec.get('status')}; rejecting declines a proposal. "
                        'Use memory_forget for an approved memory.'
                    ),
                    'memory': rec,
                }
            now = _now()
            rec['status'] = 'rejected'
            rec['updatedAt'] = now
            if reason:
                rec['rejectReason'] = reason[:500]
            await self._put_memory(rec)
            inbox = [i for i in await self._get_inbox() if i != memory_id]
            await self._set_inbox(inbox)
            await self.audit('reject', memory_id=memory_id, detail=reason or rec.get('title'))
            await self._cascade_children(rec, 'rejected', now)
            return {'ok': True, 'memory': rec}
        finally:
            if owns_lock and self._meta_lock.locked():
                self._meta_lock.release()

    async def memory_edit(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> dict[str, Any]:
        """Owner edit (dashboard only, never an agent tool): change the text of a proposed or approved memory in place."""
        owns_lock = not self._holds_batch()
        if owns_lock:
            await self._meta_lock.acquire()
        try:
            rec = await self.get_memory(memory_id)
            if not rec:
                return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No memory {memory_id}'}
            if rec.get('status') not in ('approved', 'proposed'):
                return {'ok': False, 'error': 'INVALID_STATE', 'message': 'Only approved or proposed memories can be edited.'}
            new_title = (title if title is not None else str(rec.get('title') or '')).strip()
            new_content = (content if content is not None else str(rec.get('content') or '')).strip()
            if not new_title or not new_content:
                return {'ok': False, 'error': 'INVALID_INPUT', 'message': 'title and content are required'}
            new_tags = _clean_tags(tags) if tags is not None else list(rec.get('tags') or [])
            new_type = (memory_type or str(rec.get('type') or 'note'))[:40]
            before = (rec.get('title'), rec.get('content'), list(rec.get('tags') or []), rec.get('type'))
            if before == (new_title[:500], new_content[:50_000], new_tags, new_type):
                return {'ok': True, 'changed': False, 'memory': rec, 'message': 'Nothing changed.'}
            text_changed = before[:2] != (new_title[:500], new_content[:50_000])
            if text_changed:
                key = _dedupe_key(new_title, new_content, None)
                other = ((await self._get_manifest()).get('byDedupe') or {}).get(key)
                if other and other != memory_id:
                    return {'ok': False, 'error': 'DUPLICATE', 'message': 'Another memory already says exactly this.'}
                rec['dedupeKey'] = key
            now = _now()
            rec.update(
                {
                    'title': new_title[:500],
                    'content': new_content[:50_000],
                    'summary': make_summary(new_content),
                    'tags': new_tags,
                    'type': new_type,
                    'updatedAt': now,
                    'editedAt': now,
                }
            )
            await self._put_memory(rec)
            await self.audit('edit', memory_id=memory_id, detail='owner_edit')
            return {'ok': True, 'changed': True, 'memory': rec}
        finally:
            if owns_lock and self._meta_lock.locked():
                self._meta_lock.release()

    async def export_memories(self, statuses: list[str] | None = None, *, limit: int = 5000) -> dict[str, Any]:
        """Full records for the owner's own backup. Read only."""
        entries = await self._current_index_entries()
        allowed = {str(x) for x in statuses} if statuses else {'approved'}
        ids = [mid for mid, e in entries.items() if isinstance(e, dict) and e.get('status') in allowed]
        truncated = len(ids) > limit
        ids = ids[:limit]
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), 20):
            got = await asyncio.gather(*(self.get_memory(mid) for mid in ids[start : start + 20]))
            out.extend(r for r in got if isinstance(r, dict))
        return {'ok': True, 'exportedAt': _now(), 'count': len(out), 'truncated': truncated, 'memories': out}

    async def memory_forget(self, memory_id: str, reason: str | None = None) -> dict[str, Any]:
        """Soft decay — keeps the record, marks status=decayed."""
        if not self._holds_batch():  # one write of the index however many passages follow the page
            await self.begin_batch()
            try:
                return await self.memory_forget(memory_id, reason)
            finally:
                await self.end_batch()
        owns_lock = not self._holds_batch()
        if owns_lock:
            await self._meta_lock.acquire()
        try:
            rec = await self.get_memory(memory_id)
            if not rec:
                return {'ok': False, 'error': 'NOT_FOUND', 'message': f'No memory {memory_id}'}
            if rec.get('status') == 'decayed':
                return {'ok': True, 'changed': False, 'memory': rec, 'message': 'Already forgotten; nothing changed.'}
            now = _now()
            rec['status'] = 'decayed'
            rec['updatedAt'] = now
            rec['decayedAt'] = now
            if reason:
                rec['decayReason'] = reason[:500]
            await self._put_memory(rec)
            inbox = [i for i in await self._get_inbox() if i != memory_id]
            await self._set_inbox(inbox)
            await self.audit('decay', memory_id=memory_id, detail=reason or 'soft_forget')
            await self._cascade_children(rec, 'decayed', now)
            return {'ok': True, 'memory': rec}
        finally:
            if owns_lock and self._meta_lock.locked():
                self._meta_lock.release()

    async def brain_stats(self) -> dict[str, Any]:
        """Status counts from manifest byStatus — no N body loads."""
        async with self._meta_lock:
            return await self._brain_stats_unlocked()

    async def _brain_stats_unlocked(self, index_count: int | None = None) -> dict[str, Any]:
        assert self._kv is not None
        manifest = await self._get_manifest()
        ids = list(manifest.get('ids') or [])
        by_status_lists = manifest.get('byStatus') or _empty_by_status()
        by_status = {st: len(by_status_lists.get(st) or []) for st in STATUSES}
        shard_total = sum(by_status.values())
        # Rebuild once when shards lag behind ids (not when empty store).
        if ids and shard_total != len(ids):
            await self._rebuild_search_index()
            manifest = await self._get_manifest()
            by_status_lists = manifest.get('byStatus') or _empty_by_status()
            by_status = {st: len(by_status_lists.get(st) or []) for st in STATUSES}
            ids = list(manifest.get('ids') or [])
        sources, inbox, profile = await asyncio.gather(
            self._get_sources(), self._get_inbox(), self._kv.get_value(KEY_PROFILE)
        )
        profile = profile or {}
        if index_count is None:
            index_count = len((await self._get_search_index()).get('entries') or {})
        return {
            'ok': True,
            'kvStore': KV_STORE_NAME,
            'auditDataset': AUDIT_DATASET_NAME,
            'memoryTotal': len(ids),
            'byStatus': by_status,
            'indexEntryCount': index_count,
            'indexVersion': SEARCH_INDEX_VERSION,
            'sourceCount': len(sources),
            'inboxCount': len(inbox),
            'profile': profile,
            'lifecycle': list(STATUSES),
        }


_brain: BrainStore | None = None
_brain_lock = asyncio.Lock()


async def get_brain(default_backend: str = BACKEND_ACTOR) -> BrainStore:
    """Process-wide singleton. Backend: SECOND_BRAIN_BACKEND env, else `default_backend`."""
    global _brain
    if _brain is not None:
        return _brain
    async with _brain_lock:  # two first calls must not open the store twice
        if _brain is None:
            candidate = BrainStore(default_backend=default_backend)
            candidate.auto_open = _server_may_approve()
            await candidate.init()
            _brain = candidate
    return _brain


def _server_may_approve() -> bool:
    """True when this process was started with the approval mode that lets the owner's rules approve."""
    import os

    return (os.environ.get('SECOND_BRAIN_APPROVAL') or '').strip().lower() == 'auto'
