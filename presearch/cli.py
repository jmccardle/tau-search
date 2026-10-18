"""presearch -- build, validate and query a federated-search source registry.

    presearch fetch curlie                    download an upstream corpus
    presearch load curlie --topic-prefix Computers --max-sources 5000
    presearch discover --topic Computers/Algorithms --accept
    presearch validate                        does the registry still hold?
    presearch query "sqlite b-tree internals"
    presearch check                           appliance reachable and capable?

Every command that touches JMFTS takes --jmfts and --token, falling back to
JMFTS_API_URL / JMFTS_API_TOKEN.
"""

import argparse
import itertools
import os
import sys

from . import broker as broker_mod
from . import providers
from . import validate as validate_mod
from .jmfts import DEFAULT_TOKEN, DEFAULT_URL, Jmfts, JmftsError
from .record import Manifest
from .registry import Registry

DEFAULT_DATA = os.environ.get("PRESEARCH_DATA", "./data")


def _progress(label):
    state = {"last": 0}

    def report(got, total):
        if total and got - state["last"] < total // 20:
            return
        state["last"] = got
        pct = f"{got * 100 // total}%" if total else f"{got // (1 << 20)} MB"
        print(f"\r{label}: {pct}", end="", file=sys.stderr, flush=True)

    return report


def _max_100(value):
    """argparse type: the server caps SearchRequest.limit at 100; refuse more
    rather than silently return fewer results than asked for."""
    n = int(value)
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and 100 (JMFTS search limit), got {n}")
    return n


def _client(args):
    return Jmfts(base_url=args.jmfts, token=args.token)


def _registry(args, preflight=True):
    client = _client(args)
    if preflight:
        client.preflight()
    reg = Registry(client, verbose=not args.quiet)
    reg.ensure_root()
    return reg


def _provider(args, name):
    cls = providers.get(name)
    kwargs = {"data_dir": args.data_dir}
    for attr in ("min_entries", "topic_prefix", "max_depth"):
        if hasattr(args, attr) and getattr(args, attr) is not None:
            kwargs[attr] = getattr(args, attr)
    if name == "marginalia":
        kwargs = {"data_dir": args.data_dir}
        if getattr(args, "key", None):
            kwargs["key"] = args.key
        if getattr(args, "api2", False):
            kwargs["use_current"] = True
    try:
        return cls(**kwargs)
    except TypeError:
        return cls(data_dir=args.data_dir)


# ---- commands ------------------------------------------------------------

def cmd_check(args):
    client = _client(args)
    caps = client.preflight()
    print(f"JMFTS at {args.jmfts}: reachable")
    # CapabilitiesResponse: version, retrieval{methods, *_exclude_usetypes}.
    retrieval = caps.get("retrieval", {})
    print(f"  version: {caps.get('version')}")
    print(f"  retrieval methods: {retrieval.get('methods')}")
    excluded = retrieval.get("search_exclude_usetypes") or []
    print(f"  search_exclude_usetypes: {excluded}")
    if not any(u.startswith("registry:") for u in excluded):
        print("  note: registry:* usetypes are not held out of ordinary "
              "search; see the README presearch section.")
    print("\nNote: the registry loads with auto_embed=False and needs only "
          "bm25/fulltext.")
    return 0


def cmd_fetch(args):
    provider = _provider(args, args.provider)
    if not hasattr(provider, "fetch"):
        raise SystemExit(f"{args.provider} has nothing to download")
    up = provider.fetch(force=args.force, progress=_progress(args.provider))
    print(file=sys.stderr)
    print(f"{up.provider}: version={up.version or '(none)'} "
          f"digest={up.digest[:16] or '(none)'}")
    print(f"data in {args.data_dir}")
    return 0


def cmd_load(args):
    provider = _provider(args, args.provider)
    if getattr(provider, "kind", "bulk") != "bulk":
        raise SystemExit(
            f"{args.provider} is a discovery provider; use `presearch discover`")

    # Literal facts cannot be written yet (jmfts#48). Look at the first source
    # before anything is written (even the registry skeleton), so a refused
    # load leaves nothing behind. load_sources re-checks every later source.
    sources = None
    if not args.topics_only:
        sources = iter(provider.sources())
        first = next(sources, None)
        if first is not None:
            Registry.check_loadable(first)
            sources = itertools.chain([first], sources)

    reg = _registry(args)
    reg.ensure_index()

    upstream = provider.upstream()
    if not upstream.version and not upstream.digest:
        raise SystemExit(
            f"{args.provider}: no upstream version or digest available. "
            "Run `presearch fetch` first -- a registry without provenance "
            "cannot be validated, so it is not loaded.")

    reg.log(f"loading topics from {args.provider} ...")
    n_topics = reg.load_topics(provider.topics())
    reg.log(f"topics: {n_topics} new")

    selector = ""
    for attr in ("topic_prefix", "min_entries", "max_depth", "max_sources"):
        val = getattr(args, attr, None)
        if val:
            selector += f"{attr}={val} "

    manifest = Manifest(upstream=upstream, root_id=reg.root_id,
                        topics_loaded=n_topics, sources_loaded=0,
                        selector=selector.strip())
    manifest_id = reg.write_manifest(manifest)

    if args.topics_only:
        stats = {"created": 0, "skipped_no_topic": 0, "failed": 0}
    else:
        reg.log("loading sources ...")
        stats = reg.load_sources(sources, manifest_id, workers=args.workers,
                                 max_sources=args.max_sources)
        reg.log(f"sources: {stats['created']} created, "
                f"{stats['skipped_no_topic']} skipped (topic filtered out), "
                f"{stats['failed']} failed")

    # The manifest was written before sources (their triples cite its id);
    # now that the real count is known, update that one document in place.
    reg.update_manifest(manifest_id, Manifest(
        upstream=upstream, root_id=reg.root_id, topics_loaded=n_topics,
        sources_loaded=stats["created"], selector=selector.strip()))

    if args.refresh_index:
        reg.log("refreshing bm25 index (rebuilds the whole index) ...")
        reg.j.index_refresh("presearch")
    else:
        print("\nIndex NOT refreshed. Run `presearch reindex` when loads are "
              "done -- a refresh rebuilds the entire index.", file=sys.stderr)
    return 0


