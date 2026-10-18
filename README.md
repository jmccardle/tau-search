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
- Private/loopback/non-http(s) URLs are refused (a failure, exit 1) unless
  `--allow-private`; every redirect hop is checked too, so a public URL
  cannot bounce you to 127.0.0.1 or 169.254.169.254. `common.http_get` and
  `fetch.fetch_url` take `allow_private=False` and enforce the same.
- Successful fetches cached in `.cache/` (JSON, keyed by URL sha1) as full
  text with title/metadata; `--max-chars` truncates on output only.

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

## presearch — federated search over a JMFTS appliance

`search.py` is **metasearch**: broadcast a query to every backend, merge the
results. `presearch` is **federated search**: decide *which* sources to ask
first, and ask those. The difference is a registry — a map from topics to
sources, which the broker uses for resource selection.

The registry is not a new database. It lives in a
[JMFTS](https://pypi.org/project/jmfts/) appliance as a held-out corpus:
topics are a document tree, sources hang beneath them, and facts about a
source (search endpoint, subreddit, Stack Exchange tag) are triples with
temporal validity, so a source that moves is superseded rather than
overwritten.

```sh
presearch check                                   # appliance reachable?
presearch fetch curlie                            # 177 MB → 619 MB unpacked
presearch load curlie --topic-prefix Computers --min-entries 5 --max-depth 4
presearch discover --topic Computers/Software/Databases/SQLite
presearch validate                                # still resting on upstream?
presearch query "sqlite b-tree internals"
```

### Providers

| name | kind | what it contributes |
|---|---|---|
| `curlie` | bulk | live human-edited directory; 801,720 categories, 2,739,037 sites, monthly |
| `dmoz` | bulk | the 2017 ODP terminal snapshot; pre-2015 forums and newsgroups Curlie pruned |
| `wikidata` | bulk | *handles*, not homepages — subreddit, SE tag, repo, bug tracker |
| `marginalia` | discovery | no dump; queried per-topic to harvest candidate domains |

The bulk/discovery split is load-bearing. Bulk providers populate a registry
from nothing. Discovery providers extend one that already has a taxonomy —
which is how the registry self-populates from its own fallback tier: the
broker misses, the open web answers, the answer becomes a registered source.
Candidates are never auto-trusted; `--accept` is the curation gate.

### Validation

Every load writes a **manifest** recording the upstream URL, its version
(ETag / Last-Modified / dump date / result count) and a digest of the bytes
actually parsed. `presearch validate` re-derives that stamp and reports
`CURRENT` / `DRIFTED` / `UNVERIFIED` / `UNREACHABLE` / `DEGRADED`, and
`presearch query` runs it before answering.

A broker routing queries against a silently stale index gives wrong answers
that look right. Drift is reported, not absorbed.

`DEGRADED` is the separate failure: a directory ages by *rot*, not by edit.
The dump can be perfectly current while a third of what it lists is gone —
the `Computers` slice is full of live entries for conferences that ended in
1998 — so `validate` also liveness-checks a sample of registered URLs.

### Storage

The registry loads with `auto_embed=False`. Matching a query to a category
label is lexical work, and embedding 2.7M directory stubs would dominate the
appliance for no measured gain. The `Computers` subtree at
`--min-entries 5 --max-depth 4` is 1,448 topics and 19,295 sources.

### Container

```sh
docker compose run --rm presearch check                 # against your appliance
docker compose --profile appliance up -d                # or bring one up locally
```

## Layout

```
common.py    shared http fetch, html→text, cache, SSRF guard
fetch.py     URL → clean text
search.py    query → merged results from N backends
.cache/      disk cache (url sha1 → text)

presearch/
  record.py      normalized Topic/Source/Upstream/Manifest
  providers/     curlie, dmoz, wikidata (bulk); marginalia (discovery)
  jmfts.py       REST client, with a preflight that fails before a load
  registry.py    normalized records → JMFTS documents + triples
  validate.py    does the registry still rest on its upstream?
  broker.py      query → tiered routing plan (no network, deterministic)
  cli.py         presearch <check|fetch|load|discover|validate|query|reindex>
```

## Design notes

- **Tier 1 (this):** requests + browser headers. Covers most of the web.
- **Tier 2 (not yet):** prefer machine-readable endpoints — RSS/Atom,
  `sitemap.xml`, `?format=json`, JSON backing APIs.
- **Tier 3 (not yet):** headless browser (`chromium --headless
  --dump-dom`) only when tier 1 returns 403/429 or an empty body.
- No cookies, no JS, no stealth. If a site needs those, it's a tier-2/3
  problem, not a fetcher problem.
