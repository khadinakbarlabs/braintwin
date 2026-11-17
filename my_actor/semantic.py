"""Related-word lexicon for meaning-style search without any model download.

HONEST LIMIT: this is a hand-made list of near-synonym groups (plus words learned from the owner's
own memories in ranking.py). It finds "vehicle" when you search "car"; it is not a neural embedding
and will miss concepts the list does not cover.
"""

from __future__ import annotations

GROUPS: tuple[tuple[str, ...], ...] = (
    ('car', 'vehicle', 'auto', 'automobile', 'truck'),
    ('buy', 'purchase', 'order', 'acquire'),
    ('cost', 'price', 'pricing', 'fee', 'charge', 'rate'),
    ('cheap', 'affordable', 'inexpensive', 'budget'),
    ('money', 'cash', 'funds', 'revenue', 'income', 'earnings'),
    ('profit', 'margin', 'gain'),
    ('job', 'work', 'career', 'role', 'position', 'employment'),
    ('company', 'business', 'firm', 'startup', 'employer', 'organization'),
    ('boss', 'manager', 'supervisor', 'lead'),
    ('meeting', 'call', 'sync', 'standup'),
    ('email', 'mail', 'message', 'inbox'),
    ('phone', 'mobile', 'cell', 'smartphone'),
    ('laptop', 'computer', 'pc', 'macbook', 'notebook'),
    ('bug', 'defect', 'issue', 'error', 'problem', 'fault'),
    ('fix', 'repair', 'patch', 'resolve', 'solve'),
    ('fast', 'quick', 'speedy', 'rapid', 'faster'),
    ('slow', 'sluggish', 'laggy'),
    ('big', 'large', 'huge', 'massive'),
    ('small', 'tiny', 'little', 'compact'),
    ('start', 'begin', 'launch', 'kickoff'),
    ('stop', 'halt', 'end', 'cancel', 'quit'),
    ('delete', 'remove', 'erase', 'drop'),
    ('create', 'make', 'build', 'generate'),
    ('show', 'display', 'list', 'view'),
    ('find', 'search', 'locate', 'lookup'),
    ('like', 'prefer', 'favorite', 'favourite', 'enjoy', 'love'),
    ('hate', 'dislike', 'avoid', 'despise'),
    ('food', 'meal', 'dish', 'eat', 'lunch', 'dinner'),
    ('drink', 'beverage', 'coffee', 'tea'),
    ('home', 'house', 'apartment', 'flat', 'residence'),
    ('trip', 'travel', 'journey', 'vacation', 'holiday'),
    ('doctor', 'physician', 'clinic', 'hospital'),
    ('sick', 'ill', 'unwell'),
    ('friend', 'buddy', 'pal', 'colleague'),
    ('child', 'kid', 'son', 'daughter'),
    ('video', 'clip', 'film', 'movie', 'youtube'),
    ('article', 'post', 'blog', 'essay', 'newsletter'),
    ('picture', 'image', 'photo', 'screenshot'),
    ('code', 'script', 'program', 'software', 'source'),
    ('api', 'endpoint', 'interface', 'integration'),
    ('server', 'host', 'backend', 'service'),
    ('database', 'db', 'storage', 'datastore'),
    ('login', 'signin', 'authenticate', 'auth', 'password'),
    ('secret', 'token', 'credential', 'apikey'),
    ('test', 'check', 'verify', 'validate'),
    ('plan', 'roadmap', 'strategy', 'schedule'),
    ('goal', 'target', 'objective', 'aim'),
    ('deadline', 'due', 'duedate'),
    ('remember', 'recall', 'memorize', 'note'),
    ('forget', 'ignore', 'discard'),
    ('customer', 'client', 'user', 'buyer'),
    ('sell', 'sale', 'market', 'promote'),
    ('learn', 'study', 'course', 'tutorial', 'lesson'),
    ('use', 'utilize', 'employ', 'apply'),
    ('change', 'modify', 'update', 'edit', 'alter'),
    ('rule', 'policy', 'guideline', 'standard'),
    ('safe', 'secure', 'protected'),
    ('risk', 'danger', 'threat', 'hazard'),
    ('team', 'group', 'squad', 'crew'),
    ('weekly', 'week', 'sevenday'),
    ('daily', 'day', 'everyday'),
)


def _build() -> dict[str, frozenset[str]]:
    from .ranking import stem  # local import: ranking imports this module

    out: dict[str, set[str]] = {}
    for group in GROUPS:
        stems = {stem(w) for w in group}
        for s in stems:
            out.setdefault(s, set()).update(stems - {s})
    return {k: frozenset(v) for k, v in out.items()}


_RELATED: dict[str, frozenset[str]] | None = None


def related_stems(stem_word: str) -> frozenset[str]:
    """Stemmed near-synonyms of an already-stemmed word (empty if the word is not in the lexicon)."""
    global _RELATED
    if _RELATED is None:
        _RELATED = _build()
    return _RELATED.get(stem_word, frozenset())
