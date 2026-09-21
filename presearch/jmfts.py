"""Minimal JMFTS REST client, scoped to what the registry needs.

Written against the documented surface: /capabilities, /documents,
/search/*, /triples/*, /indexes/*. Nothing here assumes an endpoint exists --
`preflight()` asks the appliance what it can do and raises if the registry's
requirements are unmet, rather than discovering it halfway through a load.
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


class JmftsError(Exception):
    """Raised when the appliance rejects a call or cannot be reached."""


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

    def _call(self, method, path, **kw):
        url = f"{self.base}{path}"
        last = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last = JmftsError(f"{method} {path}: {e}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 500 and attempt < self.retries:
                last = JmftsError(f"{method} {path}: HTTP {r.status_code}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                detail = r.text[:400]
                raise JmftsError(f"{method} {path}: HTTP {r.status_code}: {detail}")
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

        methods = caps.get("retrieval_methods") or caps.get("methods") or []
        if isinstance(methods, dict):
            methods = list(methods)
        available = {str(m).lower() for m in methods}
        if available:
            missing = [m for m in need_methods if m not in available]
            if missing:
                raise JmftsError(
                    f"appliance lacks required retrieval method(s): {missing}; "
                    f"it reports {sorted(available)}"
                )
        return caps

    # ---- documents -------------------------------------------------------

    def create_document(self, title, content, parent_id=None, usetype=None,
                        auto_embed=False):
        """Create one document. auto_embed defaults False: the registry is a
        lexical index, and embedding 2.7M directory stubs would dominate the
        appliance for no measured gain."""
        payload = {"content": content, "auto_embed": auto_embed}
        if title is not None:
            payload["title"] = title
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if usetype is not None:
            payload["usetype"] = usetype
        return self.post("/documents", payload)

    def get_document(self, doc_id):
        return self.get(f"/documents/{doc_id}")

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
               parent_id=None, methods=None):
        payload = {"query": query, "limit": limit}
        if usetype:
            payload["usetype"] = usetype
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if method == "hybrid":
            payload["methods"] = methods or ["fulltext", "bm25"]
        return self.post(f"/search/{method}", payload)

    # ---- triples ---------------------------------------------------------

    def predicates(self):
        return self.get("/triples/predicates")

    def ensure_predicate(self, name, description=""):
        """Get or create a predicate id, cached for the client's lifetime."""
        if name in self._predicate_ids:
            return self._predicate_ids[name]
        data = self.predicates() or []
        rows = data.get("results", data) if isinstance(data, dict) else data
        for row in rows:
            if row.get("name") == name:
                self._predicate_ids[name] = row["id"]
                return row["id"]
        payload = {"name": name}
        if description:
            payload["description"] = description
        created = self.post("/triples/predicates", payload)
        pid = created.get("id") if isinstance(created, dict) else created
        if pid is None:
            raise JmftsError(f"could not create predicate {name!r}")
        self._predicate_ids[name] = pid
        return pid

    def add_triple(self, subject_id, predicate, obj, datatype=None,
                   source_document_id=None, valid_from=None):
        """Write one triple. `obj` is an int document id, or a string literal
        when `predicate` is in LITERAL_PREDICATES."""
        pid = self.ensure_predicate(predicate)
        payload = {"subject_id": subject_id, "predicate_id": pid}
        if predicate in LITERAL_PREDICATES:
            payload["object_literal"] = str(obj)
            if datatype:
                payload["object_datatype"] = datatype
        else:
            payload["object_id"] = int(obj)
        if source_document_id is not None:
            payload["source_document_id"] = source_document_id
        if valid_from is not None:
            payload["valid_from"] = valid_from
        return self.post("/triples", payload)

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
