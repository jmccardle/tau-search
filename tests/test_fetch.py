"""fetch_url / http_get: cache, truncation, SSRF guard. No real network.

Run: ./venv/bin/python -m unittest -v
"""

import io
import os
import json
import socket
import tempfile
import unittest
from unittest import mock

import requests
from requests.adapters import BaseAdapter

from tau_search import common
from tau_search import fetch
from tau_search import search

HOSTS = {"public.test": "93.184.216.34", "other.test": "93.184.216.35",
         "evil.test": "127.0.0.1", "metadata.test": "169.254.169.254"}
HTML = ("<html><head><title>T</title></head><body><article><p>"
        + "word " * 100 + "</p></article></body></html>")


def _getaddrinfo(host, *a, **kw):
    ip = HOSTS.get(host, host)  # literal IPs resolve to themselves
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


class FakeAdapter(BaseAdapter):
    """Serves routes {url: (status, headers, body)}; records requested URLs."""

    def __init__(self, routes):
        super().__init__()
        self.routes, self.seen = routes, []

    def send(self, request, **kwargs):
        self.seen.append(request.url)
        status, headers, body = self.routes[request.url]
        resp = requests.Response()
        resp.status_code = status
        resp.headers = requests.structures.CaseInsensitiveDict(headers)
        resp.raw, resp.url, resp.request = io.BytesIO(body), request.url, request
        return resp

    def close(self):
        pass


def _session(routes):
    s = common.build_session()
    s.adapters.clear()
    adapter = FakeAdapter(routes)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s, adapter


def _page(url="http://public.test/"):
    return {url: (200, {"Content-Type": "text/html"}, HTML.encode())}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.dict(os.environ, {"TAU_SEARCH_CACHE": tmp.name}),
                  mock.patch.object(common.socket, "getaddrinfo", _getaddrinfo)):
            p.start()
            self.addCleanup(p.stop)


class Cache(Base):
    def test_hit_returns_full_text_truncated_per_call(self):
        s, adapter = _session(_page())
        live = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 50)
        self.assertEqual(live["source"], "live")
        self.assertIn("truncated at 50", live["text"])
        hit = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)
        self.assertEqual(hit["source"], "cache")
        self.assertEqual(len(adapter.seen), 1)
        self.assertNotIn("truncated", hit["text"])
        self.assertGreater(len(hit["text"]), 400)
        small = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 20)
        self.assertTrue(small["text"].startswith(hit["text"][:20]))
        self.assertIn("truncated at 20", small["text"])

    def test_hit_has_same_keys_as_live(self):
        s, _ = _session(_page())
        live = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)
        hit = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)
        self.assertEqual(live["source"], "live")
        self.assertEqual(hit["source"], "cache")
        self.assertEqual(set(live), set(hit))
        for k in ("url", "requested", "title", "content_type", "status", "text"):
            self.assertEqual(live[k], hit[k])
        self.assertEqual(hit["title"], "T")

    def test_small_max_chars_does_not_poison_later_fetch(self):
        s, _ = _session(_page())
        fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 10)
        big = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)
        self.assertEqual(big["source"], "cache")
        self.assertNotIn("truncated", big["text"])
        self.assertEqual(big["text"].count("word"), 100)

    def test_old_txt_entries_are_misses(self):
        old = common._cache_path("http://public.test/")[:-len(".json")] + ".txt"
        with open(old, "w") as f:
            f.write("stale truncated text")
        s, _ = _session(_page())
        r = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)
        self.assertEqual(r["source"], "live")

    def test_corrupt_entry_fails(self):
        with open(common._cache_path("http://public.test/"), "w") as f:
            f.write("{not json")
        s, _ = _session(_page())
        with self.assertRaises(common.FetchError):
            fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)

    def test_private_cache_entry_not_served_without_opt_in(self):
        url = "http://evil.test/"
        s, _ = _session(_page(url))
        fetch.fetch_url(url, s, common.DEFAULT_TIMEOUT, 0, allow_private=True)
        with self.assertRaises(common.FetchError):
            fetch.fetch_url(url, s, common.DEFAULT_TIMEOUT, 0)


    def test_cached_private_redirect_target_not_served_without_opt_in(self):
        s, _ = _session({
            "http://public.test/": (302, {"Location": "http://evil.test/"}, b""),
            "http://evil.test/": (200, {"Content-Type": "text/plain"}, b"secret")})
        fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0,
                        allow_private=True)
        with self.assertRaisesRegex(common.FetchError, "cached final URL"):
            fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)

    def test_wrong_shape_entries_fail(self):
        for raw in ("[]", "{}", '{"text": "x"}', '"s"'):
            with self.subTest(raw=raw):
                with open(common._cache_path("http://public.test/"), "w") as f:
                    f.write(raw)
                with self.assertRaisesRegex(common.FetchError, "corrupt cache entry"):
                    common.cache_get("http://public.test/")

    def test_cache_set_leaves_no_tmp_file_on_failure(self):
        with self.assertRaises(TypeError):
            common.cache_set("http://public.test/", {"text": object()})
        self.assertEqual(os.listdir(common.cache_dir()), [])


