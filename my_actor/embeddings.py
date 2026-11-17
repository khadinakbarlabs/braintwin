"""Optional meaning search (embeddings). OFF unless the owner points the Actor at an embeddings API.

Why optional: the built-in ranker (ranking.py) needs no model, no service and no extra cost. It finds
spelling variants and a few related words, but not a paraphrase that shares no words with the note
("what do I drink in the morning" -> "oat milk flat white"). An embeddings API can. Turning it on
means memory titles and summaries are SENT to that API, and you pay that provider directly, so it
is never on by default.

Turn on (all three needed; the key is a private secret, never an input):
  SECOND_BRAIN_EMBEDDINGS_URL    https base of an OpenAI-compatible API, e.g. https://api.openai.com/v1
  SECOND_BRAIN_EMBEDDINGS_MODEL  model name, e.g. text-embedding-3-small
  SECOND_BRAIN_EMBEDDINGS_KEY    the API key
  SECOND_BRAIN_EMBEDDINGS_DIMS   optional; shorter vectors are cheaper to store (model must support it)

Behaviour: vectors are stored in your key-value store (`__embeddings`) and filled in lazily, a
small batch per search. If the API fails or is slow, search silently falls back to the normal
ranking. Nothing here is charged by this Actor.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
from urllib.parse import urlparse
from array import array
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

KEY_EMBEDDINGS = '__embeddings'
MAX_BATCH = 32  # texts sent to the API per search (backfill is spread over several searches)
MAX_TEXT_CHARS = 1500
EMBED_BREAKER_S = 60.0  # after a provider failure, skip it for this long (keyword search keeps working)
MAX_VECTORS = 4000  # memory cap: ~4000 x 256 dims fits easily in 512 MB
REQUEST_TIMEOUT_S = 8.0
MIN_COSINE = 0.30  # absolute floor: below this a memory is never a meaning match
MIN_POOL = 10  # z-scores need a pool of memories; with fewer we use plain cosine thresholds
Z_ADMIT = 2.8  # a memory with NO shared word is listed only if it stands this many std-devs above the rest
Z_FULL = 4.5  # at/above this the meaning signal counts fully
SMALL_POOL_ADMIT = 0.45  # cosine needed to list a meaning-only memory when the pool is small
SMALL_POOL_FULL = 0.70
KW_FULL = 1.0  # a keyword score at/above this counts as a full keyword match
W_KEYWORD = 0.5
W_MEANING = 0.5


class AsyncEmbedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingConfig:
    url: str
    model: str
    key: str = field(repr=False)  # never print the key
    dims: int | None = None


def embedding_config(env: dict[str, str] | None = None) -> EmbeddingConfig | None:
    """Read the owner's settings. Returns None (feature off) unless all three required are present."""
    e = env if env is not None else dict(os.environ)
    url = (e.get('SECOND_BRAIN_EMBEDDINGS_URL') or '').strip().rstrip('/')
    model = (e.get('SECOND_BRAIN_EMBEDDINGS_MODEL') or '').strip()
    key = (e.get('SECOND_BRAIN_EMBEDDINGS_KEY') or '').strip()
    if not (url and model and key):
        return None
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None  # https only; no credentials, query or fragment inside the address
    dims_raw = (e.get('SECOND_BRAIN_EMBEDDINGS_DIMS') or '').strip()
    dims = int(dims_raw) if dims_raw.isdigit() and 16 <= int(dims_raw) <= 3072 else None
    return EmbeddingConfig(url=url, model=model, key=key, dims=dims)


class HttpEmbedder:
    """OpenAI-compatible `POST {url}/embeddings`."""

    def __init__(self, config: EmbeddingConfig, client: httpx.AsyncClient | None = None) -> None:
        self.model = config.model
        self.dims = config.dims
        self._config = config
        self._client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {'model': self._config.model, 'input': [t[:MAX_TEXT_CHARS] for t in texts]}
        if self._config.dims:
            body['dimensions'] = self._config.dims
        headers = {'Authorization': f'Bearer {self._config.key}', 'Content-Type': 'application/json'}
        client = self._client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, follow_redirects=False)
        try:
            resp = await client.post(f'{self._config.url}/embeddings', json=body, headers=headers)
        finally:
            if self._client is None:
                await client.aclose()
        resp.raise_for_status()
        data = resp.json().get('data')
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError('unexpected embeddings response')
        rows = sorted((d for d in data if isinstance(d, dict)), key=lambda d: d.get('index', 0))
        out = [[float(x) for x in (r.get('embedding') or [])] for r in rows]
        if len(out) != len(texts) or any(not v or not all(math.isfinite(x) for x in v) for v in out):
            raise ValueError('unexpected embeddings response')
        return out


# ---------------------------------------------------------------- vector helpers
def pack(vec: list[float]) -> str:
    return base64.b64encode(array('f', vec).tobytes()).decode('ascii')


def unpack(blob: str) -> array:
    a = array('f')
    a.frombytes(base64.b64decode(blob))
    return a


def cosine(a: Any, b: Any) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    out = dot / (na * nb) if na and nb else 0.0
    return out if math.isfinite(out) else 0.0


