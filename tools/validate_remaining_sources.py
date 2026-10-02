"""arXiv/Europe PMC adapter acceptance via real SDK + local HTTP; no cloud calls."""
import copy
import json
import threading
import time
import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
from paperpilot import fetcher
from paperpilot.agent_runtime import CancellationToken, OperationCancelled, run_scope
from paperpilot.search_filters import SearchFilters
from paperpilot.search_metrics import SearchTrace, trace_scope
from paperpilot.sources import arxiv_source as ax, europepmc_source as ep, base


def feed(start, n, total=800):
    entries = ''.join(f'''<entry><id>https://arxiv.org/abs/2401.{i:05d}v1</id>
        <title>Research paper {i}</title><summary>Complete abstract for record {i}.</summary>
        <published>2024-01-01T00:00:00Z</published><updated>2024-01-01T00:00:00Z</updated>
        <author><name>Alice Example</name></author>
        <arxiv:primary_category term="cs.AI"/><category term="cs.AI"/>
        <arxiv:journal_ref>Nature</arxiv:journal_ref></entry>''' for i in range(start, start+n))
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"
        xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
        <title>arXiv results</title><id>local</id><updated>2024-01-01T00:00:00Z</updated>
        <opensearch:totalResults>{total}</opensearch:totalResults>
        <opensearch:startIndex>{start}</opensearch:startIndex>{entries}</feed>'''.encode()


def core(i):
    return dict(id=str(i), source="MED", title=f"Biomedical record {i}",
                abstractText=f"Complete biomedical abstract {i}.", authorString="Alice Example",
                pubYear="2024", doi=f"10.0/epmc-{i}", citedByCount=400-i,
                authorList={"author": [{"fullName":"Alice Example", "firstName":"Alice", "lastName":"Example",
                                           "authorAffiliationDetailsList":{"unused":"X"*2000}}]},
                journalInfo={"journal":{"title":"Nature"}})


class Cache:
    def __init__(self): self.data = {}
    def get(self, key): return copy.deepcopy(self.data.get(key))
    def set(self, key, value, expire): self.data[key] = copy.deepcopy(value)


class Adapters(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args): pass
            def do_GET(self):
                parsed = urlparse(self.path); q = parse_qs(parsed.query)
                calls.append((parsed.path, q, self.client_address[1]))
                if parsed.path == "/arxiv":
                    start = int(q.get("start", [0])[0]); n = int(q["max_results"][0])
                    status = 503 if "partial" in q.get("search_query", [""])[0] and start else 200
                    payload = feed(start, min(n, 800-start)) if status == 200 else b"unavailable"
                    if "invalid" in q.get("search_query",[""])[0]: payload=b"not an Atom feed"
                    if "empty" in q.get("search_query",[""])[0]: payload=feed(0,0,0)
                    mime = "application/atom+xml"
                else:
                    payload = json.dumps({"resultList":{"result":[core(i) for i in range(int(q["pageSize"][0]))]}}).encode()
                    status, mime = 200, "application/json"
                self.send_response(status); self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        root = f"http://127.0.0.1:{self.server.server_port}"
        self.axcache, self.epcache = Cache(), Cache()
        self.patches = [patch.object(ax, "_arxiv_cache", self.axcache), patch.object(ep, "_epmc_cache", self.epcache),
                        patch.object(ax.arxiv.Client, "query_url_format", root+"/arxiv?{}"),
                        patch.object(ep, "_EPMC_BASE", root+"/epmc"), patch.object(ax, "_ARXIV_RATE_LIMIT", 0)]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def test_arxiv_real_sdk_400_one_page_cache_copy_and_same_source_connection(self):
        with base.source_http_scope("arxiv"):
            first = ax.fetch_arxiv(["robot navigation"], 400, request_timeout=4)
            cached = ax.fetch_arxiv(["robot navigation"], 400)
            ax.fetch_arxiv(["quantum communication"], 400)
        self.assertEqual(len(first), 400); self.assertEqual(first, cached)
        self.assertEqual(len(self.calls), 2); self.assertEqual(self.calls[0][2], self.calls[1][2])
        self.assertEqual(self.calls[0][1]["max_results"], ["400"])
        self.assertEqual([p["api_score"] for p in first], [1-i/400 for i in range(400)])
        cached[0]["abstract"] = "changed"
        self.assertEqual(ax.fetch_arxiv(["robot navigation"], 400)[0], first[0])

    def test_arxiv_second_page_failure_retains_first_and_does_not_cache_partial(self):
        errors = []
        rows = ax._fetch_arxiv_raw("partial", 800, errors=errors, request_timeout=2)
        self.assertEqual(len(rows), 400); self.assertEqual(len(self.calls), 3) # failed page + one SDK retry
        self.assertTrue(any(k == "network" for _, k, _ in errors)); self.assertFalse(self.axcache.data)

    def test_arxiv_invalid_feed_and_empty_not_cached_as_long_lived_absence(self):
        errors=[]
        self.assertEqual(ax._fetch_arxiv_raw("invalid",400,errors=errors),[])
        self.assertTrue(any(k=="invalid_response" for _,k,_ in errors))
        self.assertEqual(ax._fetch_arxiv_raw("empty",400),[])
        self.assertFalse(self.axcache.data)

    def test_arxiv_filtered_cache_isolated_and_complete_metadata(self):
        filters = SearchFilters(2024, 2024, "Alice Example", "Nature")
        first = ax._fetch_arxiv_raw("physics", 400, filters=filters, errors=[])
        second = ax._fetch_arxiv_raw("physics", 400, filters=filters, errors=[])
        self.assertEqual(len(first), 400); self.assertEqual(first, second); self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(filters.matches(p) for p in first))
        self.assertIn('au:"Alice Example"', self.calls[0][1]["search_query"][0])

    def test_arxiv_cancel_before_cache_write_and_rate_wait(self):
        run = SimpleNamespace(token=CancellationToken())
        original = ax._parse_arxiv_result
        def parse(r):
            run.token.cancel()
            return original(r)
        with patch.object(ax, "_parse_arxiv_result", side_effect=parse), run_scope(run):
            with self.assertRaises(OperationCancelled): ax._fetch_arxiv_raw("cancel", 400)
        self.assertFalse(self.axcache.data)
        run2 = SimpleNamespace(token=CancellationToken())
        with patch.object(ax, "_ARXIV_RATE_LIMIT", 3), patch.object(ax, "_ARXIV_LAST_CALL", time.monotonic()):
            timer = threading.Timer(.03, run2.token.cancel); timer.start()
            started = time.monotonic()
            with run_scope(run2), self.assertRaises(OperationCancelled): ax._wait_arxiv_rate_limit()
            self.assertLess(time.monotonic()-started, .3); timer.join()

    def test_arxiv_rate_gate_serializes_simultaneous_runs(self):
        starts = []
        with patch.object(ax, "_ARXIV_RATE_LIMIT", .035), patch.object(ax, "_ARXIV_LAST_CALL", 0):
            def reserve(): ax._wait_arxiv_rate_limit(); starts.append(time.monotonic())
            with ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(lambda _: reserve(), range(3)))
        starts.sort(); self.assertTrue(all(b-a >= .03 for a,b in zip(starts, starts[1:])))

    def test_europepmc_400_complete_abstracts_compact_cache_legacy_and_timeout(self):
        trace = SearchTrace()
        with trace_scope(trace), base.source_http_scope("europepmc"):
            first = ep.fetch_europepmc(["immunotherapy"], 400, request_timeout=7)
            cached = ep.fetch_europepmc(["immunotherapy"], 400)
        self.assertEqual(len(first), 400); self.assertEqual(first, cached); self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(p["abstract"] and p["_author_names"] for p in first))
        self.assertNotIn("unused", json.dumps(self.epcache.data)); self.assertEqual(first[399]["api_score"], 1-399/400)
        self.epcache.data = {"search:legacy|1||CITED desc":[core(0)]}
        with patch.object(ep, "source_get", side_effect=AssertionError("legacy cache must not request")):
            self.assertEqual(ep._fetch_europepmc_raw("legacy", 1)[0]["abstract"], first[0]["abstract"])
        resp = requests.Response(); resp.status_code = 200; resp._content = b'{"resultList":{"result":[]}}'
        with patch.object(ep, "source_get", return_value=resp) as get:
            ep._fetch_europepmc_raw("timeout", 1, request_timeout=7)
        self.assertEqual(get.call_args.kwargs["timeout"], 7)

    def test_europepmc_failure_stops_cascade_without_final_backoff_or_poison_cache(self):
        errors = []
        with patch.object(ep, "source_get", side_effect=requests.Timeout("slow")) as get, \
             patch.object(ep, "interruptible_wait") as wait:
            out = fetcher.fetch_multi_primary(["a", "b"], ["c"], [], source="europepmc", max_results=400, errors=errors)
        self.assertEqual(out, []); self.assertEqual(get.call_count, 3); self.assertEqual(wait.call_count, 2)
        self.assertTrue(any(k == "network" for _,k,_ in errors)); self.assertFalse(self.epcache.data)
        resp = requests.Response(); resp.status_code = 200; resp._content = b'{"unexpected":"bad"}'
        with patch.object(ep, "source_get", return_value=resp):
            self.assertEqual(ep._fetch_europepmc_raw("bad", 400, errors=errors), [])
        self.assertTrue(any(k == "invalid_response" for _,k,_ in errors)); self.assertFalse(self.epcache.data)

    def test_europepmc_cancel_empty_and_rate_limit(self):
        self.assertEqual(ep._fetch_europepmc_raw("q", 0), []); self.assertEqual(ax._fetch_arxiv_raw("q", 0), [])
        for status, expected in ((403,1),(429,3)):
            resp = requests.Response(); resp.status_code=status
            with patch.object(ep, "source_get", return_value=resp) as get, patch.object(ep, "interruptible_wait"):
                with self.assertRaises(base.SourceRateLimited): ep._fetch_europepmc_raw("rate", 400)
            self.assertEqual(get.call_count, expected)
        self.assertFalse(self.epcache.data)

    def test_europepmc_cancelled_response_and_corrupt_cache(self):
        run = SimpleNamespace(token=CancellationToken())
        resp = requests.Response(); resp.status_code=200
        resp._content=json.dumps({"resultList":{"result":[core(1)]}}).encode()
        def stopped(*a, **k): run.token.cancel(); return resp
        with patch.object(ep,"source_get",side_effect=stopped),run_scope(run):
            with self.assertRaises(OperationCancelled): ep._fetch_europepmc_raw("cancel",400)
        self.assertFalse(self.epcache.data)
        self.epcache.data["parsed-v1:search:refresh|1||CITED desc"]={"corrupt":"value"}
        self.assertEqual(len(ep._fetch_europepmc_raw("refresh",1)),1)
        errors=[]; self.epcache.data["search:bad-legacy|1||CITED desc"]={"bad":"type"}
        self.assertEqual(ep._fetch_europepmc_raw("bad-legacy",1,errors=errors),[])
        self.assertTrue(any(k=="invalid_response" for _,k,_ in errors))

    def test_success_query_cache_expiry_uses_existing_diskcache_ttl(self):
        from diskcache import Cache as DiskCache
        with tempfile.TemporaryDirectory() as directory, DiskCache(directory) as cache:
            with patch.object(ax,"_arxiv_cache",cache),patch.object(ax,"_CACHE_TTL",.05):
                first=ax._fetch_arxiv_raw("expiry",1)
                self.assertEqual(ax._fetch_arxiv_raw("expiry",1),first)
                self.assertEqual(len(self.calls),1)
                time.sleep(.08)
                self.assertEqual(ax._fetch_arxiv_raw("expiry",1),first)
                self.assertEqual(len(self.calls),2)


if __name__ == "__main__": unittest.main(verbosity=2)
