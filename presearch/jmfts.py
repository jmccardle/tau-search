"""Minimal JMFTS REST client, scoped to what the registry needs.

Written against the JMFTS 0.7.0 REST surface: /capabilities, /documents,
/search/*, /triples/*, /indexes/*. Request/response shapes follow
jmfts-client's `contracts/` models. `preflight()` asks the appliance what it
can do and raises if the registry's requirements are unmet, rather than
discovering it halfway through a load.
"""

import json
import os
import time
from urllib.parse import quote

import requests

from .record import LITERAL_PREDICATES

DEFAULT_URL = os.environ.get("JMFTS_API_URL", "http://127.0.0.1:8100")
DEFAULT_TOKEN = os.environ.get("JMFTS_API_TOKEN", "")
DEFAULT_TIMEOUT = (10, 60)


LITERAL_UNSUPPORTED = (
    "cannot write literal-valued predicate {predicate!r}: JMFTS 0.7.0 has no "
    "REST verb for literal triples (TripleCreate requires object_id). "
    "Tracked as jmfts#48 (dev.ffwf.net john/jmfts); loads of sources are "
    "refused until it lands.")

# Methods safe to repeat after a 5xx or a dropped connection: a repeat cannot
# create a second row. POST is excluded -- a 500 can arrive after the write
# committed, and retrying POST /documents or POST /triples would duplicate it.
# (PUT /triples is the idempotent assert, so triple writes do retry.)
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT"})


