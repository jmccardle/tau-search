# tau-search roadmap

**Version:** untagged, no packaging on `master`. `ffwf-tau-search` 0.1.0 is on an unmerged branch.
**Branch:** `master` @ b65a9e6. Approved and unmerged: `worktree-agent-afab9f112638baebd` @ 76d0b54.
**Target:** a pip-installable Tau extension for tau 0.14.0, shipped alongside tau-router.

## Shipped (on master)

- **Fetch layer** (merge ee08560)
  - The cache stores full text as JSON and truncates on output. Writes are atomic.
  - Corrupt cache entries raise `FetchError`.
  - Private hosts are refused, including every redirect hop and the cached final URL. The `fetch.py` CLI opts in with `--allow-private`.
- **presearch, matched to jmfts 0.7.0** (merge b68678b)
  - Reads subtree `descendants` and capabilities from `retrieval.methods`.
  - Writes triples with `PUT /triples` and refuses literal triples (jmfts#48).
  - Adds `load --topics-only`, dedup on write and an explicit `index_name`.
  - Limits outside 1..100 are refused, not clamped.
  - Only GET/HEAD/PUT retry.
  - `discover --accept` refuses before any network request.
- **Bundled appliance** (merge b65a9e6)
  - `jmfts==0.7.0` pinned.
  - Restart-safe init: a pending migration fails loudly (jmfts#50).
  - Python healthcheck, no reload.
  - Token required.
  - registry usetypes are excluded from search.

## In progress (approved, not merged)

Branch `worktree-agent-afab9f112638baebd`:

- **Layout:** a src package, `tau_search`, distributed as `ffwf-tau-search` 0.1.0.
- **Commands:** `tau-fetch`, `tau-web-search`, `presearch`.
- **Cache:** moved to XDG (`TAU_SEARCH_CACHE`, else `$XDG_CACHE_HOME/tau-search`).
- **Extension `tau_search.ext.search`:**
  - Async `web_search` and `web_fetch` tools with structured output.
  - Output is marked untrusted.
  - `web_fetch` never allows private hosts.
  - On abort, the tools raise `CancelledError`.
- **Entry point:** `tau.extensions: search = "tau_search.ext.search"`, provisional on tau#35.
- **Tests:** 75 pass, and tau 0.13.0's real loader registers both tools (`tests/check_loader.py`).

## Open decisions (John)

- **Merge order vs tectum.** The merge breaks `tectum/shim_cli.py:194-208`, which imports bare `common`/`fetch`/`search` from a checkout.
  - Option A: merge now and fix tectum separately.
  - Option B: make the tectum change first, then merge. **Recommended.**
  - The tectum change is small: `from tau_search import common, fetch, search`, and drop `TECTUM_TAU_SEARCH_DIR`.
- **tau#35(a):** whether directory drop-in extensions go through the same authorization as entry-point packages. I argued yes; tau-router argued for auto-load.

## External blockers

- **ffwf-tau-agent-core 0.13.0 on PyPI.** Only 0.12.0 is published. This blocks the Docker build and a clean `pip install`.
- **tau#35:** entry-point loading and authorization. Until it lands, the extension loads through single-file `-e`, and the config key `search` may change.
- **tau#40:** `extension_owner()` mangles package-directory labels. Until it's fixed, single-file `-e` is the only documented path.
- **jmfts#48:** no literal triples over REST. This blocks presearch `load` for literal predicates.
- **jmfts#50:** `jmfts-init-db` isn't idempotent. Compose fails loudly on pending migrations until it's fixed.
- **jmfts#49, #51–#55:** filed and tracked upstream. None blocks a merge.

## Next

- Merge the packaging branch and remove its worktree, once the tectum decision is made.
- Tag 0.1.0 once tau 0.13.0 is on PyPI and the Docker build passes.
- Run a live appliance test of the presearch CLI. This has never been run end to end.
- Switch from `-e` to entry-point loading when tau#35 lands.
