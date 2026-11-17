"""Pluggable storage for the brain: Apify Actor runtime, Apify REST API, or local SQLite.

BrainStore only needs a key-value store and an append-only audit log, so each backend
implements the two small protocols below. Records are always JSON round-tripped, so a
caller that mutates a value it just read can never corrupt stored state.

Apify imports are lazy: the local SQLite path needs only the standard library.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from .constants import (
    AUDIT_DATASET_NAME,
    BACKEND_ACTOR,
    BACKEND_APIFY,
    BACKEND_SQLITE,
    BACKENDS,
    DEFAULT_SQLITE_PATH,
    ENV_BACKEND,
    ENV_LOCAL,
    ENV_SQLITE_PATH,
    KV_STORE_NAME,
)


class KeyValue(Protocol):
    async def get_value(self, key: str) -> Any: ...
    async def set_value(self, key: str, value: Any) -> None: ...  # None deletes
    async def delete_value(self, key: str) -> None: ...
    async def list_keys(self, prefix: str = '') -> list[str]: ...


class AuditLog(Protocol):
    async def push_data(self, item: dict[str, Any]) -> None: ...
    def iterate_items(self, *, limit: int = 500, desc: bool = False) -> AsyncIterator[dict[str, Any]]: ...


_KEY_RE = re.compile(r"^[A-Za-z0-9!\-_.'()]{1,256}$")


def check_key(key: str) -> str:
    """Raise unless `key` is legal on Apify (the strictest backend): letters, digits and
    ! - _ . ' ( ) only, 1-256 chars. Every backend enforces this so a key that only works
    locally can never ship (a slash in `memory/<id>` once passed every local test and then
    crashed the hosted server on its first write)."""
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError(f"Invalid storage key {key!r}: only a-zA-Z0-9!-_.'() and up to 256 characters are allowed")
    return key


def resolve_backend(explicit: str | None = None, *, default: str = BACKEND_ACTOR) -> str:
    """Explicit arg > SECOND_BRAIN_BACKEND > SECOND_BRAIN_LOCAL=1 > caller default."""
    name = (explicit or os.environ.get(ENV_BACKEND) or '').strip().lower()
    if not name and os.environ.get(ENV_LOCAL, '').strip() == '1':
        name = BACKEND_SQLITE
    name = name or default
    if name not in BACKENDS:
        raise ValueError(f'Unknown backend {name!r}; expected one of {", ".join(BACKENDS)}')
    return name


# ---------------------------------------------------------------- in-memory (tests)


class MemoryKV:
    """Dict-backed store for tests. Every call yields to the loop like real I/O would,
    so concurrency bugs (lost updates between awaits) show up instead of hiding."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get_value(self, key: str) -> Any:
        await asyncio.sleep(0)
        raw = self._data.get(key)
        return None if raw is None else json.loads(raw)

    async def set_value(self, key: str, value: Any) -> None:
        await asyncio.sleep(0)
        check_key(key)
        if value is None:
            self._data.pop(key, None)
        else:
            self._data[key] = json.dumps(value, ensure_ascii=False)

    async def delete_value(self, key: str) -> None:
        await asyncio.sleep(0)
        self._data.pop(key, None)

    async def list_keys(self, prefix: str = '') -> list[str]:
        await asyncio.sleep(0)
        return sorted(k for k in self._data if k.startswith(prefix))


class MemoryAudit:
    def __init__(self) -> None:
        self._items: list[str] = []

    async def push_data(self, item: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        self._items.append(json.dumps(item, ensure_ascii=False))

    async def iterate_items(self, *, limit: int = 500, desc: bool = False) -> AsyncIterator[dict[str, Any]]:
        rows = list(reversed(self._items)) if desc else list(self._items)
        for raw in rows[:limit]:
            yield json.loads(raw)


# ---------------------------------------------------------------- local SQLite


def default_sqlite_path() -> Path:
    return Path(os.environ.get(ENV_SQLITE_PATH) or DEFAULT_SQLITE_PATH).expanduser()


def _connect(path: Path) -> sqlite3.Connection:
    on_disk = str(path) != ':memory:'
    if on_disk:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Personal memory: every file SQLite creates (db, -wal, -shm) must be owner-only. WAL
    # sidecar files carry note text too, so a umask during creation beats chmod-after.
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT NOT NULL)')
    finally:
        os.umask(old_umask)
    if on_disk:
        for suffix in ('', '-wal', '-shm'):  # also tighten files created by an older version
            with contextlib.suppress(OSError):
                os.chmod(f'{path}{suffix}', 0o600)
    return conn


