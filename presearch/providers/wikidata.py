"""Wikidata -- the source-handle registry.

Curlie answers "what sites are about topic X". Wikidata answers a different
and more actionable question: "for thing X, where is its canonical home, its
subreddit, its Stack Exchange tag, its repository, its bug tracker". Those are
handles you can query directly, which is exactly what a broker wants to route
to.

  P856   official website        P1324  source code repository
  P3984  subreddit               P1401  bug tracker
  P1482  Stack Exchange tag      P31    instance of (used as a topic hint)

We pull only entities carrying at least one DIRECTLY QUERYABLE handle --
a subreddit, an SE tag, or a repo -- because a bare homepage is what Curlie
already gives us and adds nothing a broker can act on.

Two caveats worth keeping in view:

  * There is no dump digest for a live SPARQL endpoint. Drift for this
    provider is detected by result-count delta (see probe()), not by hash.
  * Wikidata's class graph does not cross-walk cleanly onto Curlie's path
    taxonomy. Topic keys here are synthesized under "Wikidata/<class label>";
    reconciling the two hierarchies is an open problem, not a solved one.
"""

import hashlib
import time

import requests

from ..record import (P_CAPABILITY, P_HOME_URL, P_SE_TAG, P_SEARCH_ENDPOINT,
                      P_SOURCE_REPO, P_SUBREDDIT, P_BUG_TRACKER, P_TEMPORAL,
                      Source, Topic, Upstream)

NAME = "wikidata"
ENDPOINT = "https://query.wikidata.org/sparql"

# Wikidata asks for a descriptive agent with contact info; a generic browser
# UA gets throttled or blocked.
UA = ("tau-presearch/0.1 (federated-search registry builder; "
      "https://github.com/ - contact via repository)")

QUERY = """
SELECT ?item ?itemLabel ?itemDescription ?site ?subreddit ?setag ?repo ?tracker ?clsLabel
WHERE {
  { ?item wdt:P3984 ?subreddit. }
  UNION
  { ?item wdt:P1482 ?setag. }
  UNION
  { ?item wdt:P1324 ?repo. }
  OPTIONAL { ?item wdt:P856  ?site. }
  OPTIONAL { ?item wdt:P3984 ?subreddit. }
  OPTIONAL { ?item wdt:P1482 ?setag. }
  OPTIONAL { ?item wdt:P1324 ?repo. }
  OPTIONAL { ?item wdt:P1401 ?tracker. }
  OPTIONAL { ?item wdt:P31   ?cls. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %(limit)d OFFSET %(offset)d
"""

COUNT_QUERY = """
SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE {
  { ?item wdt:P3984 []. } UNION { ?item wdt:P1482 []. } UNION { ?item wdt:P1324 []. }
}
"""

# How to actually search each handle type, as a URL template the broker fills.
SE_SEARCH = "https://stackoverflow.com/questions/tagged/{tag}?tab=votes"
REDDIT_SEARCH = "https://www.reddit.com/r/{sub}/search/?q={q}&restrict_sr=1"


class WikidataProvider:
    kind = "bulk"
    name = NAME

    def __init__(self, data_dir, min_entries=0, topic_prefix=None,
                 max_depth=None, session=None, page_size=5000, max_rows=0,
                 pause=1.0):
        self.page_size = page_size
        self.max_rows = max_rows
        self.pause = pause
        self.topic_prefix = topic_prefix
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA,
                                     "Accept": "application/sparql-results+json"})
        self._rows = None
        self._count = None

    # ---- upstream --------------------------------------------------------

    def fetch(self, force=False, progress=None):
        """No download step; the endpoint is queried at load time. We probe
        the count so the manifest records a comparable number."""
        self._count = self.probe()
        if progress:
            progress(self._count, self._count)
        return self.upstream()

    def upstream(self):
        return Upstream(
            provider=NAME, url=ENDPOINT,
            version=f"count={self._count if self._count is not None else '?'}",
            digest=hashlib.sha256(QUERY.encode()).hexdigest()[:16])

    def probe(self):
        """Distinct entities matching the registry query. The drift signal."""
        data = self._sparql(COUNT_QUERY)
        rows = data["results"]["bindings"]
        return int(rows[0]["n"]["value"]) if rows else 0

    def _sparql(self, query):
        for attempt in range(3):
            r = self.session.get(ENDPOINT, params={"query": query},
                                 timeout=(10, 180))
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise SystemExit("wikidata: rate limited after 3 attempts")

    # ---- parsing ---------------------------------------------------------

    def _fetch_rows(self):
        if self._rows is not None:
            return self._rows
        merged = {}
        offset = 0
        while True:
            data = self._sparql(QUERY % {"limit": self.page_size, "offset": offset})
            rows = data["results"]["bindings"]
            if not rows:
                break
            for row in rows:
                qid = row["item"]["value"].rsplit("/", 1)[-1]
                rec = merged.setdefault(qid, {
                    "qid": qid,
                    "label": row.get("itemLabel", {}).get("value", qid),
                    "description": row.get("itemDescription", {}).get("value", ""),
                    "classes": set(),
                })
                for field in ("site", "subreddit", "setag", "repo", "tracker"):
                    if field in row and field not in rec:
                        rec[field] = row[field]["value"]
                if "clsLabel" in row:
                    rec["classes"].add(row["clsLabel"]["value"])
            offset += self.page_size
            if self.max_rows and offset >= self.max_rows:
                break
            time.sleep(self.pause)
        self._rows = list(merged.values())
        return self._rows

    def _topic_key(self, rec):
        cls = sorted(rec["classes"])[0] if rec["classes"] else "Uncategorized"
        cls = cls.replace("/", "-").strip()
        return f"Wikidata/{cls}"

    def topics(self):
        keys = {self._topic_key(r) for r in self._fetch_rows()}
        yield Topic(key="Wikidata", title="Wikidata",
                    description="Entities carrying directly-queryable source "
                                "handles: subreddits, Stack Exchange tags, "
                                "repositories.", parent_key=None)
        for key in sorted(keys):
            yield Topic(key=key, title=key.split("/", 1)[-1], parent_key="Wikidata")

    def sources(self):
        for rec in self._fetch_rows():
            facts = {P_TEMPORAL: "dated"}
            caps = set()
            url = rec.get("site") or ""

            if rec.get("setag"):
                facts[P_SE_TAG] = rec["setag"]
                facts[P_SEARCH_ENDPOINT] = SE_SEARCH.format(tag=rec["setag"])
                caps.add("tag")
                url = url or facts[P_SEARCH_ENDPOINT]
            if rec.get("subreddit"):
                facts[P_SUBREDDIT] = rec["subreddit"]
                facts.setdefault(
                    P_SEARCH_ENDPOINT,
                    REDDIT_SEARCH.replace("{sub}", rec["subreddit"]))
                caps.add("tag")
                url = url or f"https://www.reddit.com/r/{rec['subreddit']}/"
            if rec.get("repo"):
                facts[P_SOURCE_REPO] = rec["repo"]
                caps.add("identifier")
                url = url or rec["repo"]
            if rec.get("tracker"):
                facts[P_BUG_TRACKER] = rec["tracker"]
            if rec.get("site"):
                facts[P_HOME_URL] = rec["site"]

            if not url:
                continue
            facts[P_CAPABILITY] = ",".join(sorted(caps)) or "identifier"

            yield Source(url=url, title=rec["label"],
                         description=rec["description"],
                         topic_keys=(self._topic_key(rec),), facts=facts)
