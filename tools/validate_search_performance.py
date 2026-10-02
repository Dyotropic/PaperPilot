"""Isolated search performance/business checks; no external API or LLM calls.

Run with tools/run_validation.py. Native painting and production API latency are
separate acceptance boundaries; controlled delays only measure scheduling.
"""
import copy
import json
import random
from difflib import SequenceMatcher
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import requests
from paperpilot import indexer, mt_translator, fetcher
from paperpilot.agent_runtime import CancellationToken, OperationCancelled, run_scope, current_run
from paperpilot.search_filters import SearchFilters
from paperpilot.search_metrics import SearchTrace, trace_scope
from paperpilot.search_service import collect_sources
from paperpilot.sources import base, openalex_source as oa


class Cache:
    def __init__(self):
        self.values, self.writes = {}, []
    def get(self, key):
        item = self.values.get(key)
        return item[1] if item and item[0] > time.monotonic() else None
    def set(self, key, value, expire):
        self.values[key] = (time.monotonic() + expire, copy.deepcopy(value))
        self.writes.append((key, expire))


def response(payload, status=200):
    item = requests.Response()
    item.status_code = status
    item._content = json.dumps(payload).encode()
    return item


def rows(n, start=1):
    return [dict(title=f"Paper {i}", source="openalex", abstract="",
                 openalex_id=f"https://openalex.org/W{i}") for i in range(start, start+n)]


def collect(sources=("arxiv", "openalex", "europepmc"), **kwargs):
    options = dict(sources=sources, primary_kw=["solar"], secondary_kw=[], regular_kw=[],
                   description=None, max_per=400, year_min="", year_max="", filters=None,
                   cascade=lambda **kw: ([dict(source=kw["source"], title="a")], 0),
                   multi_primary=lambda **kw: [dict(source=kw["source"], title="b")],
                   description_fetchers={}, parallel=True)
    options.update(kwargs)
    return collect_sources(**options)


