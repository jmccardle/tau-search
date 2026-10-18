"""Checking that the registry still rests on the upstream it claims.

A broker that routes queries using a stale index gives wrong answers that look
right, which is the failure mode worth the most effort to prevent. So every
load records a manifest, and this module re-derives the upstream stamp and
compares.

Statuses:
  CURRENT     upstream version matches what was loaded
  DRIFTED     upstream has a newer version than the manifest records
  UNVERIFIED  upstream offers no comparable version signal (live APIs)
  UNREACHABLE upstream could not be contacted
  DEGRADED    sampled source URLs are failing above the tolerated rate
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from .providers import ALL
from .record import UT_SOURCE
from .registry import INDEX_NAME

CURRENT = "CURRENT"
DRIFTED = "DRIFTED"
UNVERIFIED = "UNVERIFIED"
UNREACHABLE = "UNREACHABLE"
DEGRADED = "DEGRADED"

BLOCKING = {DRIFTED, UNREACHABLE, DEGRADED}


@dataclass
class Check:
    provider: str
    status: str
    loaded_version: str
    live_version: str
    detail: str = ""

    def line(self):
        mark = {CURRENT: "ok", DRIFTED: "DRIFT", UNVERIFIED: "unverified",
                UNREACHABLE: "UNREACHABLE", DEGRADED: "DEGRADED"}[self.status]
        s = f"[{mark:>11}] {self.provider:<12} loaded={self.loaded_version or '-'}"
        if self.live_version and self.live_version != self.loaded_version:
            s += f" live={self.live_version}"
        if self.detail:
            s += f"  {self.detail}"
        return s


def check_manifest(manifest, data_dir, session=None):
    """Re-derive the upstream stamp for one manifest and compare."""
    up = manifest.upstream
    cls = ALL.get(up.provider)
    if cls is None:
        return Check(up.provider, UNVERIFIED, up.version, "",
                     "no provider registered for this manifest")

    session = session or requests.Session()
    try:
        provider = cls(data_dir=data_dir, session=session)
    except TypeError:
        provider = cls(data_dir=data_dir)

    # Bulk providers with a resolvable URL expose a cheap version signal.
    if hasattr(provider, "resolve"):
        try:
            _, live_version, _ = provider.resolve()
        except requests.RequestException as e:
            return Check(up.provider, UNREACHABLE, up.version, "", str(e)[:120])
        if not live_version:
            return Check(up.provider, UNVERIFIED, up.version, "",
                         "upstream sends no ETag or Last-Modified")
        if live_version != up.version:
            return Check(up.provider, DRIFTED, up.version, live_version,
                         "a newer dump is published; reload to pick it up")
        return Check(up.provider, CURRENT, up.version, live_version)

    # Live-API providers: no digest is possible, so compare a count probe.
    if hasattr(provider, "probe"):
        try:
            n = provider.probe()
        except requests.RequestException as e:
            return Check(up.provider, UNREACHABLE, up.version, "", str(e)[:120])
        if n < 0:
            return Check(up.provider, UNREACHABLE, up.version, "",
                         "probe failed")
        live = f"count={n}"
        if up.version.startswith("count="):
            try:
                was = int(up.version.split("=", 1)[1])
            except ValueError:
                was = None
            if was and abs(n - was) / max(was, 1) > 0.05:
                return Check(up.provider, DRIFTED, up.version, live,
                             f"result count moved {was} -> {n} (>5%)")
            return Check(up.provider, CURRENT, up.version, live)
        return Check(up.provider, UNVERIFIED, up.version, live,
                     "no comparable count recorded at load time")

    return Check(up.provider, UNVERIFIED, up.version, "",
                 "provider exposes no version signal")


def sample_sources(registry, sample=25, tolerate=0.4, workers=8):
    """Spot-check that registered source URLs still resolve.

    A directory ages by rot, not by edit: the dump can be current while a
    third of what it lists is gone. `tolerate` is the dead fraction above
    which the registry is reported DEGRADED.
    """
    hits = registry.j.search("http", method="bm25", limit=sample,
                             usetype=UT_SOURCE, parent_id=registry.root_id,
                             index_name=INDEX_NAME)
    rows = (hits or {}).get("results", []) if isinstance(hits, dict) else []
    urls = []
    for row in rows:
        doc = row.get("document", row)
        for token in (doc.get("content") or "").split():
            if token.startswith(("http://", "https://")):
                urls.append(token)
                break
    if not urls:
        return Check("sources", UNVERIFIED, "", "", "no source URLs sampled")

    session = requests.Session()
    session.headers["User-Agent"] = "tau-presearch/0.1 (link check)"

    def alive(url):
        try:
            r = session.head(url, allow_redirects=True, timeout=(5, 15))
            if r.status_code >= 400:
                r = session.get(url, stream=True, timeout=(5, 15))
            return r.status_code < 400
        except requests.RequestException:
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(alive, urls))

    dead = results.count(False)
    frac = dead / len(results)
    detail = f"{dead}/{len(results)} sampled URLs dead ({frac:.0%})"
    status = DEGRADED if frac > tolerate else CURRENT
    return Check("sources", status, "", "", detail)


def run(registry, data_dir, sample=25, tolerate=0.4):
    """Full validation pass. Returns (checks, ok)."""
    checks = []
    manifests = registry.manifests()
    if not manifests:
        checks.append(Check("registry", UNREACHABLE, "", "",
                            "no manifests found; nothing has been loaded"))
        return checks, False

    seen = set()
    for _, manifest in reversed(manifests):
        if manifest.upstream.provider in seen:
            continue
        seen.add(manifest.upstream.provider)
        checks.append(check_manifest(manifest, data_dir))

    if sample:
        checks.append(sample_sources(registry, sample=sample, tolerate=tolerate))

    ok = not any(c.status in BLOCKING for c in checks)
    return checks, ok
