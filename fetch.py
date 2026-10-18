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


def fetch_url(url, session, timeout, max_chars, use_cache=True,
              allow_private=False):
    """Fetch url and return a result dict; text is truncated to max_chars.

    The cache holds the full text, so max_chars only shapes the output.
    Unless allow_private, a private/non-http URL or redirect raises FetchError.
    """
    if not allow_private:
        # Before the cache lookup: an entry fetched with allow_private must
        # not be served to a caller that did not opt in.
        why = common.refusal(url)
        if why:
            raise common.FetchError(f"{url} refused: {why}")
    if use_cache:
        cached = common.cache_get(url)
        if cached is not None:
            return {"url": cached["url"], "requested": url, "source": "cache",
                    "title": cached["title"],
                    "content_type": cached["content_type"],
                    "status": cached["status"],
                    "text": common.truncate(cached["text"], max_chars)}

    final_url, status, headers, body = common.http_get(
        url, session=session, timeout=timeout, allow_private=allow_private)
    ctype = common.content_type(headers)
    title, text = common.body_to_text(final_url, status, headers, body)
    if use_cache:
        common.cache_set(url, {"url": final_url, "title": title,
                               "content_type": ctype, "status": status,
                               "text": text})
    return {
        "url": final_url,
        "requested": url,
        "source": "live",
        "title": title,
        "content_type": ctype,
        "status": status,
        "text": common.truncate(text, max_chars),
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
                    help="allow loopback/RFC1918/link-local hosts (refused by default, "
                         "including via redirects)")
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
        try:
            result = fetch_url(url, session, timeout, args.max_chars, use_cache,
                               allow_private=args.allow_private)
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
