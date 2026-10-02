"""Explicit real OpenAlex / cached CPU model benchmark in the isolated runner.

No cloud LLM calls or model downloads. API calls require --live-openalex;
--cached-model permits local CPU inference. Reports no paper text or credentials.
"""
import argparse
import json
import time
import shutil
from pathlib import Path
from unittest.mock import patch
import numpy as np
from difflib import SequenceMatcher
from paperpilot import fetcher

from paperpilot import indexer, config
from paperpilot.search_metrics import SearchTrace
from paperpilot.sources import openalex_source as oa
from pages import search_page

parser = argparse.ArgumentParser()
parser.add_argument("--live-openalex", action="store_true")
parser.add_argument("--cached-model", action="store_true")
parser.add_argument("--seed-cache", type=Path, help="Copy a prior isolated API cache into this run")
parser.add_argument("--count", type=int, default=400, choices=(100, 400))
args = parser.parse_args()
if args.seed_cache:
    root = Path(__file__).resolve().parents[1]
    seed = (root / args.seed_cache).resolve()
    if not seed.is_relative_to(root) or not any(part.startswith(".validation-") for part in seed.parts):
        raise SystemExit("Seed must be an isolated project validation cache")
    destination = Path(oa._oa_cache.directory)
    oa._oa_cache.close()
    shutil.copytree(seed, destination, dirs_exist_ok=True)
    from paperpilot.sources.base import open_cache
    oa._oa_cache = open_cache("openalex")
if not args.live_openalex and not args.cached_model:
    raise SystemExit("Choose an explicit benchmark boundary")
model_factory = indexer.CrossEncoder
def model(*a, **kw):
    if args.cached_model:
        if not Path(indexer._CE_PATH).exists():
            raise RuntimeError("No cached model; downloads are disabled")
        return model_factory(indexer._CE_PATH, **kw)
    return type("Model", (), {"predict": lambda self, pairs, **kw: np.zeros(len(pairs))})()

def synthetic(**kw):
    return [dict(title=f"Solar cell stability experiment {i}", source="openalex",
                 doi=f"10.0/synthetic-{i}", year=2024, api_score=1-i/args.count,
                 abstract="We measured the stability of perovskite solar cells under heat and humidity. "*8)
            for i in range(args.count)]

config.save_config({"search": {"ce_idle_seconds": 300}})
context = dict(topic_desc="stability of perovskite solar cells",
               primary_keywords=["perovskite solar cells"], secondary_keywords=[], regular_keywords=[])
summaries = []
raw_rows = []
original_dedup = search_page.deduplicate
def dedup(papers):
    if not raw_rows:
        raw_rows.extend(papers)
    return original_dedup(papers)
try:
    with patch.object(indexer, "CrossEncoder", side_effect=model), patch.object(search_page, "deduplicate", side_effect=dedup):
        patch_source = patch.object(search_page, "fetch_multi_primary", side_effect=synthetic)
        patch_desc = patch.object(search_page, "fetch_openalex", return_value=[])
        if not args.live_openalex:
            patch_source.start(); patch_desc.start()
        try:
            for boundary in ("cold", "warm"):
                trace = SearchTrace()
                context["_metrics_trace"] = trace
                started = time.perf_counter()
                papers, scores, errors = search_page._run_pipeline(
                    args.count, "", "", False, True, False, 50, 100, search_context=context)
                snap = trace.snapshot()
                summary = dict(boundary=boundary, real_api=args.live_openalex,
                               seeded_cache=bool(args.seed_cache),
                               real_cpu_model=args.cached_model, papers=len(papers), results=len(scores),
                               seconds=round(time.perf_counter()-started, 3),
                               errors=[dict(source=s, kind=k) for s, k, _ in errors],
                               counters=snap["counters"], stages=snap["stages"])
                summaries.append(summary)
                print("BENCHMARK " + json.dumps(summary), flush=True)
                if errors or not scores:
                    break
        finally:
            if not args.live_openalex:
                patch_source.stop(); patch_desc.stop()
    if raw_rows:
        started = time.perf_counter()
        baseline, norms = [], []
        for paper in raw_rows:
            norm = fetcher._normalize_title(paper["title"])
            duplicate = False
            for previous in norms:
                a, b = sorted((len(norm), len(previous)))
                if b*9 > a*11:
                    continue
                if norm == previous or SequenceMatcher(None, norm, previous).ratio() >= .9:
                    duplicate = True
                    break
            if not duplicate:
                baseline.append(paper)
                norms.append(norm)
        baseline_seconds = time.perf_counter()-started
        started = time.perf_counter()
        optimized = original_dedup(raw_rows)
        optimized_seconds = time.perf_counter()-started
        assert baseline == optimized, "Dedup result differs from the original algorithm"
        differential = dict(input=len(raw_rows), output=len(optimized), same_order_and_content=True,
                            baseline_seconds=round(baseline_seconds, 3), optimized_seconds=round(optimized_seconds, 3))
        print("DEDUP DIFFERENTIAL " + json.dumps(differential), flush=True)
    else:
        differential = None
    (Path.cwd()/"benchmark_report.json").write_text(json.dumps(dict(
        query=context["topic_desc"], primary=context["primary_keywords"], runs=summaries,
        dedup_differential=differential), indent=2), encoding="utf-8")
finally:
    indexer.unload_cross_encoder()
    for cache in (oa._oa_cache,):
        if cache is not None:
            cache.close()
if args.live_openalex and (not summaries or not summaries[0]["results"]):
    raise SystemExit("Real API benchmark did not complete; see error kinds above")
