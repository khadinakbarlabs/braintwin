"""Local stdio MCP entry point — one brain shared by every agent on this machine.

Run:  python -m my_actor.stdio
Each agent (Cursor, Claude, Codex, OpenCode…) launches its own copy of this process; they
all read and write the same store, so memory is shared with no always-on server and no cost.

Storage defaults to a local SQLite file (~/.local/share/braintwin/brain.sqlite, owner-only). Set
BRAINTWIN_BACKEND=apify (plus APIFY_TOKEN) to share the same brain across machines through
your own Apify named storages instead. Billing is a no-op here: local use is free.

stdout carries the MCP protocol, so this module must never print to it.
"""

from __future__ import annotations

import logging
import sys

from .brain import BrainStore, get_brain
from .constants import APPROVAL_AUTO, BACKEND_SQLITE, INGEST_INLINE
from .tools import build_server, resolve_approval_mode, resolve_ingest_mode, resolve_toolset


async def _brain() -> BrainStore:
    return await get_brain(default_backend=BACKEND_SQLITE)


async def _no_charge() -> None:
    return None


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    server = build_server(
        get_brain=_brain,
        charge=_no_charge,
        ingest_mode=resolve_ingest_mode(INGEST_INLINE),
        approval=resolve_approval_mode(default=APPROVAL_AUTO),  # local: your policy approves safe notes
        toolset=resolve_toolset(),
    )
    server.run(transport='stdio', show_banner=False)


if __name__ == '__main__':
    main()
