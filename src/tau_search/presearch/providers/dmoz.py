"""DMOZ / Open Directory Project -- the 2017 terminal snapshot.

Upstream: the Internet Archive mirror of the final ODP RDF dumps.

Why load a dead directory at all? Two reasons, both about coverage Curlie
does not have:

  * DMOZ at its peak listed categories and sites Curlie later pruned. For
    "where did people discuss X" questions about the pre-2015 web -- mailing
    lists, newsgroups, forums -- the dead directory is the better map.
  * A URL present in DMOZ and absent from Curlie is a signal, not noise: it
    dates the source. Loaded with its own valid_until, it tells the broker
    "this was authoritative, and stopped being listed" instead of silently
    vanishing.

Format is RDF/XML, gzipped, ~2 GB unpacked:
  structure.rdf.u8   <Topic r:id="Top/Arts">, <d:Title>, <d:Description>
  content.rdf.u8     <ExternalPage about="URL">, <d:Title>, <d:Description>,
                     <topic>Top/Arts/...</topic>
"""

import gzip
import hashlib
import os
import xml.etree.ElementTree as ET

import requests

from ..record import (P_CAPABILITY, P_HOME_URL, P_TEMPORAL, Source, Topic,
                      Upstream)

NAME = "dmoz"
BASE = "https://archive.org/download/dmoz-rdf-20150327"
FILES = {"structure": "structure.rdf.u8.gz", "content": "content.rdf.u8.gz"}


def _local(tag):
    """Strip the namespace; these dumps use three and none consistently."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


class DmozProvider:
    kind = "bulk"
    name = NAME

    def __init__(self, data_dir, min_entries=0, topic_prefix=None,
                 max_depth=None, session=None):
        self.data_dir = os.path.join(data_dir, "dmoz")
        self.topic_prefix = topic_prefix
        self.max_depth = max_depth
        self.session = session or requests.Session()

    def _path(self, which):
        return os.path.join(self.data_dir, FILES[which])

    def fetch(self, force=False, progress=None):
        os.makedirs(self.data_dir, exist_ok=True)
        for which, fn in FILES.items():
            dest = self._path(which)
            if os.path.exists(dest) and not force:
                continue
            url = f"{BASE}/{fn}"
            with self.session.get(url, stream=True, timeout=(10, 300)) as r:
                r.raise_for_status()
                size = int(r.headers.get("Content-Length") or 0)
                got = 0
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, size)
                os.replace(tmp, dest)
        return self.upstream()

    def upstream(self):
        return Upstream(provider=NAME, url=BASE, version="20150327",
                        digest=self._digest())

    def _digest(self):
        """Digest the structure file only. The content file is ~2 GB and the
        pair is versioned together by the archive item date."""
        p = self._path("structure")
        if not os.path.exists(p):
            return ""
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _keep(self, key):
        if self.topic_prefix and not (
                key == self.topic_prefix or key.startswith(self.topic_prefix + "/")):
            return False
        if self.max_depth and key.count("/") + 1 > self.max_depth:
            return False
        return True

    @staticmethod
    def _strip_top(key):
        """DMOZ keys carry a 'Top/' prefix; Curlie keys do not. Normalize to
        Curlie's shape so the two providers share one tree."""
        return key[4:] if key.startswith("Top/") else key

    def _iter(self, which, wanted):
        path = self._path(which)
        if not os.path.exists(path):
            raise SystemExit(f"{path} missing; run `presearch fetch dmoz` first")
        with gzip.open(path, "rb") as f:
            for event, el in ET.iterparse(f, events=("end",)):
                if _local(el.tag) == wanted:
                    yield el
                    el.clear()

    def topics(self):
        seen = {}
        for el in self._iter("structure", "Topic"):
            raw = el.get("{http://www.w3.org/TR/RDF/}id") or el.get("id") or ""
            if not raw:
                for k, v in el.attrib.items():
                    if _local(k) == "id":
                        raw = v
                        break
            key = self._strip_top(raw)
            if not key or key == "Top" or not self._keep(key):
                continue
            title, desc = "", ""
            for child in el:
                name = _local(child.tag)
                if name == "Title":
                    title = (child.text or "").strip()
                elif name == "Description":
                    desc = (child.text or "").strip()
            seen[key] = Topic(key=key,
                              title=title or key.rsplit("/", 1)[-1].replace("_", " "),
                              description=desc, parent_key=Topic.parent_of(key))

        for key in list(seen):
            parent = Topic.parent_of(key)
            while parent and parent not in seen:
                seen[parent] = Topic(
                    key=parent, title=parent.rsplit("/", 1)[-1].replace("_", " "),
                    parent_key=Topic.parent_of(parent))
                parent = Topic.parent_of(parent)

        for key in sorted(seen, key=lambda k: (k.count("/"), k)):
            yield seen[key]

    def sources(self):
        for el in self._iter("content", "ExternalPage"):
            url = el.get("about") or ""
            if not url.startswith(("http://", "https://")):
                continue
            title, desc, topic = "", "", ""
            for child in el:
                name = _local(child.tag)
                if name == "Title":
                    title = (child.text or "").strip()
                elif name == "Description":
                    desc = (child.text or "").strip()
                elif name == "topic":
                    topic = self._strip_top((child.text or "").strip())
            if not topic or not self._keep(topic):
                continue
            yield Source(url=url, title=title or url, description=desc,
                         topic_keys=(topic,),
                         facts={P_HOME_URL: url, P_CAPABILITY: "identifier",
                                P_TEMPORAL: "static"})
