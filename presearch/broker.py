"""The broker: query in, a routing plan out.

This is resource selection, the middle problem of federated search. It does
not fetch anything. It answers "given this query, which registered sources
should be asked, in what order, and why" -- and it answers from the registry
alone, with no network call, so routing decisions are deterministic and
testable offline.

The plan is TIERED, not a flat ranking. Tier 0 is the deepest topic match,
the last tier is always the open web. Flattening the tiers would throw away
the escalation structure, which is the only thing that distinguishes this
from metasearch.
"""

import json
from dataclasses import asdict, dataclass, field

from .record import UT_SOURCE, UT_TOPIC
from .registry import INDEX_NAME

OPEN_WEB = "open-web"
MAX_SEARCH_LIMIT = 100  # SearchRequest.limit is 1..100


@dataclass
class Selection:
    source_id: int
    title: str
    url: str
    score: float
    topic_key: str
    depth: int
    why: str
    endpoint: str = ""


@dataclass
class Tier:
    rank: int
    rationale: str
    selections: list = field(default_factory=list)


@dataclass
class Plan:
    query: str
    tiers: list = field(default_factory=list)
    provenance: list = field(default_factory=list)
    validated: bool = False
    warnings: list = field(default_factory=list)

    def to_json(self, indent=2):
        return json.dumps(asdict(self), indent=indent)

    def render(self):
        out = [f"query: {self.query}"]
        if self.warnings:
            for w in self.warnings:
                out.append(f"  ! {w}")
        for tier in self.tiers:
            out.append(f"\ntier {tier.rank}: {tier.rationale}")
            if not tier.selections:
                out.append("  (nothing registered)")
            for s in tier.selections:
                out.append(f"  {s.score:7.3f}  {s.title}")
                out.append(f"           {s.url}")
                out.append(f"           topic={s.topic_key} depth={s.depth} "
                           f"-- {s.why}")
        if self.provenance:
            out.append("\nresting on:")
            for p in self.provenance:
                out.append(f"  {p}")
        return "\n".join(out)


def _rows(payload):
    if not isinstance(payload, dict):
        return []
    return payload.get("results", []) or []


def _topic_of(registry, doc_id, cache):
    """Walk to the nearest registry:topic ancestor. Returns (key, depth)."""
    if doc_id in cache:
        return cache[doc_id]
    anc = registry.j.ancestors(doc_id) or []
    rows = anc.get("results", anc) if isinstance(anc, dict) else anc
    key, depth = "", 0
    for row in rows:
        doc = row.get("document", row)
        if doc.get("usetype") == UT_TOPIC and doc.get("title"):
            key = doc["title"]
            depth = key.count("/") + 1
    cache[doc_id] = (key, depth)
    return key, depth


def plan(registry, query, limit=10, method="bm25", validate_first=True,
         data_dir=".", tiers=3):
    """Build a routing plan for one query."""
    p = Plan(query=query)

    if validate_first:
        from . import validate as validate_mod
        checks, ok = validate_mod.run(registry, data_dir, sample=0)
        p.validated = ok
        p.provenance = [c.line() for c in checks]
        if not ok:
            p.warnings.append(
                "registry validation failed -- results below rest on an index "
                "that has drifted from its upstream. Reload, or pass "
                "--no-validate to proceed anyway.")

    # Topic hits tell us WHERE in the taxonomy the query lands; source hits
    # tell us WHAT to ask. Both come from the same lexical index.
    # `limit` is the caller's and is passed through as-is (the CLI refuses
    # values over 100, and Jmfts.search raises rather than truncate). Only the
    # source query's internal 3x over-fetch is capped at the server's 100:
    # it is headroom for tier selection, not something the caller asked for,
    # so when limit*3 > 100 the pool is just smaller and the caller still
    # gets up to `limit` per tier from it.
    topic_hits = _rows(registry.j.search(
        query, method=method, limit=limit,
        usetype=UT_TOPIC, parent_id=registry.root_id, index_name=INDEX_NAME))
    source_hits = _rows(registry.j.search(
        query, method=method, limit=min(limit * 3, MAX_SEARCH_LIMIT),
        usetype=UT_SOURCE, parent_id=registry.root_id, index_name=INDEX_NAME))

    best_topics = []
    for row in topic_hits:
        doc = row.get("document", row)
        if doc.get("title"):
            best_topics.append((doc["title"], float(row.get("score") or 0)))

    cache = {}
    selections = []
    for row in source_hits:
        doc = row.get("document", row)
        content = doc.get("content") or ""
        url = ""
        for token in content.split():
            if token.startswith(("http://", "https://")):
                url = token
        key, depth = _topic_of(registry, doc["id"], cache)
        score = float(row.get("score") or 0)

        why = "lexical match on the source's editorial description"
        for tkey, tscore in best_topics:
            if key == tkey or key.startswith(tkey + "/"):
                score += tscore * 0.5
                why = f"sits under matched topic {tkey!r}"
                break

        selections.append(Selection(
            source_id=doc["id"], title=doc.get("title") or url, url=url,
            score=score, topic_key=key, depth=depth, why=why))

    # Specificity decides the tier: deepest topic first. This is longest-prefix
    # match, the same rule a routing table uses.
    selections.sort(key=lambda s: (-s.depth, -s.score))
    if selections:
        depths = sorted({s.depth for s in selections}, reverse=True)
        bands = depths[:max(tiers - 1, 1)]
        for rank, d in enumerate(bands):
            picked = [s for s in selections if s.depth == d][:limit]
            p.tiers.append(Tier(
                rank=rank,
                rationale=f"registered sources at topic depth {d} "
                          f"(most specific first)",
                selections=picked))
    else:
        p.tiers.append(Tier(rank=0, rationale="no registered source matched",
                            selections=[]))

    p.tiers.append(Tier(
        rank=len(p.tiers),
        rationale=("open web via tau-search -- the default route. Firing this "
                   "tier is a signal to run `presearch discover` for this "
                   "topic, not merely a miss."),
        selections=[Selection(source_id=-1, title=OPEN_WEB, url="",
                              score=0.0, topic_key="", depth=0,
                              why="fallback of last resort")]))
    return p
