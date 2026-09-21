"""Normalized records every provider emits, plus upstream provenance.

A provider turns some external corpus (a directory dump, a SPARQL endpoint,
a search API) into three things: Topic nodes, Source entries, and an Upstream
stamp saying exactly which version of that corpus they came from.

The Upstream stamp is the point. A registry whose provenance is not checkable
is a registry you cannot trust to route queries, so nothing loads without one
and `presearch validate` re-derives it to detect drift.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

# Predicates the registry writes. Document-valued unless marked literal.
P_COVERS_TOPIC = "covers_topic"      # source  -> topic document
P_LISTED_BY = "listed_by"            # source  -> manifest document
P_PARENT_TOPIC = "parent_topic"      # topic   -> topic document
P_SEARCH_ENDPOINT = "search_endpoint"  # literal: URL template with {q}
P_HOME_URL = "home_url"              # literal
P_SUBREDDIT = "subreddit"            # literal
P_SE_TAG = "stack_exchange_tag"      # literal
P_SOURCE_REPO = "source_repo"        # literal
P_BUG_TRACKER = "bug_tracker"        # literal
P_CAPABILITY = "capability"          # literal: fulltext|tag|identifier|enumerate
P_TEMPORAL = "temporal"              # literal: static|dated|feed
P_ENTRY_COUNT = "entry_count"        # literal xsd:integer

LITERAL_PREDICATES = frozenset({
    P_SEARCH_ENDPOINT, P_HOME_URL, P_SUBREDDIT, P_SE_TAG, P_SOURCE_REPO,
    P_BUG_TRACKER, P_CAPABILITY, P_TEMPORAL, P_ENTRY_COUNT,
})

# usetypes. Held out of ordinary retrieval so registry rows never rank
# against real content -- see JMFTS /capabilities "usetypes held out".
UT_ROOT = "registry:root"
UT_TOPIC = "registry:topic"
UT_SOURCE = "registry:source"
UT_MANIFEST = "registry:manifest"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Upstream:
    """Identity and version of the corpus a load actually read.

    `version` is whatever the upstream offers as a change signal -- an ETag,
    a Last-Modified, a dump date, an API version string. `digest` is a sha256
    over the bytes we parsed when we hold them locally, and "" when we do not
    (a live API query cannot be digested meaningfully).
    """
    provider: str
    url: str
    version: str
    digest: str = ""
    fetched_at: str = field(default_factory=utcnow)

    def to_dict(self):
        return {
            "provider": self.provider, "url": self.url,
            "version": self.version, "digest": self.digest,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(provider=d["provider"], url=d["url"], version=d["version"],
                   digest=d.get("digest", ""), fetched_at=d.get("fetched_at", ""))


@dataclass(frozen=True)
class Topic:
    """A node in a subject taxonomy.

    `key` is the canonical slash path, without a leading root segment:
    "Computers/Algorithms". `parent_key` is None only for a provider root.
    """
    key: str
    title: str
    description: str = ""
    parent_key: str | None = None
    entry_count: int = 0

    @property
    def depth(self):
        return self.key.count("/") + 1

    @staticmethod
    def parent_of(key):
        head, sep, _ = key.rpartition("/")
        return head if sep else None


@dataclass(frozen=True)
class Source:
    """A place that can be searched or read directly.

    `facts` carries literal-valued predicates from LITERAL_PREDICATES.
    `topic_keys` are Topic.key values; the first is treated as primary and
    decides tree placement.
    """
    url: str
    title: str
    description: str = ""
    topic_keys: tuple[str, ...] = ()
    facts: Mapping[str, str] = field(default_factory=dict)

    @property
    def primary_topic(self):
        return self.topic_keys[0] if self.topic_keys else None


@dataclass(frozen=True)
class Manifest:
    """What one load put into JMFTS, so validate can check it later."""
    upstream: Upstream
    root_id: int
    topics_loaded: int
    sources_loaded: int
    selector: str = ""          # the --topic-prefix / --min-entries used
    loaded_at: str = field(default_factory=utcnow)

    def to_dict(self):
        return {
            "upstream": self.upstream.to_dict(), "root_id": self.root_id,
            "topics_loaded": self.topics_loaded,
            "sources_loaded": self.sources_loaded,
            "selector": self.selector, "loaded_at": self.loaded_at,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(upstream=Upstream.from_dict(d["upstream"]),
                   root_id=d["root_id"], topics_loaded=d["topics_loaded"],
                   sources_loaded=d["sources_loaded"],
                   selector=d.get("selector", ""), loaded_at=d.get("loaded_at", ""))
