"""Per-user settings kept in the user's OWN storage (never in the Actor's environment).

Why: on Apify every user runs the same Actor build, so an environment variable is shared by all of
them. A dashboard key or an embeddings API key set that way would be the publisher's, not the
user's. Here each user's settings live in their own key-value store:

  config.dashboard_key   salted hash of the key that gates dashboard changes (the key itself is never stored)
  config.embeddings      the user's own meaning-search settings (their API address, model and key)

Environment variables are still honoured when the server runs OFF the Apify platform (local or
self-hosted), where the environment belongs to the one person running it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

from .brand import env_value
from .embeddings import EmbeddingConfig, embedding_config

KEY_DASHBOARD_KEY = 'config.dashboard_key'
KEY_EMBEDDINGS = 'config.embeddings'
KEY_EXTRACTOR = 'config.extractor'
PBKDF2_ROUNDS = 60_000
MIN_KEY_CHARS = 8
MAX_KEY_CHARS = 200
MAX_FAILS = 5  # wrong keys in a row before a short lockout
LOCKOUT_S = 30.0
RELOAD_S = 60.0  # re-read the stored key this often, so a reset in Apify Console takes effect


def on_platform() -> bool:
    """True when running on Apify, where the environment is shared by every user of the Actor."""
    return (os.environ.get('APIFY_IS_AT_HOME') or '').strip().lower() in ('1', 'true')


def _derive(key: str, salt: bytes, rounds: int) -> bytes:
    return hashlib.pbkdf2_hmac('sha256', key.encode('utf-8'), salt, rounds)


def make_key_record(key: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    return {'salt': salt.hex(), 'hash': _derive(key, salt, PBKDF2_ROUNDS).hex(), 'rounds': PBKDF2_ROUNDS, 'setAt': int(time.time())}


def valid_key_record(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get('salt'), str)
        and isinstance(record.get('hash'), str)
        and isinstance(record.get('rounds'), int)
        and 1_000 <= record['rounds'] <= 2_000_000
    )


def check_key(key: str, record: dict[str, Any]) -> bool:
    try:
        expected = bytes.fromhex(record['hash'])
        got = _derive(key, bytes.fromhex(record['salt']), int(record['rounds']))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def key_problem(key: Any) -> str | None:
    """Plain-words reason a new key is not acceptable, or None when it is fine."""
    if not isinstance(key, str) or len(key.strip()) < MIN_KEY_CHARS:
        return f'Use at least {MIN_KEY_CHARS} characters.'
    if len(key) > MAX_KEY_CHARS:
        return f'Use at most {MAX_KEY_CHARS} characters.'
    if key != key.strip() or not all(32 <= ord(ch) < 127 for ch in key):
        return 'Use plain letters, digits and symbols, with no space at the start or end.'
    return None


class KeyGate:
    """Checks the dashboard key for one user's server. Remembers the last good key to stay fast."""

    def __init__(self, env_key: str = '') -> None:
        self._env_key = env_key  # only set off-platform
        self._record: dict[str, Any] | None = None
        self._loaded = False
        self._loaded_at = 0.0
        self._corrupt = False  # a damaged record locks changes (fail closed) until the owner deletes it
        self._good_digest: bytes | None = None
        self._fails = 0
        self._locked_until = 0.0

    async def load(self, kv: Any) -> None:
        if self._loaded and time.monotonic() - self._loaded_at < RELOAD_S:
            return
        raw = await kv.get_value(KEY_DASHBOARD_KEY)
        record = raw if valid_key_record(raw) else None
        if record != self._record:
            self._good_digest = None
        self._record = record
        self._corrupt = raw is not None and record is None
        self._loaded = True
        self._loaded_at = time.monotonic()

    @property
    def required(self) -> bool:
        return bool(self._env_key) or self._record is not None or self._corrupt

    @property
    def user_set(self) -> bool:
        return self._record is not None

    def locked(self) -> bool:
        return time.monotonic() < self._locked_until

    def verify(self, supplied: str) -> bool:
        if self.locked():
            return False
        ok = False
        if self._record is not None:
            digest = hashlib.sha256(supplied.encode('utf-8')).digest()
            if self._good_digest is not None and hmac.compare_digest(digest, self._good_digest):
                ok = True
            elif check_key(supplied, self._record):
                ok = True
                self._good_digest = digest
        elif self._env_key:
            ok = hmac.compare_digest(supplied.encode(), self._env_key.encode())
        if ok:
            self._fails = 0
        else:
            self._fails += 1
            if self._fails >= MAX_FAILS:
                self._fails = 0
                self._locked_until = time.monotonic() + LOCKOUT_S
        return ok

    async def set(self, kv: Any, new_key: str) -> None:
        record = make_key_record(new_key)
        await kv.set_value(KEY_DASHBOARD_KEY, record)
        self._record = record
        self._corrupt = False
        self._good_digest = hashlib.sha256(new_key.encode('utf-8')).digest()
        self._loaded = True
        self._loaded_at = time.monotonic()

    async def clear(self, kv: Any) -> None:
        await kv.set_value(KEY_DASHBOARD_KEY, None)
        self._record = None
        self._corrupt = False
        self._good_digest = None
        self._loaded = True
        self._loaded_at = time.monotonic()


def embeddings_from_record(record: Any) -> EmbeddingConfig | None:
    """Validate a stored settings record with the same rules as the environment form."""
    if not isinstance(record, dict):
        return None
    return embedding_config(
        {
            'BRAINTWIN_EMBEDDINGS_URL': str(record.get('url') or ''),
            'BRAINTWIN_EMBEDDINGS_MODEL': str(record.get('model') or ''),
            'BRAINTWIN_EMBEDDINGS_KEY': str(record.get('key') or ''),
            'BRAINTWIN_EMBEDDINGS_DIMS': str(record.get('dims') or ''),
        }
    )


async def address_is_internal(url: str) -> bool:
    """On Apify, refuse an embeddings address that points at a private or internal machine.

    Off the platform the person running the server may well use a model on their own computer.
    """
    if not on_platform():
        return False
    import asyncio
    from urllib.parse import urlparse

    from .ingest import _host_is_blocked

    return await asyncio.to_thread(_host_is_blocked, urlparse(url).hostname or '')


async def load_embeddings(kv: Any) -> EmbeddingConfig | None:
    """The user's stored settings win. The environment is used only off the Apify platform."""
    cfg = embeddings_from_record(await kv.get_value(KEY_EMBEDDINGS))
    if cfg is not None:
        return None if await address_is_internal(cfg.url) else cfg
    return None if on_platform() else embedding_config()


async def load_extractor(kv: Any) -> Any:
    """The user's own smart-capture settings, or None. There is no environment form: it is per user only."""
    from .extractor import extractor_config

    cfg = extractor_config(await kv.get_value(KEY_EXTRACTOR))
    if cfg is None or await address_is_internal(cfg.url):
        return None
    return cfg


def env_dashboard_key() -> str:
    """Environment key, ignored on the Apify platform (it would be shared by every user)."""
    if on_platform():
        return ''
    return (env_value('BRAINTWIN_DASHBOARD_KEY') or '').strip()