class SourceScheduling(unittest.TestCase):
    def test_dedup_matches_original_algorithm_languages_lengths_and_identities(self):
        rng = random.Random(27)
        titles = ["solar cell stability", "SOLAR  cell stability", "solar cell stabilities",
                  "钙钛矿太阳能电池稳定性", "钙钛矿太阳能电池的稳定性", "", "a", "b",
                  "repeated characters "*20, "repeated characters "*19 + "different suffix"]
        titles += [" ".join(rng.sample(["heat", "cells", "stability", "perovskite", "light", "module",
                                       "doping", "analysis", "synthesis", "fabrication", "interface"], rng.randint(3, 9)))
                   for _ in range(100)]
        papers = [dict(title=t, doi=f"10.0/{i}") for i, t in enumerate(titles)]
        expected, norms = [], []
        for paper in papers:
            title = fetcher._normalize_title(paper["title"])
            if not any(SequenceMatcher(None, title, previous).ratio() >= .9 for previous in norms):
                expected.append(paper)
                norms.append(title)
        self.assertEqual(fetcher.deduplicate(papers), expected)

    def test_real_adapter_network_failure_stops_cascade_and_description(self):
        with patch.object(oa, "source_get", side_effect=requests.Timeout()) as get:
            papers, errors = collect(("openalex",), multi_primary=fetcher.fetch_multi_primary,
                                     description="desc", description_fetchers={"openalex": oa.fetch_openalex})
        self.assertEqual(get.call_count, 1)
        self.assertFalse(papers)
        self.assertEqual(errors[0][1], "network")

    def test_overlap_stable_merge_and_context(self):
        barrier = threading.Barrier(3)
        run = SimpleNamespace(token=CancellationToken())
        coordinator, observed, progress_threads = threading.get_ident(), [], []
        def fetch(**kw):
            observed.append((kw["source"], current_run(), kw["errors"]))
            barrier.wait(2)
            time.sleep({"arxiv": .09, "openalex": .04, "europepmc": .01}[kw["source"]])
            return [dict(source=kw["source"], title=kw["source"])]
        with run_scope(run):
            papers, errors = collect(cascade=lambda **kw: (fetch(**kw), 0), multi_primary=fetch,
                                     on_progress=lambda _: progress_threads.append(threading.get_ident()))
        self.assertEqual([p["source"] for p in papers], ["arxiv", "openalex", "europepmc"])
        self.assertFalse(errors)
        self.assertTrue(all(owner is run for _, owner, _ in observed))
        self.assertEqual(len({id(errors) for _, _, errors in observed}), 3)
        self.assertEqual(set(progress_threads), {coordinator})

    def test_serial_parallel_same_results_and_wall_time(self):
        def fetch(**kw):
            time.sleep(.08)
            return [dict(source=kw["source"], title="Shared", doi="10.1/shared", api_score=.7)]
        outputs, durations = [], []
        for parallel in (False, True):
            started = time.perf_counter()
            papers, errors = collect(parallel=parallel, cascade=lambda **kw: (fetch(**kw), 0), multi_primary=fetch)
            outputs.append((fetcher.deduplicate(papers), errors))
            durations.append(time.perf_counter()-started)
        self.assertEqual(outputs[0], outputs[1])
        self.assertLess(durations[1], durations[0]*.8)
        print(f"CONTROLLED SOURCE DELAY serial={durations[0]:.3f}s parallel={durations[1]:.3f}s")

    def test_description_order_and_filtered_full_skip(self):
        stages = []
        def primary(**kw):
            stages.append("keywords")
            return [dict(title="primary", source="openalex")]
        def description(*args, **kw):
            stages.append("description")
            return [dict(title="description", source="openalex")]
        def enrich(papers, **kw):
            stages.append("abstracts")
            self.assertEqual(len(papers), 2)
        with patch.object(oa, "enrich_abstracts", side_effect=enrich):
            collect(("openalex",), multi_primary=primary, description="desc",
                    description_fetchers={"openalex": description})
        self.assertEqual(stages, ["keywords", "description", "abstracts"])
        stages.clear()
        collect(("europepmc",), multi_primary=primary, description="desc", max_per=1,
                filters=SearchFilters(year_from=2020), description_fetchers={"europepmc": description})
        self.assertEqual(stages, ["keywords"])

    def test_source_failure_preserves_other_source_and_no_fatal_description(self):
        def fetch(**kw):
            if kw["source"] == "openalex":
                kw["errors"].append(("openalex", "network", "network"))
                return []
            return [dict(title="survivor", source=kw["source"])]
        called = []
        desc = {name: lambda *a, **k: (called.append(k) or []) for name in ("arxiv", "openalex", "europepmc")}
        papers, errors = collect(multi_primary=fetch, description="desc", description_fetchers=desc)
        self.assertEqual(len(papers), 2)
        self.assertEqual(errors, [("openalex", "network", "network")])
        self.assertEqual(len(called), 2)

    def test_cancel_drains_workers_and_stops_later_stages(self):
        run = SimpleNamespace(token=CancellationToken())
        barrier, ended, desc = threading.Barrier(3), [], []
        def fetch(**kw):
            barrier.wait(2)
            run.token.cancel()
            time.sleep(.02)
            ended.append(kw["source"])
            return [dict(title="late", source=kw["source"])]
        with run_scope(run), self.assertRaises(OperationCancelled):
            collect(cascade=lambda **kw: (fetch(**kw), 0), multi_primary=fetch,
                    description="desc", description_fetchers={s: lambda *a, **k: desc.append(s)
                                                               for s in ("arxiv", "openalex", "europepmc")})
        self.assertEqual(len(ended), 3)
        self.assertFalse(desc)

    def test_simultaneous_searches_keep_trace_and_results(self):
        def job(label):
            trace = SearchTrace()
            def fetch(**kw):
                time.sleep(.01)
                return [dict(title=label, source=kw["source"])]
            with trace_scope(trace):
                papers, _ = collect(cascade=lambda **kw: (fetch(**kw), 0), multi_primary=fetch)
            return papers, trace.snapshot()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(job, ("Chinese task", "English task")))
        for label, (papers, trace) in zip(("Chinese task", "English task"), results):
            self.assertTrue(all(p["title"] == label for p in papers))
            self.assertEqual(trace["counters"]["openalex.recalled"], 1)
        self.assertNotEqual(results[0][1]["run_id"], results[1][1]["run_id"])