async def cosines_async(query_vec: Any, items: list[tuple[str, Any]]) -> dict[str, float]:
    """Score many vectors off the event loop so a big store cannot stall other requests."""
    return await asyncio.to_thread(lambda: {mid: cosine(query_vec, vec) for mid, vec in items})


def memory_text(entry: dict[str, Any]) -> str:
    return f"{entry.get('title') or ''}. {entry.get('summary') or ''}".strip()


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]


def _z_scores(cosines: dict[str, float]) -> dict[str, float]:
    vals = list(cosines.values())
    mean = sum(vals) / len(vals)
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    if std < 1e-9:
        return dict.fromkeys(cosines, 0.0)
    return {k: (v - mean) / std for k, v in cosines.items()}


def meaning_signals(cosines: dict[str, float]) -> dict[str, float]:
    """Per memory: 0..1 meaning strength, and 0 when it is not a meaning match at all.

    Raw cosines depend on the model, so with a pool of memories we judge each one by how far it
    stands above the others (z-score). With a tiny pool we fall back to plain cosine thresholds.
    """
    if not cosines:
        return {}
    if len(cosines) < MIN_POOL:
        return {
            k: min(1.0, max(0.0, (c - SMALL_POOL_ADMIT) / (SMALL_POOL_FULL - SMALL_POOL_ADMIT))) if c >= SMALL_POOL_ADMIT else 0.0
            for k, c in cosines.items()
        }
    z = _z_scores(cosines)
    return {
        k: (min(1.0, (z[k] - Z_ADMIT + 1.0) / (Z_FULL - Z_ADMIT + 1.0)) if z[k] >= Z_ADMIT and cosines[k] >= MIN_COSINE else 0.0)
        for k in cosines
    }


def fuse(
    ranked: list[dict[str, Any]],
    cosines: dict[str, float],
    entries_by_id: dict[str, dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Blend keyword rows (hybrid_rank output) with meaning strength. Pure function, easy to test."""
    sem = meaning_signals(cosines)
    rows: dict[str, dict[str, Any]] = {}
    for r in ranked:
        rows[r['entry'].get('id')] = {**r, '_kw': min(1.0, r['score'] / KW_FULL)}
    for mid, strength in sem.items():
        if strength > 0 and mid not in rows and mid in entries_by_id:
            rows[mid] = {'entry': entries_by_id[mid], 'score': 0.0, 'matchedTerms': [], 'signals': {}, '_kw': 0.0}
    fused: list[dict[str, Any]] = []
    for mid, r in rows.items():
        strength = sem.get(mid, 0.0)
        score = round(W_KEYWORD * r['_kw'] + W_MEANING * strength, 4)
        if score <= 0:
            continue
        signals = dict(r.get('signals') or {})
        if strength:
            signals['meaning'] = round(cosines.get(mid, 0.0), 3)
        fused.append({**{k: v for k, v in r.items() if k != '_kw'}, 'score': score, 'signals': signals})
    fused.sort(key=lambda r: r['score'], reverse=True)
    return fused[:limit]


class VectorStore:
    """Vectors for ONE model (and vector size), kept in the key-value store as {id: 'hash:base64'}."""

    def __init__(self, kv: Any, model: str, dims: int | None = None) -> None:
        self._kv = kv
        self._model = model
        self._dims = dims
        self._vectors: dict[str, tuple[str, Any]] = {}
        self._loaded = False
        self._dirty = False
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            raw = await self._kv.get_value(KEY_EMBEDDINGS)  # if this raises, we stay unloaded and retry next time
            self._loaded = True
            if not isinstance(raw, dict) or raw.get('model') != self._model or raw.get('dims') != self._dims:
                return  # another model or vector size: those vectors are useless, start clean
            for mid, packed in (raw.get('vectors') or {}).items():
                if isinstance(packed, str) and ':' in packed:
                    h, blob = packed.split(':', 1)
                    try:
                        self._vectors[mid] = (h, unpack(blob))
                    except Exception:  # noqa: BLE001 - one bad row must not break search
                        continue

    def get(self, mid: str, text_h: str) -> Any | None:
        row = self._vectors.get(mid)
        return row[1] if row and row[0] == text_h else None

    def has_room(self) -> bool:
        return len(self._vectors) < MAX_VECTORS

    def put(self, mid: str, text_h: str, vec: list[float]) -> None:
        if mid not in self._vectors and not self.has_room():
            return
        self._vectors[mid] = (text_h, array('f', vec))
        self._dirty = True

    def prune(self, live_ids: set[str]) -> None:
        for mid in [m for m in self._vectors if m not in live_ids]:
            del self._vectors[mid]
            self._dirty = True

    async def save(self) -> None:
        if not self._dirty:
            return
        await self._kv.set_value(
            KEY_EMBEDDINGS,
            {
                'model': self._model,
                'dims': self._dims,
                'vectors': {m: f'{h}:{base64.b64encode(v.tobytes()).decode("ascii")}' for m, (h, v) in self._vectors.items()},
            },
        )
        self._dirty = False
