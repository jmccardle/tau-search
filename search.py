#!/usr/bin/env python3
"""tau-search: query public search endpoints and merge results.

Backends (all keyless, all plain HTTP):
  ddg        DuckDuckGo HTML endpoint (html.duckduckgo.com)
  ddg_api    DuckDuckGo instant-answer API (narrow, but very stable)
  mojeek     Mojeek (independent index, no key)
  searxng    public SearXNG instances (json first, html fallback)
  wikipedia  Wikipedia search API (entity-oriented, very stable)

Usage:
  python3 search.py "python 3.11 release notes"
  python3 search.py "rust async" --backends ddg,mojeek --limit 5
  python3 search.py "searxng" --json
"""

import argparse
import html
import json
import os
import re
import sys
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
import common

# Public SearXNG instances known to exist as of writing. Many disable the
# JSON API; we try json first and fall back to HTML. The list is ordered
# by observed reliability; first working instance wins per query.
SEARXNG_INSTANCES = [
    "https://searx.be",
    "https://search.inetol.net",
    "https://priv.au",
    "https://searx.tiekoetter.com",
    "https://search.disroot.org",
    "https://baresearch.org",
    "https://search.rhscz.eu",
]

# Overridden by --searxng-url or the SEARXNG_URL env var (e.g. a local
# instance with JSON enabled: http://127.0.0.1:8080).
SEARXNG_LOCAL = ""


# ---------------------------------------------------------------- backends

def _result(title, url, snippet, backend):
    return {"title": (title or "").strip(),
            "url": (url or "").strip(),
            "snippet": (snippet or "").strip(),
            "backend": backend}


def search_ddg(query, limit, session, timeout, local=None):
    # POST avoids the 202/challenge that GET often draws.
    url = "https://html.duckduckgo.com/html/"
    final_url, status, headers, body = _post(url, {"q": query}, session, timeout)
    if status not in (200, 202):
        raise common.FetchError(f"ddg: HTTP {status}")
    soup = BeautifulSoup(body.decode("utf-8", "replace"), "html.parser")
    results = []
    for div in soup.select("div.result"):
        a = div.select_one("h2 a") or div.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        snippet_el = div.select_one(".result__snippet")
        results.append(_result(
            a.get_text(" ", strip=True),
            common._resolve_ddg_url(a["href"], final_url),
            snippet_el.get_text(" ", strip=True) if snippet_el else "",
            "ddg"))
        if len(results) >= limit:
            break
    if not results:
        raise common.FetchError("ddg: no results (challenge page?)")
    return results


