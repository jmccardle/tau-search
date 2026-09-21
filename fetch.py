#!/usr/bin/env python3
"""tau-search fetch: fetch a URL and print clean, truncated text.

Usage:
  python3 fetch.py URL [URL ...]
  python3 fetch.py --no-cache --max-chars 4000 --show-meta URL
  python3 fetch.py --json URL        # machine-readable output
"""

import argparse
import json
import sys

import common


def fetch_url(url, session, timeout, max_chars, use_cache=True):
    if use_cache:
        cached = common.cache_get(url)
        if cached is not None:
            return {"url": url, "source": "cache",
                    "text": common.truncate(cached, max_chars)}

    final_url, status, headers, body = common.http_get(url, session=session,
                                                       timeout=timeout)
    ctype = common.content_type(headers)
    title, text = common.body_to_text(final_url, status, headers, body)
    text = common.truncate(text, max_chars)
    if use_cache:
        common.cache_set(url, text)
    return {
        "url": final_url,
        "requested": url,
        "source": "live",
        "title": title,
        "content_type": ctype,
        "status": status,
        "text": text,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", metavar="URL")
    ap.add_argument("--max-chars", type=int, default=8000,
                    help="truncate output text to this many chars (default 8000, 0=off)")
    ap.add_argument("--no-cache", action="store_true",
                    help="neither read from nor write to the disk cache")
    ap.add_argument("--allow-private", action="store_true",
                    help="fetch loopback/RFC1918/link-local hosts without a warning")
    ap.add_argument("--timeout", type=float, default=None,
                    help="timeout in seconds (default: 10s connect, 25s read)")
    ap.add_argument("--show-meta", action="store_true",
                    help="print status/content-type header block before each result")
    ap.add_argument("--json", action="store_true", help="output JSON instead of text")
    args = ap.parse_args(argv)

    session = common.build_session()
    timeout = (args.timeout, args.timeout) if args.timeout else common.DEFAULT_TIMEOUT
    use_cache = not args.no_cache

    failures = 0
    for url in args.urls:
        host = common.urlsplit(url).hostname
        if common.is_private_host(host) and not args.allow_private:
            print(f"[warn] {url}: private/loopback host {host!r} "
                  f"(pass --allow-private to silence)", file=sys.stderr)
        try:
            result = fetch_url(url, session, timeout, args.max_chars, use_cache)
        except common.FetchError as e:
            failures += 1
            if args.json:
                print(json.dumps({"requested": url, "error": str(e)}))
            else:
                print(f"[error] {url}: {e}", file=sys.stderr)
            continue

        if args.json:
            print(json.dumps(result, ensure_ascii=False))
            continue

        print(f"===== {result['url']} =====")
        if args.show_meta:
            print(f"status: {result.get('status')}  "
                  f"content-type: {result.get('content_type')}  "
                  f"source: {result['source']}")
        if result.get("title"):
            print(f"title: {result['title']}")
        print()
        print(result["text"])
        print()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