def cmd_discover(args):
    provider = _provider(args, "marginalia")
    found = list(provider.discover(args.topic, limit_domains=args.limit))
    if not found:
        print("no candidate domains met the hit threshold")
        return 1
    if args.accept:
        # jmfts#48: refuse before _registry(), which would create the root
        # skeleton. Marginalia sources always carry home_url, so this fires.
        for src in found:
            Registry.check_loadable(src)

    reg = _registry(args)
    reg._load_existing_topics()
    if args.topic not in reg._topic_ids:
        raise SystemExit(
            f"topic {args.topic!r} is not in the registry; load a taxonomy "
            "first (`presearch load curlie --topic-prefix ...`)")

    for src in found:
        print(f"  {src.url}\n      {src.title}\n      {src.description[:160]}")

    if not args.accept:
        print(f"\n{len(found)} candidates. Re-run with --accept to register "
              "them. Marginalia results are CC-BY-NC-SA 4.0.", file=sys.stderr)
        return 0

    manifest = Manifest(upstream=provider.upstream(), root_id=reg.root_id,
                        topics_loaded=0, sources_loaded=len(found),
                        selector=f"discover topic={args.topic}")
    manifest_id = reg.write_manifest(manifest)
    stats = reg.load_sources(iter(found), manifest_id, workers=4)
    print(f"registered {stats['created']} source(s) under {args.topic}")
    return 0


def cmd_validate(args):
    reg = _registry(args, preflight=False)
    checks, ok = validate_mod.run(reg, args.data_dir, sample=args.sample,
                                  tolerate=args.tolerate)
    for c in checks:
        print(c.line())
    if not ok:
        print("\nregistry is NOT current", file=sys.stderr)
    return 0 if ok else 1


def cmd_query(args):
    reg = _registry(args, preflight=False)
    p = broker_mod.plan(reg, args.query, limit=args.limit, method=args.method,
                        validate_first=not args.no_validate,
                        data_dir=args.data_dir, tiers=args.tiers)
    print(p.to_json() if args.json else p.render())
    if p.warnings and not args.no_validate:
        return 1
    return 0


def cmd_reindex(args):
    reg = _registry(args)
    reg.ensure_index()
    reg.j.index_refresh("presearch")
    print("presearch index refreshed")
    return 0


# ---- argument parsing ----------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="presearch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jmfts", default=DEFAULT_URL, help="JMFTS base URL")
    p.add_argument("--token", default=DEFAULT_TOKEN, help="JMFTS API token")
    p.add_argument("--data-dir", default=DEFAULT_DATA,
                   help="where upstream corpora are stored")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", help="is the appliance reachable and capable?")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("fetch", help="download an upstream corpus")
    s.add_argument("provider", choices=sorted(providers.ALL))
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("load", help="load a corpus into JMFTS")
    s.add_argument("provider", choices=sorted(providers.BULK))
    s.add_argument("--topic-prefix", help="only this subtree, e.g. Computers")
    s.add_argument("--min-entries", type=int, default=0,
                   help="skip categories listing fewer than N sites")
    s.add_argument("--max-depth", type=int, help="skip topics deeper than N")
    s.add_argument("--max-sources", type=int, default=0,
                   help="stop after N sources (0 = no limit; see the warning "
                        "in registry.load_sources before using 0 on Curlie)")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--topics-only", action="store_true",
                   help="load the taxonomy without sources (sources are "
                        "refused while literal facts are unwritable, jmfts#48)")
    s.add_argument("--refresh-index", action="store_true")
    s.set_defaults(func=cmd_load)

    s = sub.add_parser("discover", help="harvest candidate sources for a topic")
    s.add_argument("--topic", required=True)
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--key", default="public", help="Marginalia API key")
    s.add_argument("--api2", action="store_true", help="use api2 endpoint")
    s.add_argument("--accept", action="store_true",
                   help="register the candidates instead of just listing them")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("validate", help="check the registry against upstream")
    s.add_argument("--sample", type=_max_100, default=25,
                   help="source URLs to liveness-check (0 to skip)")
    s.add_argument("--tolerate", type=float, default=0.4,
                   help="dead fraction above which the registry is DEGRADED")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("query", help="build a routing plan")
    s.add_argument("query")
    s.add_argument("--limit", type=_max_100, default=10)
    s.add_argument("--method", default="bm25",
                   choices=["bm25", "fulltext", "hybrid", "vector"])
    s.add_argument("--tiers", type=int, default=3)
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-validate", action="store_true",
                   help="skip the upstream check (results may rest on a "
                        "drifted index)")
    s.set_defaults(func=cmd_query)

    s = sub.add_parser("reindex", help="rebuild the presearch bm25 index")
    s.set_defaults(func=cmd_reindex)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except JmftsError as e:
        print(f"jmfts: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
