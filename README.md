# tau-search

Local web access for agents. Tier-1 first: plain HTTP with browser-like
headers, hard timeouts, size caps, and a small disk cache. Escalate to a
headless browser only when tier 1 genuinely fails.

## Setup

```sh
pip install ffwf-tau-search        # from PyPI
pip install -e .                   # or from a checkout
```

Requires Python 3.11+. This installs three console scripts (`tau-fetch`,
`tau-web-search`, `presearch`) and a Tau extension (below). The package depends
on `ffwf-tau-agent-core>=0.13.0` for the extension.

The disk cache lives in `$TAU_SEARCH_CACHE`, else `$XDG_CACHE_HOME/tau-search`,
else `~/.cache/tau-search`.

To run the tests from a checkout:

```sh
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests
```

## Fetch a URL

```sh
tau-fetch https://docs.python.org/3/whatsnew/3.11.html
tau-fetch --max-chars 2000 --show-meta URL
tau-fetch --json URL        # machine-readable
tau-fetch --no-cache URL    # skip disk cache
tau-fetch --allow-private http://127.0.0.1:8080/
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
- Successful fetches cached in the cache directory (see Setup) (JSON, keyed by URL sha1) as
  full text with title/metadata; `--max-chars` truncates on output only.

## Search

```sh
tau-web-search "rust async tokio tutorial"
tau-web-search "rust async" --backends ddg,mojeek --limit 5
tau-web-search "searxng" --json
tau-web-search "query" --searxng-url http://127.0.0.1:8080
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
SEARXNG_URL=http://127.0.0.1:8080 tau-web-search "query"
# or: --searxng-url http://127.0.0.1:8080
```

## Tau extension

`tau_search.ext.search` registers two tools with [Tau](https://ffwfrobotics.github.io/tau/)
(0.13.0 or later):

| tool | what it does |
|---|---|
| `web_search(query, limit)` | keyless metasearch (the `tau-web-search` backends); returns title, url, snippet and backend per hit, plus per-backend errors |
| `web_fetch(url, max_chars)` | fetch a URL as clean text; returns url, title, content type, status, `source` (`live`/`cache`), text and `truncated` |

`web_fetch` only fetches public http(s) hosts. Private, loopback and
link-local targets are refused, including via redirects, and no config key or
parameter turns that off. Fetched text and search snippets reach the model
under a header marking them as untrusted external content: data, not
instructions. Tau has no untrusted-content policy of its own, so that marking
is this extension's job.

**Loading it (interim).** Tau 0.13.0 loads extensions by file path. Until
tau#35 ships entry-point loading in tau 0.14.0, point `-e` at the installed
file:

```sh
tau -e "$(python -c 'import tau_search.ext.search as m; print(m.__file__)')"
```

Use the single-file form: in 0.13.0 the package-directory form gets a mangled
extension name (a reported tau bug). The package also declares a
`tau.extensions` entry point (`search = "tau_search.ext.search"`); Tau ignores
it until that issue lands.

Under tau#35 an extension's identifier is its full Python module name, here
`tau_search.ext.search`. Users will authorize it by distribution name and
version (`ffwf-tau-search` and its version), and an update needs authorizing
again. That arrives in tau 0.14.0.

**Config** goes under `"extensions": {"search": {...}}` in `~/.tau/config.json`.
That key is the file stem, which is what the interim `-e` single-file path
uses. Under #35 the identifier becomes the module name, so the config key may
change when tau 0.14.0 ships; check its release notes.
Every key is optional and the defaults live in code; unknown keys and bad
values fail the extension at load.

```json
{
  "extensions": {
    "search": {
      "backends": ["ddg", "wikipedia"],
      "searxng_url": "http://127.0.0.1:8080",
      "limit": 5,
      "max_chars": 8000
    }
  }
}
```

| key | default | meaning |
|---|---|---|
| `backends` | all, in `tau-web-search` order | which search backends to use |
| `searxng_url` | empty | local SearXNG base URL, tried first by the `searxng` backend |
| `limit` | 5 | default max results per backend (a tool-call argument overrides) |
| `max_chars` | 8000 | default cap on fetched text length (a tool-call argument overrides) |

## presearch — federated search over a JMFTS appliance

`tau-web-search` is **metasearch**: broadcast a query to every backend, merge the
results. `presearch` is **federated search**: decide *which* sources to ask
first, and ask those. The difference is a registry — a map from topics to
sources, which the broker uses for resource selection.

The registry is not a new database. It lives in a
[JMFTS](https://pypi.org/project/jmfts/) 0.7.0 appliance as a held-out corpus:
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

### Appliance setup and known gaps

**Hold the registry out of ordinary search.** Registry rows are *not* held out
by default: JMFTS's `search_exclude_usetypes` defaults to `entity`, `entities`,
`summary`, `derived` (and `bm25_exclude_usetypes` adds `cell`). To keep
registry rows from ranking against real content, set on the appliance, keeping
the defaults:

```sh
JMFTS_SEARCH_EXCLUDE_USETYPES='["entity","entities","summary","derived","registry:root","registry:topic","registry:source","registry:manifest"]'
```

The value is a JSON array (pydantic-settings parses a `list[str]` from JSON),
and entries are matched **exactly**, not as globs, so `registry:*` does not
work. Do **not** add them to `JMFTS_BM25_EXCLUDE_USETYPES`: that is applied
when a BM25 index is built and would leave the `presearch` index empty.
presearch's own queries name the usetype, and a positive usetype filter
overrides the exclusion list, so the broker is unaffected. Whether the
appliance should hold out `registry:*` itself is an open upstream design
question (jmfts#52).

**Literal facts are not loadable yet.** `search_endpoint`, `subreddit`,
`stack_exchange_tag` and the other literal-valued facts need literal triples,
which JMFTS 0.7.0 cannot create over REST (`TripleCreate` requires
`object_id`; jmfts#48). Every current provider attaches them to its sources,
so `presearch load` and `discover --accept` refuse, before writing anything,
until that lands. `presearch load <provider> --topics-only` loads the taxonomy
alone.

### Container

```sh
# One shared secret for client and server (empty makes JMFTS mint a throwaway
# token each boot, so every client call 401s). Both sides refuse to start without it.
export JMFTS_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

