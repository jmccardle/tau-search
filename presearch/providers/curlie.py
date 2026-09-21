"""Curlie -- the live human-edited web directory, successor to DMOZ.

Upstream: https://curlie.org/directory-dl  302s to a versioned tar.gz at LRZ.
~177 MB compressed, 619 MB unpacked, monthly. As of the 2026-02 dump:
801,720 categories and 2,739,037 sites.

File format (TSV, UTF-8, from Curlie-Readme.txt v2 2025-05):
  *-c.tsv  URL, Title, Description, Category ID
  *-s.tsv  Category ID, Full Path, Entry Count, Description, Geo (lat, lon)

Category descriptions may contain HTML. Paths are slash-separated with no
leading root segment ("Computers/Algorithms"); the row for "Top" is the root.
"""

import hashlib
import os
import re
import tarfile

import requests

from ..record import (P_CAPABILITY, P_HOME_URL, P_TEMPORAL, Source, Topic,
                      Upstream)

DOWNLOAD = "https://curlie.org/directory-dl"
NAME = "curlie"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(text):
    """Strip the HTML the editorial descriptions carry, collapse whitespace."""
    if not text:
        return ""
    return _WS.sub(" ", _TAG.sub(" ", text)).strip()


class CurlieProvider:
    kind = "bulk"
    name = NAME

    def __init__(self, data_dir, min_entries=0, topic_prefix=None,
                 max_depth=None, session=None):
        self.data_dir = data_dir
        self.min_entries = min_entries
        self.topic_prefix = topic_prefix
        self.max_depth = max_depth
        self.session = session or requests.Session()
        self.archive = os.path.join(data_dir, "curlie-rdf-all.tar.gz")
        self.extract_dir = os.path.join(data_dir, "curlie-rdf")
        self._version = ""
        self._kept = None

    # ---- acquisition -----------------------------------------------------

    def resolve(self):
        """HEAD the redirect to learn the real file URL and its version."""
        r = self.session.head(DOWNLOAD, allow_redirects=True, timeout=(10, 60))
        r.raise_for_status()
        version = (r.headers.get("Last-Modified")
                   or r.headers.get("ETag", "").strip('"') or "")
        return str(r.url), version, int(r.headers.get("Content-Length") or 0)

    def fetch(self, force=False, progress=None):
        """Download and unpack. Returns the resolved Upstream."""
        os.makedirs(self.data_dir, exist_ok=True)
        url, version, size = self.resolve()
        self._version = version

        if force or not os.path.exists(self.archive):
            with self.session.get(url, stream=True, timeout=(10, 300)) as r:
                r.raise_for_status()
                got = 0
                tmp = self.archive + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, size)
                os.replace(tmp, self.archive)

        if force or not os.path.isdir(self.extract_dir):
            with tarfile.open(self.archive, "r:gz") as tf:
                # Refuse absolute or traversing members rather than trusting
                # the tarball; this file comes off the public internet.
                for m in tf.getmembers():
                    if m.name.startswith("/") or ".." in m.name.split("/"):
                        raise ValueError(f"unsafe tar member: {m.name!r}")
                tf.extractall(self.data_dir)

        return self.upstream()

    def upstream(self):
        return Upstream(provider=NAME, url=DOWNLOAD,
                        version=self._version or self._mtime_version(),
                        digest=self._digest())

    def _mtime_version(self):
        if os.path.exists(self.archive):
            return str(int(os.path.getmtime(self.archive)))
        return ""

    def _digest(self):
        """sha256 of the archive we actually parsed. Streamed; ~177 MB."""
        if not os.path.exists(self.archive):
            return ""
        h = hashlib.sha256()
        with open(self.archive, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ---- parsing ---------------------------------------------------------

    def _tsv_files(self, suffix):
        if not os.path.isdir(self.extract_dir):
            raise SystemExit(
                f"{self.extract_dir} missing; run `presearch fetch curlie` first")
        for fn in sorted(os.listdir(self.extract_dir)):
            if fn.endswith(suffix):
                yield os.path.join(self.extract_dir, fn)

    def _keep(self, key, entry_count):
        if self.topic_prefix and not (
                key == self.topic_prefix or key.startswith(self.topic_prefix + "/")):
            return False
        if self.max_depth and key.count("/") + 1 > self.max_depth:
            return False
        if entry_count < self.min_entries:
            return False
        return True

    def _kept_topics(self):
        """Topic key -> Topic for everything this filter admits, ancestors
        included. Built once and shared by topics() and sources() so the two
        cannot disagree about what is in the tree."""
        if self._kept is not None:
            return self._kept
        kept = {}
        for path in self._tsv_files("-s.tsv"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    key = parts[1].strip()
                    if not key or key == "Top":
                        continue
                    try:
                        count = int(parts[2] or 0)
                    except ValueError:
                        count = 0
                    desc = _clean(parts[3]) if len(parts) > 3 else ""
                    if not self._keep(key, count):
                        continue
                    kept[key] = Topic(
                        key=key, title=key.rsplit("/", 1)[-1].replace("_", " "),
                        description=desc, parent_key=Topic.parent_of(key),
                        entry_count=count)

        for key in list(kept):
            parent = Topic.parent_of(key)
            while parent and parent not in kept:
                kept[parent] = Topic(
                    key=parent, title=parent.rsplit("/", 1)[-1].replace("_", " "),
                    description="", parent_key=Topic.parent_of(parent))
                parent = Topic.parent_of(parent)

        self._kept = kept
        return kept

    def topics(self):
        """Yield Topic nodes, parents before children.

        A filtered load must not orphan a node, so every kept key's ancestors
        are emitted too, even when they would fail the filter themselves.
        A tree load needs the parent id to exist before the child arrives.
        """
        kept = self._kept_topics()
        for key in sorted(kept, key=lambda k: (k.count("/"), k)):
            yield kept[key]

    def _category_paths(self):
        """Category ID -> full path, for the ids the -c.tsv rows point at."""
        paths = {}
        for path in self._tsv_files("-s.tsv"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 2:
                        continue
                    cid, key = parts[0].strip(), parts[1].strip()
                    if cid and key:
                        paths[cid] = key
        return paths

    def sources(self):
        paths = self._category_paths()
        # Membership in the ACTUAL kept set, not a re-run of the predicate:
        # a topic dropped for low entry_count must not still emit its sites.
        kept = self._kept_topics()
        for path in self._tsv_files("-c.tsv"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 4:
                        continue
                    url, title, desc, cid = (parts[0].strip(), parts[1].strip(),
                                             _clean(parts[2]), parts[3].strip())
                    if not url or not url.startswith(("http://", "https://")):
                        continue
                    key = paths.get(cid)
                    if not key or key not in kept:
                        continue
                    yield Source(
                        url=url, title=title or url, description=desc,
                        topic_keys=(key,),
                        facts={
                            P_HOME_URL: url,
                            # A directory entry is a homepage. Nothing in the
                            # dump says the site is searchable, so claim only
                            # what is true and let discovery upgrade it later.
                            P_CAPABILITY: "identifier",
                            P_TEMPORAL: "static",
                        })
