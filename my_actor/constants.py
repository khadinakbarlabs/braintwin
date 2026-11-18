"""Fixed store names, key prefixes and tunables for the personal Braintwin store."""

VERSION = '0.30.0'

KV_STORE_NAME = 'braintwin-kv'
AUDIT_DATASET_NAME = 'braintwin-audit'

# Apify key-value store keys allow only letters, digits and ! - _ . ' ( ) (max 256): NO slashes.
# Local stores accept slashes, which hid this until a live smoke test; storage.check_key now
# enforces the rule on every backend.
KEY_SOURCES = 'config.sources'
KEY_INBOX = '__inbox'
KEY_MANIFEST = '__manifest'
KEY_SEARCH_INDEX = '__search_index'
KEY_SYNC_STATE = '__sync_state'
KEY_INDEX_STAMP = '__index_stamp'  # tiny marker: lets a warm server skip re-downloading the big index
KEY_POLICY = 'config.policy'  # owner-set auto-approval rules (CLI only)
KEY_AUTO_STATS = '__auto_stats'  # per-day count of automatic approvals
KEY_PROFILE = 'profile'
MEMORY_PREFIX = 'memory.'
INGEST_JOB_PREFIX = 'ingest_job.'  # one KV key per job → no shared-blob write races

# Thin search cards (title+summary+tags) — not full bodies.
SEARCH_INDEX_VERSION = 1
SUMMARY_CHARS = 500
INDEX_TOKEN_CHARS = 2_000  # tokenize at most this much text for BM25 docs

STATUSES = ('working', 'proposed', 'approved', 'decayed', 'rejected')
DEFAULT_SEARCH_STATUSES = ('approved',)

TOOL_CALL_EVENT = 'tool-call'

# PPE: charge only mutations — never recall tools.
CHARGEABLE_TOOLS = frozenset(
    {
        'memory_propose',
        'inbox_approve',
        'inbox_reject',
        'memory_forget',
        'memory_capture',
    }
)
FREE_READ_TOOLS = frozenset(
    {
        'inbox_list',
        'memory_search',
        'memory_explain',
        'brain_health',
        'recent_followed_content',
        'memory_context',
    }
)

# Storage backends
BACKEND_ACTOR = 'actor'  # Apify Actor runtime (Standby / normal run)
BACKEND_APIFY = 'apify'  # Apify REST API from a local process (needs APIFY_TOKEN)
BACKEND_SQLITE = 'sqlite'  # local file, $0, shared by every agent on this machine
BACKENDS = (BACKEND_ACTOR, BACKEND_APIFY, BACKEND_SQLITE)
ENV_BACKEND = 'BRAINTWIN_BACKEND'
ENV_SQLITE_PATH = 'BRAINTWIN_SQLITE'
ENV_LOCAL = 'BRAINTWIN_LOCAL'  # 1 → force sqlite
DEFAULT_SQLITE_PATH = '~/.local/share/braintwin/brain.sqlite'

# Ingest
INGEST_INLINE = 'inline'  # fetch during the tool call (local stdio: no hot-path cost)
INGEST_QUEUE = 'queue'  # enqueue only; a worker fetches (Standby: keep the server idle)
ENV_INGEST_MODE = 'BRAINTWIN_INGEST'
MAX_QUEUE_JOBS = 200
MAX_JOB_ATTEMPTS = 3

# Owned-Actor scrape bridge (human-triggered only; never exposed as an MCP tool)
ENV_OWNED_ACTOR_OWNER = 'BRAINTWIN_OWNED_ACTOR_OWNER'
DEFAULT_OWNED_ACTOR_OWNER = 'khadinakbar'
MAX_SCRAPE_COST_USD = 5.0
MAX_SCRAPE_ITEMS = 50

# Sync / import
MAX_SYNC_WRITES = 200
MAX_IMPORT_CHARS = 5_000_000  # refuse absurdly large import_json before parsing it

# Stored-field bounds (untrusted agent input must not bloat the store)
MAX_URL_CHARS = 2_000
MAX_TAGS = 40
MAX_TAG_CHARS = 80
MAX_CITATIONS = 30
MAX_CITATION_VALUE_CHARS = 1_000
MAX_SOURCES = 500

# Paid scraping: total of per-job caps one drain may commit to
MAX_DRAIN_BUDGET_USD = 10.0

# Who may approve memories. "agent" (default): the connected agent can call inbox_approve /
# approve=true. "human": those are refused server-side; only the CLI can approve.
# "auto": agents may propose; the server approves only proposals that pass the owner's policy.
ENV_APPROVAL = 'BRAINTWIN_APPROVAL'
APPROVAL_AGENT = 'agent'
APPROVAL_HUMAN = 'human'
APPROVAL_AUTO = 'auto'  # the server approves by the owner's policy (see policy.py)