docker compose run --rm presearch check                 # against your appliance (set JMFTS_API_URL and its token)
docker compose --profile appliance up -d                # or bring one up locally
```

The local appliance initialises its schema once, applies any pending jmfts
migrations on later starts, and survives `down`/`up`; `down -v` wipes the data.

## Layout

```
pyproject.toml
src/tau_search/
  common.py    shared http fetch, html→text, cache, SSRF guard
  fetch.py     URL → clean text                    (tau-fetch)
  search.py    query → merged results from N backends  (tau-web-search)
  ext/
    search.py  Tau extension: web_search, web_fetch
  presearch/                                       (presearch)
    record.py      normalized Topic/Source/Upstream/Manifest
    providers/     curlie, dmoz, wikidata (bulk); marginalia (discovery)
    jmfts.py       REST client, with a preflight that fails before a load
    registry.py    normalized records → JMFTS documents + triples
    validate.py    does the registry still rest on its upstream?
    broker.py      query → tiered routing plan (no network, deterministic)
    cli.py         presearch <check|fetch|load|discover|validate|query|reindex>
tests/             stdlib unittest; check_loader.py drives tau's real loader
```

## Design notes

- **Tier 1 (this):** requests + browser headers. Covers most of the web.
- **Tier 2 (not yet):** prefer machine-readable endpoints — RSS/Atom,
  `sitemap.xml`, `?format=json`, JSON backing APIs.
- **Tier 3 (not yet):** headless browser (`chromium --headless
  --dump-dom`) only when tier 1 returns 403/429 or an empty body.
- No cookies, no JS, no stealth. If a site needs those, it's a tier-2/3
  problem, not a fetcher problem.
