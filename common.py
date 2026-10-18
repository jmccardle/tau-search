"""Shared plumbing for fetch.py and search.py.

Tier-1 web access: plain HTTP GET, browser-like headers, hard timeouts,
size caps, small disk cache. No JS, no cookies, no stealth.
"""

import hashlib
import json
import os
import re
import socket
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Tag

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEFAULT_TIMEOUT = (10, 25)   # (connect, read) seconds
MAX_BYTES = 5_000_000        # stop downloading beyond this


class FetchError(Exception):
    """Raised when a URL cannot be fetched or converted to usable text."""


def build_session(user_agent=DEFAULT_UA):
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "application/json;q=0.8,*/*;q=0.7"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def refusal(url):
    """Why url must not be fetched by default, or None. http(s) to public hosts only."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"only http and https are fetched, not {parts.scheme or 'no scheme'}"
    if not parts.hostname:
        return "the URL names no host"
    if is_private_host(parts.hostname):
        return f"{parts.hostname!r} is a private/loopback/non-public host"
    return None


def _redirect_guard(resp, *args, **kwargs):
    """requests response hook: check each redirect target before it is followed."""
    if not resp.is_redirect:
        return
    nxt = urljoin(resp.url, resp.headers.get("location", ""))
    why = refusal(nxt)
    if why:
        resp.close()
        raise FetchError(f"redirect to {nxt} refused: {why}")


def http_get(url, session=None, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_BYTES,
             allow_private=False):
    """GET a URL. Returns (final_url, status, headers, body_bytes).

    Unless allow_private, the URL and every redirect hop must be http(s) to a
    public host (SSRF guard). Raises FetchError on a refused URL, network
    failure, oversize body, or non-2xx.
    """
    session = session or build_session()
    hooks = None
    if not allow_private:
        why = refusal(url)
        if why:
            raise FetchError(f"{url} refused: {why}")
        hooks = {"response": _redirect_guard}  # per-request: session is untouched
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True,
                           stream=True, hooks=hooks)
    except requests.RequestException as e:
        raise FetchError(f"request failed: {e}") from e

    body = b""
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            body += chunk
            if len(body) > max_bytes:
                raise FetchError(f"body exceeds {max_bytes} bytes; aborting")
    except requests.RequestException as e:
        raise FetchError(f"read failed: {e}") from e
    finally:
        try:
            resp.close()
        except Exception:
            pass

    return str(resp.url), resp.status_code, dict(resp.headers), body


def content_type(headers):
    for k, v in headers.items():
        if k.lower() == "content-type":
            return (v or "").split(";")[0].strip().lower()
    return ""


BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "pre",
              "blockquote", "li", "dt", "dd", "figcaption", "tr", "td", "th"}
INLINE_TAGS = {"a", "b", "i", "em", "strong", "span", "code", "abbr", "s",
               "small", "u", "sub", "sup", "mark", "cite", "q", "time",
               "kbd", "var", "samp", "bdo", "wbr"}


def _is_leaf_block(el):
    """True if el contains no non-inline element (i.e. no nested block)."""
    for child in el.descendants:
        if isinstance(child, Tag) and child.name not in INLINE_TAGS:
            return False
    return True


def html_to_text(html, base_url):
    """Convert an HTML document to clean text. Returns (title, text).

    Strategy: find the main content container, strip boilerplate, then
    collect text from 'leaf blocks' — block-level elements whose
    descendants are all inline, so text is taken at the right granularity
    without duplication. Links become 'text (url)'.
    """
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    for tag in soup(["script", "style", "noscript", "template", "svg",
                     "iframe", "canvas", "form", "head"]):
        tag.decompose()
    for tag in soup.find_all(["nav", "footer", "aside", "header"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": re.compile(
            "^(navigation|banner|complementary|search|contentinfo|form)$")}):
        tag.decompose()
    def _has_class(tag, cls):
        classes = tag.get("class") or []
        return any(c == cls for c in classes)
    for tag in soup.find_all(lambda t: _has_class(t, "headerlink")
                             or _has_class(t, "edit-link")):
        tag.decompose()

    # Inline links: append the target so 'text (url)' survives extraction.
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if href.startswith(("http://", "https://")):
            a.append(f" ({urljoin(base_url, href)})")

    # Main content container, else fall back to body.
    root = None
    for sel in ("article", "main", "[role=main]", "#content", ".content"):
        root = soup.select_one(sel)
        if root is not None:
            break
    if root is None:
        root = soup.body if soup.body else soup

    parts = []
    for el in root.find_all(True):
        if el.name not in BLOCK_TAGS or not _is_leaf_block(el):
            continue
        text = el.get_text(" ", strip=True)
        if text:
            parts.append(text)

    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text.strip()


def body_to_text(final_url, status, headers, body):
    """Dispatch on content type. Returns (title, text) or (title, json_str).

    Raises FetchError for non-2xx or content types we cannot handle.
    """
    if status != 200:
        raise FetchError(f"HTTP {status}")

    ctype = content_type(headers)

    if ctype in ("application/json", "application/ld+json",
                 "application/xml", "text/xml"):
        return "json", body.decode("utf-8", errors="replace")

    if ctype == "text/plain":
        return "text", body.decode("utf-8", errors="replace")

    if ctype in ("text/html", "application/xhtml+xml") or ctype == "":
        if b"<html" not in body.lower()[:2048] and ctype not in ("", "text/html"):
            # Unknown binary type with no HTML marker: bail.
            raise FetchError(f"unsupported content type: {ctype!r}")
        return html_to_text(body.decode("utf-8", errors="replace"), final_url)

    # Guess by content sniffing.
    if b"<html" in body.lower()[:2048]:
        return html_to_text(body.decode("utf-8", errors="replace"), final_url)
    if body.lstrip().startswith((b"{", b"[")):
        return "json", body.decode("utf-8", errors="replace")
    return "text", body.decode("utf-8", errors="replace")


def _resolve_ddg_url(href, base_url):
    """DuckDuckGo html endpoint wraps real URLs in a redirect:
    //duckduckgo.com/l/?uddg=<percent-encoded-real-url>&rut=...
    Unwrap it; pass through anything that isn't.
    """
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlsplit(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        from urllib.parse import parse_qs
        q = parse_qs(parsed.query)
        if q.get("uddg"):
            return q["uddg"][0]
    return href


def is_private_host(hostname):
    """SSRF guard: True if the host is, or resolves to, a non-public address.

    Every address the name resolves to is checked (a name with one public and
    one loopback address is not public), with the ipaddress module rather than
    string prefixes: 172.16.0.0/12, 100.64.0.0/10 (carrier-grade NAT), IPv6
    loopback and link-local, and the rest of the reserved space all count.
    """
    import ipaddress

    h = (hostname or "").lower().strip("[]")
    if h in ("localhost",) or h.endswith(".local") or h.endswith(".internal"):
        return True
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror:
        return False  # unresolvable; the fetch will fail on its own
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return True
    return False


CACHE_KEYS = {"url", "title", "content_type", "status", "text"}


def _cache_path(url):
    # .json: entries from the old text-only scheme (.txt) are simply misses.
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".json")


def cache_get(url):
    """Return the cached entry (dict: url, title, content_type, status, text) or None."""
    path = _cache_path(url)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        entry = json.loads(raw)
    except ValueError as e:
        raise FetchError(f"corrupt cache entry {path}: {e}") from e
    if not isinstance(entry, dict) or not CACHE_KEYS <= entry.keys():
        raise FetchError(f"corrupt cache entry {path}: expected an object "
                         f"with keys {sorted(CACHE_KEYS)}")
    return entry


def cache_set(url, entry):
    """Store the full (untruncated) entry dict for url."""
    path = _cache_path(url)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
        os.replace(tmp, path)  # atomic: a reader never sees half an entry
    except OSError:
        pass  # cache is best-effort
    finally:
        try:
            os.remove(tmp)  # only still there if the write or replace failed
        except OSError:
            pass


def truncate(text, limit):
    """Cut text to limit chars, adding a marker if cut. limit<=0 disables."""
    if limit and len(text) > limit:
        return text[:limit] + f"\n\n[... truncated at {limit} chars ...]"
    return text