class OpenAlexBatching(unittest.TestCase):
    def setUp(self):
        self.cache = Cache()
        self.scope = patch.object(oa, "_oa_cache", self.cache)
        self.scope.start()
        self.key = patch.object(oa, "_get_api_key", return_value="synthetic-key")
        self.key.start()
    def tearDown(self):
        self.scope.stop(); self.key.stop()

    def test_400_paging_four_requests_and_cache(self):
        calls = []
        def get(url, **kw):
            params = kw["params"]
            calls.append(params)
            start = (params["page"]-1)*params["per_page"]+1
            return response({"results": [dict(id=f"https://openalex.org/W{i}", title=f"Paper {i}",
                                               relevance_score=401-i, abstract_inverted_index={"solar": [0]})
                                         for i in range(start, start+100)]})
        with patch.object(oa, "source_get", side_effect=get), patch.object(oa, "interruptible_wait"):
            first = oa.fetch_openalex(["solar"], 400)
            second = oa.fetch_openalex(["solar"], 400)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(c["per_page"] == 100 and c["api_key"] == "synthetic-key" for c in calls))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 400)
        self.assertEqual(oa.fetch_openalex(["solar"], 0), [])

    def test_unique_400_missing_and_known_absence_cache_expiry(self):
        papers, calls = rows(400) + rows(400), []
        def get(url, **kw):
            self.assertEqual(url, "https://api.openalex.org/works")
            self.assertEqual(kw["params"]["api_key"], "synthetic-key")
            ids = kw["params"]["filter"].split(":")[1].split("|")
            calls.append(ids)
            return response({"results": [dict(id=f"https://openalex.org/{i}",
                              abstract_inverted_index=None if int(i[1:])%2 else {"available": [0]}) for i in ids]})
        with patch.object(oa, "source_get", side_effect=get):
            oa.enrich_abstracts(papers)
            oa.enrich_abstracts(rows(400))
            self.assertEqual(len(calls), 4)
            self.assertEqual(sum(bool(p["abstract"]) for p in papers), 400)
            self.cache.values["abs:https://openalex.org/W1"] = (0, {"absent": True})
            oa.enrich_abstracts(rows(1))
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[-1], ["W1"])

    def test_failure_invalid_omitted_and_rate_limit_do_not_negative_cache(self):
        outcomes = [requests.Timeout(), response({"wrong": []}),
                    response({"results": []}), response({"results": [{"id": "https://openalex.org/W1"}]}),
                    response({"results": [{"id": "https://openalex.org/W1", "abstract_inverted_index": {}}]}),
                    response({}, 429), response({"results": [{"id": "https://openalex.org/W1", "abstract_inverted_index": None}]})]
        errors = []
        with patch.object(oa, "source_get", side_effect=outcomes) as get:
            for _ in range(6):
                oa.enrich_abstracts(rows(1), errors)
                self.assertIsNone(self.cache.get("abs:https://openalex.org/W1"))
            oa.enrich_abstracts(rows(1), errors)
            self.assertEqual(get.call_count, 6)  # Same limited run avoids more HTTP.
            oa.enrich_abstracts(rows(1), [])  # A fresh run can retry; no poisoned cache.
        self.assertEqual(get.call_count, 7)
        self.assertTrue(self.cache.get("abs:https://openalex.org/W1")["absent"])
        self.assertEqual({kind for _, kind, _ in errors}, {"network", "invalid_response", "rate_limited"})

    def test_existing_cache_and_duplicate_rich_record(self):
        self.cache.set("abs:https://openalex.org/W1", "old abstract", expire=100)
        papers = rows(2) + [dict(openalex_id="W2", abstract="rich abstract")]
        with patch.object(oa, "source_get") as get:
            oa.enrich_abstracts(papers)
        get.assert_not_called()
        self.assertEqual([p["abstract"] for p in papers], ["old abstract", "rich abstract", "rich abstract"])

    def test_cancel_response_not_cached_or_applied_and_deferral_resets(self):
        run = SimpleNamespace(token=CancellationToken())
        papers = rows(101)
        def get(*a, **kw):
            run.token.cancel()
            # The production source_get checks after network; emulate it here.
            run.token.check()
        with patch.object(oa, "source_get", side_effect=get), run_scope(run), self.assertRaises(OperationCancelled):
            oa.enrich_abstracts(papers)
        self.assertFalse(self.cache.values)
        self.assertFalse(any(p["abstract"] for p in papers))
        with oa.defer_abstracts(), patch.object(oa, "enrich_abstracts") as enrich:
            oa._fetch_missing_abstracts(rows(1))
            enrich.assert_not_called()
        self.assertFalse(oa._defer_abstracts.get())

    def test_filtered_cursor_and_strict_year_preserved(self):
        calls = []
        def get(url, **kw):
            calls.append(kw["params"])
            return response({"results": [dict(title="old", id="https://openalex.org/W1", publication_year=2019),
                                           dict(title="eligible", id="https://openalex.org/W2", publication_year=2024,
                                                abstract_inverted_index={"solar": [0]})],
                             "meta": {"next_cursor": None}})
        with patch.object(oa, "source_get", side_effect=get):
            papers = oa.fetch_openalex(["solar"], 400, filters=SearchFilters(year_from=2020))
        self.assertEqual([p["title"] for p in papers], ["eligible"])
        self.assertEqual(calls[0]["cursor"], "*")
        self.assertIn("publication_year:>2019", calls[0]["filter"])