class SqliteKV:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def get_value(self, key: str) -> Any:
        row = self._conn.execute('SELECT value FROM kv WHERE key = ?', (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    async def set_value(self, key: str, value: Any) -> None:
        check_key(key)
        if value is None:
            await self.delete_value(key)
            return
        self._conn.execute(
            'INSERT INTO kv (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, json.dumps(value, ensure_ascii=False)),
        )

    async def delete_value(self, key: str) -> None:
        self._conn.execute('DELETE FROM kv WHERE key = ?', (key,))

    async def list_keys(self, prefix: str = '') -> list[str]:
        # substr compare, not LIKE: '_' and '%' in prefixes must stay literal.
        rows = self._conn.execute(
            'SELECT key FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key', (len(prefix), prefix)
        ).fetchall()
        return [r[0] for r in rows]


class SqliteAudit:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def push_data(self, item: dict[str, Any]) -> None:
        self._conn.execute('INSERT INTO audit (item) VALUES (?)', (json.dumps(item, ensure_ascii=False),))

    async def iterate_items(self, *, limit: int = 500, desc: bool = False) -> AsyncIterator[dict[str, Any]]:
        order = 'DESC' if desc else 'ASC'
        # `order` is one of two fixed literals above, never caller input.
        rows = self._conn.execute(f'SELECT item FROM audit ORDER BY id {order} LIMIT ?', (int(limit),)).fetchall()  # noqa: S608
        for (raw,) in rows:
            yield json.loads(raw)


def open_sqlite(path: str | Path | None = None) -> tuple[SqliteKV, SqliteAudit]:
    conn = _connect(Path(path).expanduser() if path else default_sqlite_path())
    return SqliteKV(conn), SqliteAudit(conn)


# ---------------------------------------------------------------- Apify REST API


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


class ApiKV:
    """Named Apify key-value store over the REST API (no Actor runtime needed)."""

    def __init__(self, client: Any, store_id: str) -> None:
        self._kv = client.key_value_store(store_id)

    async def get_value(self, key: str) -> Any:
        rec = await self._kv.get_record(key)
        return None if rec is None else _field(rec, 'value')

    async def set_value(self, key: str, value: Any) -> None:
        if value is None:
            await self.delete_value(key)
            return
        await self._kv.set_record(
            key, json.dumps(value, ensure_ascii=False), content_type='application/json; charset=utf-8'
        )

    async def delete_value(self, key: str) -> None:
        await self._kv.delete_record(key)

    async def list_keys(self, prefix: str = '') -> list[str]:
        keys: list[str] = []
        start: str | None = None
        while True:
            page = await self._kv.list_keys(prefix=prefix or None, exclusive_start_key=start, limit=1000)
            keys.extend(str(_field(k, 'key')) for k in (_field(page, 'items') or []))
            nxt = _field(page, 'next_exclusive_start_key')
            if not _field(page, 'is_truncated') or not nxt:
                return keys
            start = nxt


class ApiAudit:
    def __init__(self, client: Any, dataset_id: str) -> None:
        self._ds = client.dataset(dataset_id)

    async def push_data(self, item: dict[str, Any]) -> None:
        await self._ds.push_items([item])

    async def iterate_items(self, *, limit: int = 500, desc: bool = False) -> AsyncIterator[dict[str, Any]]:
        async for item in self._ds.iterate_items(limit=limit, desc=desc):
            yield dict(item)


async def open_apify_api(token: str | None = None) -> tuple[ApiKV, ApiAudit]:
    from apify_client import ApifyClientAsync  # lazy: local SQLite path never imports it

    token = token or os.environ.get('APIFY_TOKEN')
    if not token:
        raise RuntimeError('APIFY_TOKEN is required for the apify backend')
    client = ApifyClientAsync(token)
    kv_res = await client.key_value_stores().get_or_create(name=KV_STORE_NAME)
    ds_res = await client.datasets().get_or_create(name=AUDIT_DATASET_NAME)
    return ApiKV(client, str(_field(kv_res, 'id'))), ApiAudit(client, str(_field(ds_res, 'id')))


# ---------------------------------------------------------------- Apify Actor runtime


class ActorKV:
    """Wraps the Actor SDK store. Key listing goes through the REST API when running on Apify."""

    def __init__(self, store: Any, api: ApiKV | None) -> None:
        self._store = store
        self._api = api

    async def get_value(self, key: str) -> Any:
        return await self._store.get_value(key)

    async def set_value(self, key: str, value: Any) -> None:
        await self._store.set_value(key, value)  # None deletes the record

    async def delete_value(self, key: str) -> None:
        await self._store.set_value(key, None)

    async def list_keys(self, prefix: str = '') -> list[str]:
        if self._api is not None:
            return await self._api.list_keys(prefix)
        keys: list[str] = []
        async for meta in self._store.iterate_keys():
            name = _field(meta, 'key')
            if name and str(name).startswith(prefix):
                keys.append(str(name))
        return sorted(keys)


class ActorAudit:
    def __init__(self, dataset: Any) -> None:
        self._ds = dataset

    async def push_data(self, item: dict[str, Any]) -> None:
        await self._ds.push_data(item)

    async def iterate_items(self, *, limit: int = 500, desc: bool = False) -> AsyncIterator[dict[str, Any]]:
        async for item in self._ds.iterate_items(limit=limit, desc=desc):
            yield dict(item)


async def open_actor_runtime() -> tuple[ActorKV, ActorAudit]:
    from apify import Actor  # lazy

    store = await Actor.open_key_value_store(name=KV_STORE_NAME)
    dataset = await Actor.open_dataset(name=AUDIT_DATASET_NAME)
    api: ApiKV | None = None
    if Actor.configuration.is_at_home:
        client = Actor.new_client()
        api = ApiKV(client, str(store.id))
    return ActorKV(store, api), ActorAudit(dataset)


async def open_storage(backend: str | None = None, *, default: str = BACKEND_ACTOR) -> tuple[Any, Any, str]:
    """Return (kv, audit, backend_name)."""
    name = resolve_backend(backend, default=default)
    if name == BACKEND_SQLITE:
        kv, audit = open_sqlite()
        return kv, audit, name
    if name == BACKEND_APIFY:
        kv, audit = await open_apify_api()
        return kv, audit, name
    kv, audit = await open_actor_runtime()
    return kv, audit, name
