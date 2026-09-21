"""Marginalia -- a DISCOVERY provider, not a bulk one.

Marginalia has no downloadable directory. What it has is an index of the
non-commercial web -- documentation, forums, personal sites -- which is
precisely the population a source registry wants and precisely the population
the big engines rank last. So it is used the other way round: given a topic
that already exists in the registry, ask Marginalia what is out there, and
harvest DOMAINS (not pages) as candidate sources.

That makes it the mechanism for the loop the broker design depends on: the
registry misses, discovery answers, the answer becomes a registered source
that the next query routes to directly.

API. Two endpoints are live:
  legacy  https://api.marginalia.nu/{key}/search/{query}        key in path
  current https://api2.marginalia-search.com/search?query=...   API-Key header

The shared `public` key works on legacy and is usually over its daily limit on
api2. A free non-commercial key is an email to contact@marginalia-search.com.
Results are CC-BY-NC-SA 4.0 -- attribution travels with anything ingested.

Candidates are NOT auto-trusted. `discover` returns them for curation; the
`--accept` flag on `presearch discover` is what writes them to the registry.
"""

import time
from collections import defaultdict
from urllib.parse import quote, urlsplit

import requests

from ..record import (P_CAPABILITY, P_HOME_URL, P_TEMPORAL, Source, Upstream)

NAME = "marginalia"
LEGACY = "https://api.marginalia.nu/{key}/search/{query}"
CURRENT = "https://api2.marginalia-search.com/search"
LICENSE = "CC-BY-NC-SA 4.0"


class MarginaliaProvider:
    kind = "discovery"
    name = NAME

    # The shared key queues behind every other consumer, so a generous read
    # timeout just converts rate limiting into a hang. Fail at 30s instead.
    TIMEOUT = (5, 30)

    def __init__(self, data_dir=None, key="public", use_current=False,
                 session=None, pause=1.0, min_hits=2):
        self.key = key
        self.use_current = use_current
        self.pause = pause
        # A domain seen once is a page; seen repeatedly across a topic's
        # queries it is a SOURCE. This threshold is the whole filter.
        self.min_hits = min_hits
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "tau-presearch/0.1"

    def fetch(self, force=False, progress=None):
        return self.upstream()

    def upstream(self):
        base = CURRENT if self.use_current else LEGACY.split("/{key}")[0]
        return Upstream(provider=NAME, url=base,
                        version="api2" if self.use_current else "legacy")

    def probe(self):
        """Liveness, for validate. Returns result count for a fixed query."""
        try:
            return len(self._search("sqlite internals"))
        except Exception:
            return -1

    def _search(self, query):
        if self.use_current:
            r = self.session.get(CURRENT, params={"query": query},
                                 headers={"API-Key": self.key},
                                 timeout=self.TIMEOUT)
        else:
            url = LEGACY.format(key=self.key, query=quote(query))
            r = self.session.get(url, timeout=self.TIMEOUT)
        if r.status_code in (429, 503):
            raise RuntimeError(
                f"marginalia rate limited (HTTP {r.status_code}). The shared "
                "'public' key is heavily contended; get a free key from "
                "contact@marginalia-search.com and pass --key.")
        r.raise_for_status()
        return (r.json() or {}).get("results", [])

    def discover(self, topic_key, queries=None, limit_domains=25):
        """Harvest candidate source domains for one topic.

        Queries default to the topic's own path segments, which is crude but
        needs no model and is a fine baseline to beat.
        """
        terms = queries or [topic_key.replace("/", " ").replace("_", " ")]
        domains = defaultdict(lambda: {"hits": 0, "title": "", "desc": "",
                                       "quality": 0.0, "pages": []})
        for term in terms:
            try:
                results = self._search(term)
            except RuntimeError as e:
                raise SystemExit(str(e))
            for row in results:
                url = row.get("url") or ""
                host = urlsplit(url).netloc.lower()
                if not host:
                    continue
                d = domains[host]
                d["hits"] += 1
                d["quality"] = max(d["quality"], float(row.get("quality") or 0))
                d["pages"].append(url)
                if not d["title"]:
                    d["title"] = row.get("title") or host
                    d["desc"] = (row.get("description") or "")[:500]
            time.sleep(self.pause)

        keep = [(h, d) for h, d in domains.items() if d["hits"] >= self.min_hits]
        keep.sort(key=lambda kv: (-kv[1]["hits"], -kv[1]["quality"]))

        for host, d in keep[:limit_domains]:
            yield Source(
                url=f"https://{host}/", title=d["title"], description=d["desc"],
                topic_keys=(topic_key,),
                facts={P_HOME_URL: f"https://{host}/",
                       P_CAPABILITY: "identifier", P_TEMPORAL: "static"})

    def topics(self):
        return iter(())

    def sources(self):
        raise SystemExit(
            "marginalia is a discovery provider: use "
            "`presearch discover --topic <key>`, not `load`.")