class TranslationAndModel(unittest.TestCase):
    def test_translation_batch_dedup_budget_cache_and_model_isolation(self):
        mt_translator._cache.clear()
        client = SimpleNamespace(provider="mock", model="a", base_url="local", is_available=True)
        calls = []
        def chat(messages, **kw):
            calls.append(kw)
            return SimpleNamespace(content="1. Long scientific description\n2. solar cells")
        client.chat = chat
        groups = (["长研究描述"*100], ["太阳能电池", "solar"], ["太阳能电池"], [""])
        with patch.object(mt_translator, "get_client", return_value=client), patch.object(mt_translator, "llm_configured", return_value=True):
            first = mt_translator.translate_all_terms(*groups)
            self.assertEqual(first, mt_translator.translate_all_terms(*groups))
            self.assertEqual(len(calls), 1)
            self.assertGreater(calls[0]["max_tokens"], 1000)
            client.model = "b"
            mt_translator.translate_all_terms(*groups)
        self.assertEqual(len(calls), 2)
        self.assertEqual(first[1], ["solar cells", "solar"])
        self.assertEqual(first[2], ["solar cells"])

    def test_bad_translation_retries_and_no_llm_ascii(self):
        mt_translator._cache.clear()
        client = SimpleNamespace(provider="mock", model="a", is_available=True,
                                 chat=lambda *a, **k: SimpleNamespace(content="1. 中文"))
        with patch.object(mt_translator, "get_client", return_value=client) as get, patch.object(mt_translator, "llm_configured", return_value=True):
            self.assertEqual(mt_translator.translate_terms(["plain", ""]), ["plain", ""])
            get.assert_not_called()
            self.assertEqual(mt_translator.translate_terms(["中文"]), [""])
            self.assertEqual(mt_translator.translate_terms(["中文"]), [""])
            self.assertEqual(get.call_count, 2)

    def test_model_reuse_idle_expiry_and_score_equivalence(self):
        indexer.unload_cross_encoder()
        model = SimpleNamespace(predict=lambda pairs, **kw: np.array([.8, -.3]))
        papers = [dict(title="solar", abstract="long abstract "*10, api_score=.9),
                  dict(title="battery", abstract="long abstract "*10, api_score=.8)]
        with patch.object(indexer, "CrossEncoder", return_value=model) as load, patch.object(indexer, "load_config", return_value={"search": {"ce_idle_seconds": .05}}):
            first = indexer.rank_papers("topic", papers, top_k=2, ce_candidates=2, primary_kw=["solar"])
            second = indexer.rank_papers("topic", papers, top_k=2, ce_candidates=2, primary_kw=["solar"])
            self.assertEqual(first, second)
            self.assertAlmostEqual(first[0][1], 1/(1+np.exp(-.8))+.12)
            self.assertEqual(load.call_count, 1)
            time.sleep(.09)
            self.assertIsNone(indexer._cross_encoder)
        indexer.unload_cross_encoder()

    def test_cancel_during_load_keeps_one_future_and_releases_lease(self):
        indexer.unload_cross_encoder()
        entered, finish_load = threading.Event(), threading.Event()
        model = SimpleNamespace(predict=lambda pairs, **kw: np.zeros(len(pairs)))
        run = SimpleNamespace(token=CancellationToken())
        def load(*a, **kw):
            entered.set()
            finish_load.wait(3)
            return model
        def rank():
            with run_scope(run):
                return indexer.rerank_with_cross_encoder("topic", [(dict(title="paper"), .5)])
        with patch.object(indexer, "CrossEncoder", side_effect=load) as factory:
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(rank)
                self.assertTrue(entered.wait(3))
                run.token.cancel()
                with self.assertRaises(OperationCancelled):
                    future.result(timeout=2)
                self.assertEqual(indexer._ce_users, 0)
                finish_load.set()
                result = indexer.rerank_with_cross_encoder("topic", [(dict(title="paper"), .5)])
                self.assertEqual(len(result), 1)
                self.assertEqual(factory.call_count, 1)
        indexer.unload_cross_encoder()

    def test_force_release_during_cancelled_prediction_waits_for_worker(self):
        indexer.unload_cross_encoder()
        entered, drain = threading.Event(), threading.Event()
        model = SimpleNamespace(predict=lambda *a, **k: (entered.set(), drain.wait(3), np.array([.8]))[-1])
        run = SimpleNamespace(token=CancellationToken())
        def rank():
            with run_scope(run):
                return indexer.rerank_with_cross_encoder("topic", [(dict(title="paper"), .5)])
        with patch.object(indexer, "CrossEncoder", return_value=model):
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(rank)
                self.assertTrue(entered.wait(3))
                run.token.cancel()
                with self.assertRaises(OperationCancelled):
                    future.result(timeout=2)
                indexer.unload_cross_encoder()
                self.assertIs(indexer._cross_encoder, model)
                self.assertEqual(indexer._ce_users, 1)
                drain.set()
                deadline = time.monotonic()+2
                while indexer._ce_users and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIsNone(indexer._cross_encoder)
        indexer.unload_cross_encoder()

    def test_second_sort_can_cancel_while_first_shares_cold_load(self):
        indexer.unload_cross_encoder()
        entered, finish_load, second_waiting = threading.Event(), threading.Event(), threading.Event()
        model = SimpleNamespace(predict=lambda pairs, **kw: np.zeros(len(pairs)))
        first_run = SimpleNamespace(token=CancellationToken())
        second_run = SimpleNamespace(token=CancellationToken())
        def load(*args, **kwargs):
            entered.set()
            if not finish_load.wait(5):
                raise TimeoutError("Test did not release the shared load")
            return model
        def rank(run, title):
            with run_scope(run):
                return indexer.rerank_with_cross_encoder("topic", [(dict(title=title), .5)])
        get_model = indexer._get_cross_encoder
        def shared_get():
            if current_run() is second_run:
                second_waiting.set()
            return get_model()
        try:
            with patch.object(indexer, "CrossEncoder", side_effect=load) as factory, \
                 patch.object(indexer, "_get_cross_encoder", side_effect=shared_get):
                with ThreadPoolExecutor(2) as pool:
                    first = pool.submit(rank, first_run, "first")
                    self.assertTrue(entered.wait(2))
                    second = pool.submit(rank, second_run, "second")
                    try:
                        self.assertTrue(second_waiting.wait(1.5))
                        second_run.token.cancel()
                        with self.assertRaises(OperationCancelled):
                            second.result(timeout=1.5)
                        self.assertEqual(indexer._ce_users, 1)
                        self.assertEqual(factory.call_count, 1)
                    finally:
                        finish_load.set()
                    self.assertEqual(len(first.result(timeout=2)), 1)
                    self.assertEqual(factory.call_count, 1)
        finally:
            finish_load.set()
            indexer.unload_cross_encoder()