class Guard(Base):
    def test_private_initial_url_refused_by_default(self):
        s, adapter = _session(_page("http://evil.test/"))
        with self.assertRaisesRegex(common.FetchError, "evil.test"):
            fetch.fetch_url("http://evil.test/", s, common.DEFAULT_TIMEOUT, 0)
        with self.assertRaises(common.FetchError):
            common.http_get("http://evil.test/", s)
        with self.assertRaises(common.FetchError):
            common.http_get("http://127.0.0.1:8080/", s)
        self.assertEqual(adapter.seen, [])

    def test_private_initial_url_allowed_with_opt_out(self):
        s, _ = _session(_page("http://evil.test/"))
        r = fetch.fetch_url("http://evil.test/", s, common.DEFAULT_TIMEOUT, 0,
                            allow_private=True)
        self.assertEqual(r["status"], 200)
        _, status, _, _ = common.http_get("http://evil.test/", s,
                                          allow_private=True)
        self.assertEqual(status, 200)

    def test_non_http_scheme_refused(self):
        s, _ = _session({})
        with self.assertRaisesRegex(common.FetchError, "only http"):
            common.http_get("file:///etc/passwd", s)

    def test_redirect_to_private_refused(self):
        for target in ("http://127.0.0.1/admin", "http://metadata.test/latest",
                       "http://169.254.169.254/latest"):
            with self.subTest(target=target):
                s, adapter = _session({
                    "http://public.test/": (302, {"Location": target}, b""),
                    target: (200, {"Content-Type": "text/plain"}, b"secret")})
                with self.assertRaisesRegex(common.FetchError, "redirect to"):
                    fetch.fetch_url("http://public.test/", s,
                                    common.DEFAULT_TIMEOUT, 0)
                self.assertEqual(adapter.seen, ["http://public.test/"])

    def test_multi_hop_redirect_checked(self):
        s, adapter = _session({
            "http://public.test/": (301, {"Location": "http://other.test/a"}, b""),
            "http://other.test/a": (302, {"Location": "http://evil.test/b"}, b""),
            "http://evil.test/b": (200, {}, b"x")})
        with self.assertRaises(common.FetchError):
            common.http_get("http://public.test/", s)
        self.assertNotIn("http://evil.test/b", adapter.seen)

    def test_public_relative_redirect_followed(self):
        s, _ = _session({
            "http://public.test/": (302, {"Location": "/page"}, b""),
            "http://public.test/page": (200, {"Content-Type": "text/html"},
                                        HTML.encode())})
        r = fetch.fetch_url("http://public.test/", s, common.DEFAULT_TIMEOUT, 0)
        self.assertEqual(r["url"], "http://public.test/page")

    def test_redirect_to_private_allowed_with_opt_out(self):
        s, _ = _session({
            "http://public.test/": (302, {"Location": "http://evil.test/"}, b""),
            "http://evil.test/": (200, {"Content-Type": "text/plain"}, b"ok")})
        final, _, _, body = common.http_get("http://public.test/", s,
                                            allow_private=True)
        self.assertEqual((final, body), ("http://evil.test/", b"ok"))

    def test_session_hooks_not_mutated(self):
        s, _ = _session(_page())
        common.http_get("http://public.test/", s)
        self.assertEqual(s.hooks["response"], [])


class Searxng(Base):
    def test_only_the_configured_local_instance_may_be_private(self):
        calls = []

        def fake_get(url, session, timeout, allow_private=False):
            calls.append((url.split("/search")[0], allow_private))
            raise common.FetchError("down")

        with mock.patch.object(common, "http_get", fake_get):
            with self.assertRaises(common.FetchError):
                search.search_searxng("q", 3, None, None,
                                      local="http://127.0.0.1:8080/")
        self.assertEqual(calls[0], ("http://127.0.0.1:8080", True))
        self.assertTrue(len(calls) > 2)
        self.assertTrue(all(not priv for base, priv in calls
                            if base != "http://127.0.0.1:8080"))


class Cli(Base):
    def _run(self, argv):
        s, _ = _session(_page("http://evil.test/"))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(common, "build_session", return_value=s), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = fetch.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_private_is_a_failure_in_text_and_json_modes(self):
        rc, _, err = self._run(["http://evil.test/"])
        self.assertEqual(rc, 1)
        self.assertIn("[error]", err)
        rc, out, _ = self._run(["--json", "http://evil.test/"])
        self.assertEqual(rc, 1)
        self.assertIn("refused", json.loads(out)["error"])

    def test_allow_private_flag(self):
        rc, out, _ = self._run(["--allow-private", "http://evil.test/"])
        self.assertEqual(rc, 0)
        self.assertIn("word", out)


if __name__ == "__main__":
    unittest.main()