def _post(url, data, session, timeout):
    try:
        resp = session.post(url, data=data, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        raise common.FetchError(f"request failed: {e}") from e
    return str(resp.url), resp.status_code, dict(resp.headers), resp.content


def search_ddg_api(query, limit, session, timeout, local=None):
    url = ("https://api.duckduckgo.com/?q=" + quote_plus(query)
           + "&format=json&no_html=1&skip_disambig=1")
    final_url, status, headers, body = common.http_get(url, session, timeout)
    # DDG uses 202 for "valid but possibly throttled"; payload is still JSON.
    if status not in (200, 202):
        raise common.FetchError(f"ddg_api: HTTP {status}")
    data = json.loads(body.decode("utf-8", "replace"))
    results = []
    abstract = data.get("AbstractText")
    abstract_url = data.get("AbstractURL")
    if abstract and abstract_url:
        results.append(_result(data.get("Heading", query), abstract_url,
                               abstract, "ddg_api"))
    for topic in data.get("RelatedTopics", []):
        if isinstance(topic, dict) and topic.get("Text") and topic.get("FirstURL"):
            results.append(_result(topic.get("Text", "")[:120],
                                   topic["FirstURL"], "", "ddg_api"))
        if len(results) >= limit:
            break
    return results


def search_mojeek(query, limit, session, timeout, local=None):
    url = f"https://www.mojeek.com/search?q={quote_plus(query)}"
    final_url, status, headers, body = common.http_get(url, session, timeout)
    if status != 200:
        raise common.FetchError(f"mojeek: HTTP {status}")
    html = body.decode("utf-8", "replace")
    if "JavaScript is required" in html or "complete this challenge" in html:
        raise common.FetchError("mojeek: JS challenge (try another network/backend)")
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for item in soup.select("ul#results li, .results li, ol.results li"):
        a = item.select_one("h2 a") or item.select_one("a[href]")
        if not a or not a.get("href"):
            continue
        desc = item.select_one("p")
        results.append(_result(
            a.get_text(" ", strip=True),
            urljoin(final_url, a["href"]),
            desc.get_text(" ", strip=True) if desc else "",
            "mojeek"))
        if len(results) >= limit:
            break
    return results


CHALLENGE_MARKERS = ("checking your browser", "verifying your browser",
                     "anomaly", "complete this challenge",
                     "javascript is required")


def _looks_like_challenge(html, lowered):
    return any(m in lowered for m in CHALLENGE_MARKERS)


def _parse_searxng_html(html, base):
    """Standard SearXNG SERP: <article class='result ...'> with h3 > a and p.content."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for art in soup.select("article.result, .result"):
        a = art.select_one("h3 a, h2 a, a.result-title")
        if not a or not a.get("href"):
            continue
        desc = art.select_one("p.content, .content, p")
        results.append(_result(
            a.get_text(" ", strip=True),
            urljoin(base, a["href"]),
            desc.get_text(" ", strip=True) if desc else "",
            "searxng"))
    return results


def search_searxng(query, limit, session, timeout, local=None):
    """Walk instances; first one yielding results wins.

    A local instance (with JSON enabled, e.g. docker searxng) via
    SEARXNG_URL or --searxng-url is tried first.
    """
    bases = []
    local = local or os.environ.get("SEARXNG_URL") or SEARXNG_LOCAL
    if local:
        bases.append(local.rstrip("/"))
    bases += SEARXNG_INSTANCES

    last_err = "no instances"
    for base in bases:
        # Only the user-configured instance may be on a private address.
        priv = bool(local) and base == local.rstrip("/")
        # 1) JSON API (most instances disable it; cheap to try).
        try:
            url = (f"{base}/search?q={quote_plus(query)}"
                   f"&format=json&language=en")
            final_url, status, headers, body = common.http_get(
                url, session, timeout, allow_private=priv)
            if status == 200 and common.content_type(headers).endswith("json"):
                data = json.loads(body.decode("utf-8", "replace"))
                results = [
                    _result(r.get("title"), r.get("url"), r.get("content"), "searxng")
                    for r in data.get("results", [])[:limit] if r.get("url")
                ]
                if results:
                    for r in results:
                        r["backend"] = f"searxng:{base}"
                    return results
        except common.FetchError as e:
            last_err = str(e)
        # 2) HTML SERP.
        try:
            url = f"{base}/search?q={quote_plus(query)}&language=en"
            final_url, status, headers, body = common.http_get(
                url, session, timeout, allow_private=priv)
            if status != 200:
                last_err = f"{base}: HTTP {status}"
                continue
            html = body.decode("utf-8", "replace")
            if _looks_like_challenge(html, html.lower()):
                last_err = f"{base}: browser challenge"
                continue
            results = _parse_searxng_html(html, base)[:limit]
            if results:
                for r in results:
                    r["backend"] = f"searxng:{base}"
                return results
            last_err = f"{base}: empty result set"
        except common.FetchError as e:
            last_err = str(e)
    raise common.FetchError(f"searxng: all instances failed ({last_err})")


def search_wikipedia(query, limit, session, timeout, local=None):
    url = ("https://en.wikipedia.org/w/api.php?action=query"
           f"&list=search&srsearch={quote_plus(query)}"
           "&format=json&srlimit=" + str(limit))
    final_url, status, headers, body = common.http_get(url, session, timeout)
    if status != 200:
        raise common.FetchError(f"wikipedia: HTTP {status}")
    data = json.loads(body.decode("utf-8", "replace"))
    results = []
    for r in data.get("query", {}).get("search", []):
        snippet = re.sub(r"<[^>]+>", "", r.get("snippet", ""))
        snippet = html.unescape(snippet)
        title = r.get("title", "")
        # list=search omits url; reconstruct it from the title.
        wiki_url = "https://en.wikipedia.org/wiki/" + \
            quote_plus(title.replace(" ", "_"))
        results.append(_result(title, wiki_url, snippet, "wikipedia"))
    return results


BACKENDS = {
    "ddg": search_ddg,
    "ddg_api": search_ddg_api,
    "mojeek": search_mojeek,
    "searxng": search_searxng,
    "wikipedia": search_wikipedia,
}

DEFAULT_ORDER = ["ddg", "mojeek", "searxng", "ddg_api", "wikipedia"]


# ------------------------------------------------------------------- driver

def run_query(query, backend_names, limit, local=None):
    session = common.build_session()
    merged, seen = [], set()
    errors = []
    for name in backend_names:
        fn = BACKENDS[name]
        try:
            results = fn(query, limit, session, common.DEFAULT_TIMEOUT,
                         local=local)
        except (common.FetchError, Exception) as e:
            errors.append(f"{name}: {e}")
            continue
        for r in results:
            key = r["url"].split("#")[0].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    return merged, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="+", metavar="QUERY")
    ap.add_argument("--backends", default=",".join(DEFAULT_ORDER),
                    help="comma list from: " + ",".join(BACKENDS) +
                    " (default: all, in this order)")
    ap.add_argument("--limit", type=int, default=5,
                    help="max results per backend (default 5)")
    ap.add_argument("--searxng-url", default=None,
                    help="local SearXNG base URL, e.g. http://127.0.0.1:8080 "
                         "(or set SEARXNG_URL)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args(argv)

    query = " ".join(args.query)
    names = [b.strip() for b in args.backends.split(",") if b.strip()]
    unknown = [b for b in names if b not in BACKENDS]
    if unknown:
        print(f"unknown backend(s): {', '.join(unknown)}; "
              f"available: {', '.join(BACKENDS)}", file=sys.stderr)
        return 2

    merged, errors = run_query(query, names, args.limit,
                               local=args.searxng_url)

    if args.json:
        print(json.dumps({"query": query, "results": merged,
                          "errors": errors}, ensure_ascii=False, indent=2))
    else:
        print(f"== search: {query!r} ({len(merged)} results) ==")
        for i, r in enumerate(merged, 1):
            print(f"\n{i}. {r['title']}  [{r['backend']}]")
            print(f"   {r['url']}")
            if r["snippet"]:
                print(f"   {r['snippet'][:240]}")
        for e in errors:
            print(f"\n[warn] {e}", file=sys.stderr)

    return 0 if merged else 1


if __name__ == "__main__":
    sys.exit(main())