class JmftsError(Exception):
    """Raised when the appliance rejects a call or cannot be reached.

    `status` is the HTTP status when the appliance answered, else None.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Jmfts:
    def __init__(self, base_url=DEFAULT_URL, token=DEFAULT_TOKEN,
                 timeout=DEFAULT_TIMEOUT, retries=2):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Content-Type"] = "application/json"
        self._predicate_ids = {}

    # ---- transport -------------------------------------------------------

    def _call(self, method, path, retry=None, **kw):
        """`retry` overrides the idempotency default: True for a POST that only
        reads (search), never for one that writes."""
        url = f"{self.base}{path}"
        last = None
        if retry is None:
            retry = method in IDEMPOTENT_METHODS
        retries = self.retries if retry else 0
        for attempt in range(retries + 1):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last = JmftsError(f"{method} {path}: {e}")
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 500 and attempt < retries:
                last = JmftsError(f"{method} {path}: HTTP {r.status_code}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                detail = r.text[:400]
                raise JmftsError(f"{method} {path}: HTTP {r.status_code}: {detail}",
                                 status=r.status_code)
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError as e:
                raise JmftsError(f"{method} {path}: non-JSON response") from e
        raise last

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, payload=None, **kw):
        data = json.dumps(payload) if payload is not None else None
        return self._call("POST", path, data=data, **kw)

    def put(self, path, payload=None, **kw):
        data = json.dumps(payload) if payload is not None else None
        return self._call("PUT", path, data=data, **kw)

    def patch(self, path, payload=None, **kw):
        data = json.dumps(payload) if payload is not None else None
        return self._call("PATCH", path, data=data, **kw)

    # ---- preflight -------------------------------------------------------

    def capabilities(self):
        return self.get("/capabilities")

    def preflight(self, need_methods=("bm25",)):
        """Fail before a load rather than during one.

        Returns the capabilities dict. Raises if a retrieval method the
        registry depends on is not available on this appliance.
        """
        try:
            caps = self.capabilities()
        except JmftsError as e:
            raise JmftsError(
                f"cannot reach JMFTS at {self.base}: {e}\n"
                "Set JMFTS_API_URL / JMFTS_API_TOKEN, or pass --jmfts."
            ) from e

        # CapabilitiesResponse.retrieval.methods (contracts/meta.py). An absent
        # or empty list is an error, not a pass: nothing could be verified.
        retrieval = caps.get("retrieval") if isinstance(caps, dict) else None
        methods = retrieval.get("methods") if isinstance(retrieval, dict) else None
        if not isinstance(methods, list) or not methods:
            raise JmftsError(
                f"{self.base}/capabilities has no retrieval.methods list; "
                f"cannot verify the appliance supports {list(need_methods)} "
                "(is this JMFTS >= 0.7.0?)")
        available = {str(m).lower() for m in methods}
        missing = [m for m in need_methods if m not in available]
        if missing:
            raise JmftsError(
                f"appliance lacks required retrieval method(s): {missing}; "
                f"it reports {sorted(available)}")
        return caps

    # ---- documents -------------------------------------------------------

    def create_document(self, title, content, parent_id=None, usetype=None,
                        auto_embed=False, dedup=False):
        """Create one document. auto_embed defaults False: the registry is a
        lexical index, and embedding 2.7M directory stubs would dominate the
        appliance for no measured gain.

        auto_index_bm25 is always False: DocumentCreate defaults it True, which
        writes the row into the `default` BM25 index beside real content. The
        registry is searched through its own `presearch` index, which
        index_refresh rebuilds from the registered root's subtree.

        dedup=True sends ?dedup=true: an identical (content, parent) document
        is returned instead of inserted, so a reload does not duplicate it.
        """
        payload = {"content": content, "auto_embed": auto_embed,
                   "auto_index_bm25": False}
        if title is not None:
            payload["title"] = title
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if usetype is not None:
            payload["usetype"] = usetype
        return self.post("/documents", payload,
                         params={"dedup": "true"} if dedup else None)

    def get_document(self, doc_id):
        return self.get(f"/documents/{doc_id}")

    def update_document(self, doc_id, content=None, title=None):
        """PATCH /documents/{id} (DocumentUpdate). re_embed is off: the
        registry never embeds, and the default (True) would."""
        payload = {"re_embed": False}
        if content is not None:
            payload["content"] = content
        if title is not None:
            payload["title"] = title
        return self.patch(f"/documents/{doc_id}", payload)

    def subtree(self, doc_id):
        """GET /documents/{id}/subtree -> SubtreeResponse
        {root, descendants: [flat DocumentResponse list], total}.

        A root that is still ingesting is a 409 (InFlightSubtreeError); that is
        an error here, never an empty tree -- an empty answer would make the
        caller recreate everything under it."""
        try:
            return self.get(f"/documents/{doc_id}/subtree")
        except JmftsError as e:
            if e.status == 409:
                raise JmftsError(
                    f"document {doc_id} is still ingesting (subtree 409); "
                    "wait for it to settle and retry", status=409) from e
            raise

    def children(self, doc_id, depth=1, limit=1000):
        return self.get(f"/documents/{doc_id}/children?depth={depth}&limit={limit}")

    def ancestors(self, doc_id):
        return self.get(f"/documents/{doc_id}/ancestors")

    def roots(self):
        return self.get("/documents/roots")

    def find_root(self, title, usetype):
        """Locate an existing registry root by title, or None."""
        data = self.roots() or []
        rows = data.get("results", data) if isinstance(data, dict) else data
        for row in rows:
            doc = row.get("document", row)
            if doc.get("title") == title and doc.get("usetype") == usetype:
                return doc.get("id")
        return None

    # ---- search ----------------------------------------------------------

    def search(self, query, method="bm25", limit=10, usetype=None,
               parent_id=None, methods=None, index_name=None):
        """SearchRequest.limit is 1..100 server-side; asking for more is a
        caller bug, so it raises here rather than as a 422.

        index_name scopes the BM25 leg to a named index: a query param on
        /search/bm25, a body field on /search/hybrid. fulltext and vector
        have no index concept, so there parent_id is the only scope.
        """
        if not 1 <= limit <= 100:
            raise JmftsError(f"search limit must be 1..100, got {limit}")
        payload = {"query": query, "limit": limit}
        if usetype:
            payload["usetype"] = usetype
        if parent_id is not None:
            payload["parent_id"] = parent_id
        kw = {}
        if method == "hybrid":
            payload["methods"] = methods or ["fulltext", "bm25"]
            if index_name:
                payload["index_name"] = index_name
        elif method == "bm25" and index_name:
            kw["params"] = {"index_name": index_name}
        # Search is a read: safe to retry despite being a POST.
        return self.post(f"/search/{method}", payload, retry=True, **kw)

    # ---- triples ---------------------------------------------------------

    def predicates(self):
        # with_triples_only defaults True server-side, which hides predicates
        # that exist but have no triples yet -- exactly the ones we create.
        return self.get("/triples/predicates",
                        params={"with_triples_only": "false"})

    def _find_predicate(self, name):
        data = self.predicates() or []
        rows = data.get("results", data) if isinstance(data, dict) else data
        for row in rows:
            if row.get("name") == name:
                return row["id"]
        return None

    def ensure_predicate(self, name, description=""):
        """Get or create a predicate id, cached for the client's lifetime."""
        if name in self._predicate_ids:
            return self._predicate_ids[name]
        pid = self._find_predicate(name)
        if pid is None:
            payload = {"name": name}
            if description:
                payload["description"] = description
            try:
                created = self.post("/triples/predicates", payload)
            except JmftsError as e:
                if e.status != 409:
                    raise
                # Created by someone else between our list and our create.
                pid = self._find_predicate(name)
                if pid is None:
                    raise JmftsError(
                        f"predicate {name!r}: create returned 409 but it is "
                        "not listed") from e
            else:
                pid = created.get("id") if isinstance(created, dict) else created
        if pid is None:
            raise JmftsError(f"could not create predicate {name!r}")
        self._predicate_ids[name] = pid
        return pid

    def add_triple(self, subject_id, predicate, obj, source_document_id=None,
                   valid_from=None):
        """Assert one document-valued fact via PUT /triples.

        PUT is the idempotent assert: a repeat of (subject, predicate, object)
        returns the existing triple untouched (ON CONFLICT DO NOTHING), so a
        reload neither 500s on the unique key nor rewrites the original
        validity window or provenance. POST /triples?dedup=true behaves the
        same; PUT is the named verb, and being idempotent it is also safe for
        this client to retry.

        Literal-valued predicates cannot be written over REST in JMFTS 0.7.0:
        TripleCreate requires object_id and has no literal fields. This raises
        rather than store the value anywhere else (jmfts#48).
        """
        if predicate in LITERAL_PREDICATES:
            raise JmftsError(LITERAL_UNSUPPORTED.format(predicate=predicate))
        pid = self.ensure_predicate(predicate)
        payload = {"subject_id": subject_id, "predicate_id": pid,
                   "object_id": int(obj)}
        if source_document_id is not None:
            payload["source_document_id"] = source_document_id
        if valid_from is not None:
            payload["valid_from"] = valid_from
        return self.put("/triples", payload)

    def triples_for(self, entity_id, limit=50):
        return self.get(f"/triples/query?entity_id={entity_id}&limit={limit}")

    # ---- indexes ---------------------------------------------------------

    def indexes(self):
        return self.get("/indexes")

    def ensure_index(self, name, description=""):
        data = self.indexes() or []
        rows = data.get("results", data) if isinstance(data, dict) else data
        for row in rows:
            if row.get("name") == name:
                return row
        payload = {"name": name}
        if description:
            payload["description"] = description
        return self.post("/indexes", payload)

    def index_add_root(self, name, root_document_id):
        return self.post(
            f"/indexes/{quote(name)}/roots?root_document_id={root_document_id}")

    def index_refresh(self, name):
        return self.post(f"/indexes/{quote(name)}/refresh")