class ModelTimeout(unittest.TestCase):
    def test_worker_timeout_failure_does_not_wait_for_global_deadline(self):
        indexer.unload_cross_encoder()
        paper = dict(title="Paper")
        with patch.object(indexer, "CrossEncoder", side_effect=TimeoutError()):
            started = time.perf_counter()
            result = indexer.rerank_with_cross_encoder("topic", [(paper, .8)])
            self.assertLess(time.perf_counter()-started, 2)
            self.assertEqual(result, [(paper, .5)])
        model = SimpleNamespace(predict=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
        with patch.object(indexer, "CrossEncoder", return_value=model):
            started = time.perf_counter()
            result = indexer.rerank_with_cross_encoder("topic", [(paper, .8)])
            self.assertLess(time.perf_counter()-started, 2)
            self.assertEqual(result, [(paper, .8)])
        indexer.unload_cross_encoder()

    def test_timeout_retains_model_until_prediction_drains(self):
        indexer.unload_cross_encoder()
        entered, drain = threading.Event(), threading.Event()
        def predict(*a, **kw):
            entered.set()
            drain.wait(3)
            return np.array([-1., 2.])
        model = SimpleNamespace(predict=predict)
        high, low = dict(title="High API"), dict(title="Low API")
        with patch.object(indexer, "CrossEncoder", return_value=model), \
             patch.object(indexer, "_CE_PREDICT_TIMEOUT", .05):
            result = indexer.rerank_with_cross_encoder("topic", [(high, .8), (low, .2)], top_k=1)
            self.assertTrue(entered.is_set())
            self.assertEqual(result, [(high, .8)])
            indexer.unload_cross_encoder()
            self.assertIs(indexer._cross_encoder, model)
            self.assertEqual(indexer._ce_users, 1)
            drain.set()
            deadline = time.monotonic()+2
            while indexer._ce_users and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(indexer._cross_encoder)
        indexer.unload_cross_encoder()


class HttpReuse(unittest.TestCase):
    def test_real_local_http_connection_reuse_and_separate_sessions(self):
        ports = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args): pass
            def do_GET(self):
                ports.append(self.client_address[1])
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            trace = SearchTrace()
            with trace_scope(trace):
                with base.source_http_scope("openalex"):
                    base.source_get(url, timeout=2)
                    base.source_get(url, timeout=2)
                with base.source_http_scope("europepmc"):
                    base.source_get(url, timeout=2)
            self.assertEqual(ports[0], ports[1])
            self.assertNotEqual(ports[1], ports[2])
            self.assertIsNone(base._http.get())
            self.assertEqual(trace.snapshot()["counters"]["openalex.http_requests"], 2)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        indexer.unload_cross_encoder()
