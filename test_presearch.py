"""presearch client + registry against a fake HTTP layer.

Responses are shaped like jmfts-client's contracts (SubtreeResponse,
CapabilitiesResponse, PredicateResponse, ...), not like what the old client
guessed. Run: python -m unittest -v
"""

import json
import unittest
from unittest import mock

from presearch import broker, cli
from presearch.record import P_HOME_URL
from presearch.jmfts import Jmfts, JmftsError
from presearch.record import (P_COVERS_TOPIC, P_LISTED_BY, P_SEARCH_ENDPOINT,
                              Source, Topic, UT_SOURCE, UT_TOPIC)
from presearch.registry import Registry


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else ""
        self.content = self.text.encode()

    def json(self):
        return self._body


class FakeSession:
    """Routes (method, path) to a handler; records every call."""

    def __init__(self):
        self.headers = {}
        self.calls = []
        self.routes = {}  # (method, path) -> callable(kw) -> FakeResponse

    def on(self, method, path, handler):
        self.routes[(method, path)] = handler

    def request(self, method, url, timeout=None, **kw):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        body = json.loads(kw["data"]) if kw.get("data") else None
        self.calls.append((method, path, body, kw.get("params")))
        handler = self.routes.get((method, path))
        if handler is None:
            raise AssertionError(f"unexpected {method} {path}")
        return handler(kw)

    def calls_to(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


def doc(id_, title, usetype, parent_id=None):
    """A DocumentResponse-shaped dict (the fields presearch reads)."""
    return {"id": id_, "parent_id": parent_id, "title": title,
            "content": title, "structured_content": {}, "path": [],
            "depth": 0, "usetype": usetype, "settled": "settled"}


def make_client():
    c = Jmfts(base_url="http://jmfts.test", retries=2)
    c.session = FakeSession()
    return c


def caps(methods):
    return {"version": "0.7.0", "extras": [],
            "retrieval": {"methods": methods, "default_methods": ["bm25"],
                          "default_weights": {},
                          "search_exclude_usetypes": [],
                          "bm25_exclude_usetypes": []}}


class PreflightTest(unittest.TestCase):
    def test_passes_with_bm25(self):
        c = make_client()
        c.session.on("GET", "/capabilities",
                     lambda kw: FakeResponse(200, caps(["vector", "bm25"])))
        self.assertEqual(c.preflight()["version"], "0.7.0")

    def test_raises_when_methods_missing(self):
        for body in ({"version": "0.7.0"},                       # no retrieval
                     {"retrieval": {}},                          # no methods
                     caps([]),                                   # empty list
                     {"retrieval_methods": ["bm25"]}):           # old location
            c = make_client()
            c.session.on("GET", "/capabilities",
                         lambda kw, b=body: FakeResponse(200, b))
            with self.assertRaises(JmftsError, msg=str(body)):
                c.preflight()

    def test_raises_when_bm25_absent(self):
        c = make_client()
        c.session.on("GET", "/capabilities",
                     lambda kw: FakeResponse(200, caps(["vector"])))
        with self.assertRaises(JmftsError):
            c.preflight()


class PredicateTest(unittest.TestCase):
    def test_lists_with_triples_only_false(self):
        c = make_client()
        c.session.on("GET", "/triples/predicates", lambda kw: FakeResponse(
            200, [{"id": 7, "name": "covers_topic"}]))
        self.assertEqual(c.ensure_predicate("covers_topic"), 7)
        self.assertEqual(c.session.calls[0][3], {"with_triples_only": "false"})

    def test_create_409_relists(self):
        c = make_client()
        listings = [[], [{"id": 9, "name": "listed_by"}]]
        c.session.on("GET", "/triples/predicates",
                     lambda kw: FakeResponse(200, listings.pop(0)))
        c.session.on("POST", "/triples/predicates", lambda kw: FakeResponse(
            409, {"detail": "Predicate 'listed_by' already exists"}))
        self.assertEqual(c.ensure_predicate("listed_by"), 9)
        self.assertEqual(len(c.session.calls_to("POST", "/triples/predicates")), 1)

    def test_other_create_errors_propagate(self):
        c = make_client()
        c.session.on("GET", "/triples/predicates", lambda kw: FakeResponse(200, []))
        c.session.on("POST", "/triples/predicates",
                     lambda kw: FakeResponse(422, {"detail": "bad"}))
        with self.assertRaises(JmftsError):
            c.ensure_predicate("x")


class TripleTest(unittest.TestCase):
    def test_document_triple_uses_idempotent_put(self):
        c = make_client()
        c._predicate_ids["listed_by"] = 3
        c.session.on("PUT", "/triples", lambda kw: FakeResponse(200, {"id": 1}))
        c.add_triple(10, P_LISTED_BY, 20, source_document_id=5)
        (_, _, body, _), = c.session.calls
        self.assertEqual(body, {"subject_id": 10, "predicate_id": 3,
                                "object_id": 20, "source_document_id": 5})

    def test_literal_triple_raises_naming_issue_and_sends_nothing(self):
        c = make_client()
        with self.assertRaises(JmftsError) as cm:
            c.add_triple(10, P_SEARCH_ENDPOINT, "https://x/?q={q}")
        self.assertIn("jmfts#48", str(cm.exception))
        self.assertEqual(c.session.calls, [])


class RetryTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch("presearch.jmfts.time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_post_500_is_not_retried(self):
        c = make_client()
        c.session.on("POST", "/documents", lambda kw: FakeResponse(500, {}))
        with self.assertRaises(JmftsError):
            c.create_document("t", "c")
        self.assertEqual(len(c.session.calls), 1)

    def test_get_500_is_retried(self):
        c = make_client()
        answers = [FakeResponse(500, {}), FakeResponse(200, {"ok": 1})]
        c.session.on("GET", "/capabilities", lambda kw: answers.pop(0))
        self.assertEqual(c.capabilities(), {"ok": 1})
        self.assertEqual(len(c.session.calls), 2)

    def test_search_post_is_retried(self):
        c = make_client()
        answers = [FakeResponse(503, {}), FakeResponse(200, {"results": []})]
        c.session.on("POST", "/search/bm25", lambda kw: answers.pop(0))
        c.search("q", index_name="presearch")
        self.assertEqual(len(c.session.calls), 2)


class DocumentTest(unittest.TestCase):
    def test_create_document_disables_default_bm25_index(self):
        c = make_client()
        c.session.on("POST", "/documents",
                     lambda kw: FakeResponse(200, doc(1, "t", UT_TOPIC)))
        c.create_document("t", "c", parent_id=4, usetype=UT_TOPIC)
        (_, _, body, _), = c.session.calls
        self.assertIs(body["auto_index_bm25"], False)
        self.assertIs(body["auto_embed"], False)

    def test_update_document_patches_without_reembed(self):
        c = make_client()
        c.session.on("PATCH", "/documents/5",
                     lambda kw: FakeResponse(200, doc(5, "m", "x")))
        c.update_document(5, content="new")
        (_, _, body, _), = c.session.calls
        self.assertEqual(body, {"re_embed": False, "content": "new"})


class SearchTest(unittest.TestCase):
    def test_bm25_index_name_is_query_param(self):
        c = make_client()
        c.session.on("POST", "/search/bm25",
                     lambda kw: FakeResponse(200, {"results": []}))
        c.search("q", index_name="presearch", parent_id=1)
        (_, _, body, params), = c.session.calls
        self.assertEqual(params, {"index_name": "presearch"})
        self.assertNotIn("index_name", body)

    def test_hybrid_index_name_is_body_field(self):
        c = make_client()
        c.session.on("POST", "/search/hybrid",
                     lambda kw: FakeResponse(200, {"results": []}))
        c.search("q", method="hybrid", index_name="presearch")
        (_, _, body, _), = c.session.calls
        self.assertEqual(body["index_name"], "presearch")

    def test_limit_over_100_raises(self):
        with self.assertRaises(JmftsError):
            make_client().search("q", limit=101)

    def test_broker_caps_source_query_and_names_index(self):
        reg = Registry(make_client(), verbose=False)
        reg.root_id = 1
        seen = []

        def handler(kw):
            seen.append((json.loads(kw["data"]), kw.get("params")))
            return FakeResponse(200, {"results": []})

        reg.j.session.on("POST", "/search/bm25", handler)
        broker.plan(reg, "q", limit=60, validate_first=False)
        limits = sorted(b["limit"] for b, _ in seen)
        self.assertEqual(limits, [60, 100])
        self.assertTrue(all(p == {"index_name": "presearch"} for _, p in seen))


class CliTest(unittest.TestCase):
    def test_limit_and_sample_above_100_are_refused(self):
        for argv in (["query", "x", "--limit", "150"],
                     ["query", "x", "--limit", "0"],
                     ["validate", "--sample", "500"]):
            with self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr"):
                cli.build_parser().parse_args(argv)

    def test_limit_100_is_accepted(self):
        args = cli.build_parser().parse_args(["query", "x", "--limit", "100"])
        self.assertEqual(args.limit, 100)

    def test_sample_zero_still_means_skip(self):
        args = cli.build_parser().parse_args(["validate", "--sample", "0"])
        self.assertEqual(args.sample, 0)

    def test_discover_accept_refuses_before_any_request(self):
        from presearch.providers.marginalia import MarginaliaProvider

        client = make_client()  # no routes: any JMFTS request would raise
        args = cli.build_parser().parse_args(
            ["discover", "--topic", "Computers", "--accept"])
        # Real provider, with the network layer booby-trapped.
        with mock.patch.object(cli, "_client", return_value=client), \
                mock.patch("requests.Session.request",
                           side_effect=AssertionError("network used")), \
                mock.patch("requests.get",
                           side_effect=AssertionError("network used")):
            with self.assertRaises(JmftsError) as cm:
                cli.cmd_discover(args)
        self.assertIn("jmfts#48", str(cm.exception))
        self.assertEqual(client.session.calls, [])
        self.assertTrue(MarginaliaProvider.ALWAYS_LITERAL)

    def test_discover_accept_per_candidate_backstop(self):
        class Prov:
            ALWAYS_LITERAL = frozenset()  # lies; the candidate check catches it

            def discover(self, topic, limit_domains=25):
                yield Source(url="https://e.org", title="E",
                             topic_keys=(topic,),
                             facts={P_HOME_URL: "https://e.org"})

        client = make_client()  # no routes: any request would raise
        args = cli.build_parser().parse_args(
            ["discover", "--topic", "Computers", "--accept"])
        with mock.patch.object(cli, "_provider", return_value=Prov()), \
                mock.patch.object(cli, "_client", return_value=client):
            with self.assertRaises(JmftsError) as cm:
                cli.cmd_discover(args)
        self.assertIn("jmfts#48", str(cm.exception))
        self.assertEqual(client.session.calls, [])

    def test_sources_and_skeleton_use_dedup(self):
        c = make_client()
        c.session.on("POST", "/documents",
                     lambda kw: FakeResponse(200, doc(1, "t", "x")))
        c.create_document("t", "c", dedup=True)
        self.assertEqual(c.session.calls[0][3], {"dedup": "true"})


def registry_with_topics(descendants, subtree_status=200):
    c = make_client()
    reg = Registry(c, verbose=False)
    reg.root_id, reg.manifests_id, reg.topics_id = 1, 2, 3
    body = {"root": doc(3, "Topics", "registry:root"),
            "descendants": descendants, "total": len(descendants) + 1}
    c.session.on("GET", "/documents/3/subtree",
                 lambda kw: FakeResponse(subtree_status, body))
    return reg


class SubtreeTest(unittest.TestCase):
    def test_finds_existing_topics_in_flat_descendants(self):
        reg = registry_with_topics([
            doc(10, "Computers", UT_TOPIC, 3),
            doc(11, "Computers/Algorithms", UT_TOPIC, 10),
            doc(12, "Some Site", UT_SOURCE, 11),
        ])
        self.assertEqual(reg._load_existing_topics(),
                         {"Computers": 10, "Computers/Algorithms": 11})

    def test_reload_does_not_duplicate_topics(self):
        reg = registry_with_topics([doc(10, "Computers", UT_TOPIC, 3)])
        created = reg.load_topics([Topic(key="Computers", title="Computers")])
        self.assertEqual(created, 0)
        self.assertEqual(reg.j.session.calls_to("POST", "/documents"), [])

    def test_in_flight_root_is_an_error_not_empty(self):
        reg = registry_with_topics([], subtree_status=409)
        with self.assertRaises(JmftsError) as cm:
            reg._load_existing_topics()
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("ingesting", str(cm.exception))


class LiteralLoadTest(unittest.TestCase):
    def literal_source(self):
        return Source(url="https://example.org", title="Example",
                      topic_keys=("Computers",),
                      facts={P_SEARCH_ENDPOINT: "https://example.org/?q={q}"})

    def test_load_sources_refuses_before_writing_any_document(self):
        reg = registry_with_topics([doc(10, "Computers", UT_TOPIC, 3)])
        with self.assertRaises(JmftsError) as cm:
            reg.load_sources([self.literal_source()], manifest_id=2)
        self.assertIn("jmfts#48", str(cm.exception))
        self.assertEqual(
            [c for c in reg.j.session.calls if c[0] != "GET"], [])

    def test_sources_without_literals_still_load(self):
        reg = registry_with_topics([doc(10, "Computers", UT_TOPIC, 3),
                                    doc(11, "Other", UT_TOPIC, 3)])
        s = reg.j.session
        s.on("POST", "/documents",
             lambda kw: FakeResponse(200, doc(50, "s", UT_SOURCE, 10)))
        s.on("GET", "/triples/predicates", lambda kw: FakeResponse(
            200, [{"id": 1, "name": P_LISTED_BY},
                  {"id": 2, "name": P_COVERS_TOPIC}]))
        s.on("PUT", "/triples", lambda kw: FakeResponse(200, {"id": 1}))
        src = Source(url="https://e.org", title="E",
                     topic_keys=("Computers", "Other"))
        stats = reg.load_sources([src], manifest_id=2)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(s.calls_to("POST", "/documents")[0][3],
                         {"dedup": "true"})
        self.assertEqual(len(s.calls_to("PUT", "/triples")), 2)


if __name__ == "__main__":
    unittest.main()
