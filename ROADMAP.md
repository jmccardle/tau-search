# tau-search roadmap

**Version:** `ffwf-tau-search` 0.1.0, released 2026-10-18 on PyPI and TestPyPI. Tag `v0.1.0` is at d7c698a.
**Branch:** `master`. The remotes are `origin` (dev.ffwf.net) and `github` (public github.com/jmccardle/tau-search).
**Target:** a pip-installable Tau extension for tau 0.14.0, shipped alongside tau-router.

## Shipped

- **0.1.0 packaging** (merged 2026-10-18 after FFwF_Robotics/tectum#3 landed as tectum fceff60)
  - **Layout:** a src package, `tau_search`. The public modules are `tau_search.common`, `tau_search.fetch` and `tau_search.search`, plus `tau_search.presearch`.
  - **Commands:** `tau-fetch`, `tau-web-search`, `presearch`.
  - **Cache:** `common.cache_dir()` uses `TAU_SEARCH_CACHE`, else `$XDG_CACHE_HOME/tau-search`, else `~/.cache/tau-search`. `CACHE_DIR` and `HERE` are gone.
  - **Extension `tau_search.ext.search`:**
    - Async `web_search` and `web_fetch` tools with structured output.
    - Output is marked untrusted.
    - `web_fetch` never allows private hosts.
    - On abort, the tools raise `CancelledError`.
  - **Entry point:** `tau.extensions: search = "tau_search.ext.search"`, provisional on tau#35. For now, load it with single-file `-e`.
  - **Verified 2026-10-18:**
    - A fresh-venv install takes all dependencies from PyPI (tau 0.13.0).
    - 75 tests and `tests/check_loader.py` pass.
    - `ffwf-tau` 0.13.0 ran headless on `claude-haiku-5-5` and called both tools.
    - `docker build` passes.
  - Gotcha: `--tools web_fetch` fails with `ValueError: Unknown tool: web_fetch` (`sdk.py:243`). The allowlist is resolved before extensions load, so use `--no-builtin-tools`. Filed as tau#43 (milestone 0.14.0).
- **Fetch layer** (merge ee08560)
  - Full-text JSON cache with atomic writes.
  - Private hosts are refused, including every redirect hop and the cached final URL.
  - Malformed URLs raise `FetchError`.
- **presearch, matched to jmfts 0.7.0** (merge b68678b)
  - Uses `PUT /triples` and refuses literal triples (jmfts#48).
  - Adds `--topics-only`, dedup and an explicit `index_name`.
  - Limits must be 1..100.
  - `discover --accept` refuses before any request.
- **Bundled appliance** (merge b65a9e6)
  - `jmfts==0.7.0` pinned.
  - Pending migrations fail loudly (jmfts#50).
  - Python healthcheck, token required, registry usetypes excluded.
- **Default test model:** `claude-haiku-5-5`. No tau-search script, test or doc names a model.

## Open decisions (John)

- **`pypi` environment protection:** the GitHub environments were auto-created on first use without rules. Suggested: John as required reviewer, plus a `v*` deployment-tag rule.

## Decided

- **search-pypi** (2026-10-18): published with the history as is, after the public-readiness audit (no secrets).
  - `v0.1.0` was moved to d7c698a so the tag contains `.github/workflows/publish.yml`.
  - TestPyPI rehearsal: run 39172673064. PyPI tag push: run 39172826560. Both were green on py3.11–3.14, and both indexes install cleanly.
  - Publishing uses trusted publishers (OIDC) for `jmccardle/tau-search`, `publish.yml`, with environments `pypi` and `testpypi`.
- **search-public:** moot now that the GitHub repo is public. dev.ffwf.net stays the working remote.

- **tau#35(a)** (2026-10-18): drop-ins and entry-point packages go through one authorization gate, and existing drop-ins are grandfathered.
  - The identifier is the full dotted module name.
  - In 0.14.0, config becomes `tau_search.ext.search:KEY=VALUE`, replacing the stem key `search`. Topics become `ext:tau_search.ext.search:<topic>`.

## External blockers

- **tau#35:** entry-point loading and authorization. The config key changes in 0.14.0.
- **tau#40:** `extension_owner()` mangles package-directory labels. Single-file `-e` is the only documented path until it's fixed.
- **tau#43:** `--tools` can't name extension tools. Use `--no-builtin-tools` until it's fixed.
- **jmfts#48:** no literal triples over REST. This blocks presearch `load` for literal predicates.
- **jmfts#50:** `jmfts-init-db` isn't idempotent. Compose fails loudly on pending migrations.
- **jmfts#49, #51–#55:** tracked upstream, non-blocking.

## Next

- Run a live appliance test of the presearch CLI. This has never been run end to end.
- When tau 0.14.0 lands, switch from `-e` to entry-point loading and update the config key docs.
