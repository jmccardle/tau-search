# tau-search

Local web access for agents. Tier-1 first: plain HTTP with browser-like
headers, hard timeouts, size caps, and a small disk cache. Escalate to a
headless browser only when tier 1 genuinely fails.

## Setup

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Fetch a URL

```sh
./venv/bin/python fetch.py https://docs.python.org/3/whatsnew/3.11.html
./venv/bin/python fetch.py --max-chars 2000 --show-meta URL
./venv/bin/python fetch.py --json URL        # machine-readable
./venv/bin/python fetch.py --no-cache URL    # skip disk cache
./venv/bin/python fetch.py --allow-private http://127.0.0.1:8080/
```

Behavior:
- GET with a real-browser `User-Agent` and `Accept` headers (defeats most
  naive UA-based 403s).
- Connect timeout 10s, read timeout 25s, body cap 5 MB.
- HTML → clean text: main content container, boilerplate stripped, leaf
  blocks collected, links kept as `text (url)`. JSON/XML/text pass through.
- Output truncated to `--max-chars` (default 8000).
- Private/loopback hosts print a warning unless `--allow-private`.
- Successful fetches cached in `.cache/` keyed by URL sha1.

## Search

```sh
./venv/bin/python search.py "rust async tokio tutorial"
./venv/bin/python search.py "rust async" --backends ddg,mojeek --limit 5
./venv/bin/python search.py "searxng" --json
./venv/bin/python search.py "query" --searxng-url http://127.0.0.1:8080
```

`--backends` takes a comma list (default: all, in the order below). Results
are merged in backend order and deduped by URL. Backends that fail print a
warning and don't sink the whole query.

| name        | what it is                                          | status from this network |
|-------------|-----------------------------------------------------|--------------------------|
| `ddg`       | DuckDuckGo HTML endpoint, POST (avoids 202s)       | working                  |
| `ddg_api`   | DuckDuckGo instant-answer API (entity queries only)| working, thin            |
| `mojeek`    | Mojeek — independent index                          | JS challenge (best-effort) |
| `searxng`   | public SearXNG instances (JSON, then HTML)          | flaky (see below)        |
| `wikipedia` | Wikipedia search API (entity-oriented)              | working                  |

**SearXNG note:** public instances are a lottery — some serve a browser
challenge, some return empty result sets, most disable the JSON API. The
backend walks the instance list and takes the first one that yields
results. For reliability, run a local instance (docker searxng with JSON
enabled) and point at it:

```sh
SEARXNG_URL=http://127.0.0.1:8080 ./venv/bin/python search.py "query"
# or: --searxng-url http://127.0.0.1:8080
```

## Layout

```
common.py    shared http fetch, html→text, cache, SSRF guard
fetch.py     URL → clean text
search.py    query → merged results from N backends
.cache/      disk cache (url sha1 → text)
```

## Design notes

- **Tier 1 (this):** requests + browser headers. Covers most of the web.
- **Tier 2 (not yet):** prefer machine-readable endpoints — RSS/Atom,
  `sitemap.xml`, `?format=json`, JSON backing APIs.
- **Tier 3 (not yet):** headless browser (`chromium --headless
  --dump-dom`) only when tier 1 returns 403/429 or an empty body.
- No cookies, no JS, no stealth. If a site needs those, it's a tier-2/3
  problem, not a fetcher problem.
