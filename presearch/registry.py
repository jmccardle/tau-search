"""Loading normalized records into a JMFTS appliance.

Shape inside JMFTS:

    tau-presearch                      registry:root
      Manifests                        registry:root
        curlie @ 2026-02-02            registry:manifest   (provenance, JSON)
        wikidata @ count=48211         registry:manifest
      Topics                           registry:root
        Computers                      registry:topic
          Computers/Algorithms         registry:topic
            <source>                   registry:source
            <source>                   registry:source

Topics are keyed by path and shared across providers, so Curlie and DMOZ land
in one tree rather than two parallel ones. Sources hang under their primary
topic; additional topics become covers_topic triples.

Everything is loaded with auto_embed=False. The registry is a LEXICAL index:
matching a query to a category label is bm25 work, and embedding 2.7M
directory stubs would dominate the appliance for no measured gain.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from .jmfts import Jmfts, JmftsError
from .record import (LITERAL_PREDICATES, Manifest, P_COVERS_TOPIC, P_LISTED_BY,
                     P_PARENT_TOPIC, UT_MANIFEST, UT_ROOT, UT_SOURCE, UT_TOPIC,
                     utcnow)

ROOT_TITLE = "tau-presearch"
MANIFESTS_TITLE = "Manifests"
TOPICS_TITLE = "Topics"
INDEX_NAME = "presearch"


def _doc_id(created):
    if isinstance(created, dict):
        return created.get("id") or (created.get("document") or {}).get("id")
    return created


class Registry:
    def __init__(self, client: Jmfts, verbose=True):
        self.j = client
        self.verbose = verbose
        self.root_id = None
        self.manifests_id = None
        self.topics_id = None
        self._topic_ids = {}

    def log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    # ---- skeleton --------------------------------------------------------

    def ensure_root(self):
        """Create or locate the three structural nodes. Idempotent."""
        self.root_id = self.j.find_root(ROOT_TITLE, UT_ROOT)
        if self.root_id is None:
            self.root_id = _doc_id(self.j.create_document(
                title=ROOT_TITLE,
                content=("Federated-search source registry built by "
                         "tau-presearch. Topics form a subject taxonomy; "
                         "sources hang beneath them; manifests record which "
                         "upstream version each load came from."),
                usetype=UT_ROOT))
            self.log(f"created registry root id={self.root_id}")

        for attr, title in (("manifests_id", MANIFESTS_TITLE),
                            ("topics_id", TOPICS_TITLE)):
            found = None
            kids = self.j.children(self.root_id, depth=1, limit=50) or []
            rows = kids.get("results", kids) if isinstance(kids, dict) else kids
            for row in rows:
                doc = row.get("document", row)
                if doc.get("title") == title:
                    found = doc.get("id")
                    break
            if found is None:
                found = _doc_id(self.j.create_document(
                    title=title, content=f"{title} for {ROOT_TITLE}.",
                    parent_id=self.root_id, usetype=UT_ROOT))
            setattr(self, attr, found)
        return self.root_id

    def ensure_index(self):
        self.j.ensure_index(INDEX_NAME, "tau-presearch source registry")
        self.j.index_add_root(INDEX_NAME, self.root_id)

    # ---- topics ----------------------------------------------------------

    def _load_existing_topics(self):
        """Map topic key -> document id for topics already loaded.

        Keys are stored as the document title so one lookup rebuilds the map;
        a path is unique in the tree by construction.
        """
        if self._topic_ids:
            return self._topic_ids
        sub = self.j.get(f"/documents/{self.topics_id}/subtree?max_depth=32")
        rows = sub.get("results", sub) if isinstance(sub, dict) else (sub or [])

        def walk(node):
            doc = node.get("document", node)
            if doc.get("usetype") == UT_TOPIC and doc.get("title"):
                self._topic_ids[doc["title"]] = doc["id"]
            for child in node.get("children", []) or []:
                walk(child)

        if isinstance(rows, list):
            for row in rows:
                walk(row)
        else:
            walk(rows)
        return self._topic_ids

    def load_topics(self, topics):
        """Create topic documents parents-first. Returns count created."""
        self._load_existing_topics()
        created = 0
        for t in topics:
            if t.key in self._topic_ids:
                continue
            parent_id = self.topics_id
            if t.parent_key:
                parent_id = self._topic_ids.get(t.parent_key)
                if parent_id is None:
                    # Provider contract is parents-first; a miss here means a
                    # real ordering bug, not something to paper over.
                    raise JmftsError(
                        f"topic {t.key!r} arrived before its parent "
                        f"{t.parent_key!r}; provider must yield parents first")
            content = t.description or t.title
            doc_id = _doc_id(self.j.create_document(
                title=t.key, content=content, parent_id=parent_id,
                usetype=UT_TOPIC))
            self._topic_ids[t.key] = doc_id
            if t.parent_key:
                self.j.add_triple(doc_id, P_PARENT_TOPIC,
                                  self._topic_ids[t.parent_key])
            created += 1
            if created % 500 == 0:
                self.log(f"  topics: {created}")
        return created

    # ---- sources ---------------------------------------------------------

    def load_sources(self, sources, manifest_id, workers=8, max_sources=0):
        """Create source documents and their fact triples.

        NOTE: this is one POST /documents per source. At Curlie's full
        2,739,037 rows that is not a load, it is a denial of service against
        your own appliance -- even at 8 workers. Use --max-sources and
        --topic-prefix to take a slice, and treat "JMFTS needs a bulk ingest
        endpoint" as the finding it is.
        """
        self._load_existing_topics()
        stats = {"created": 0, "skipped_no_topic": 0, "failed": 0}

        def one(src):
            topic_key = src.primary_topic
            parent_id = self._topic_ids.get(topic_key) if topic_key else None
            if parent_id is None:
                stats["skipped_no_topic"] += 1
                return
            body = src.description or src.title
            try:
                doc_id = _doc_id(self.j.create_document(
                    title=src.title, content=f"{body}\n\n{src.url}",
                    parent_id=parent_id, usetype=UT_SOURCE))
                for pred, value in (src.facts or {}).items():
                    if pred in LITERAL_PREDICATES:
                        self.j.add_triple(doc_id, pred, value,
                                          source_document_id=manifest_id,
                                          valid_from=utcnow())
                for extra in src.topic_keys[1:]:
                    tid = self._topic_ids.get(extra)
                    if tid:
                        self.j.add_triple(doc_id, P_COVERS_TOPIC, tid,
                                          source_document_id=manifest_id)
                self.j.add_triple(doc_id, P_LISTED_BY, manifest_id)
                stats["created"] += 1
                if stats["created"] % 250 == 0:
                    self.log(f"  sources: {stats['created']}")
            except JmftsError as e:
                stats["failed"] += 1
                if stats["failed"] <= 5:
                    self.log(f"  ! {src.url}: {e}")

        batch = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, src in enumerate(sources):
                if max_sources and i >= max_sources:
                    break
                batch.append(pool.submit(one, src))
                if len(batch) >= workers * 32:
                    for f in batch:
                        f.result()
                    batch = []
            for f in batch:
                f.result()

        if stats["failed"]:
            self.log(f"  {stats['failed']} source(s) failed to load")
        return stats

    # ---- manifests -------------------------------------------------------

    def write_manifest(self, manifest: Manifest):
        title = f"{manifest.upstream.provider} @ {manifest.upstream.version}"
        return _doc_id(self.j.create_document(
            title=title, content=json.dumps(manifest.to_dict(), indent=2),
            parent_id=self.manifests_id, usetype=UT_MANIFEST))

    def manifests(self):
        """Every manifest currently recorded, newest last."""
        kids = self.j.children(self.manifests_id, depth=1, limit=500) or []
        rows = kids.get("results", kids) if isinstance(kids, dict) else kids
        out = []
        for row in rows:
            doc = row.get("document", row)
            if doc.get("usetype") != UT_MANIFEST:
                continue
            full = self.j.get_document(doc["id"])
            body = (full.get("document", full) if isinstance(full, dict)
                    else {}).get("content", "")
            try:
                out.append((doc["id"], Manifest.from_dict(json.loads(body))))
            except (ValueError, KeyError):
                self.log(f"  ! manifest {doc['id']} is unreadable; ignoring")
        out.sort(key=lambda kv: kv[1].loaded_at)
        return out
