"""Bounded real three-source retrieval, English query supplied for Chinese topics.

Isolated runner required. No paid model calls; no TLS verification bypass.
"""
import argparse
import json
import time
from pathlib import Path
from paperpilot.search_metrics import SearchTrace, trace_scope
from paperpilot.sources import base, openalex_source as oa, arxiv_source as ax, europepmc_source as ep

parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true")
parser.add_argument("--count", type=int, default=400)
args = parser.parse_args()
if not args.live: raise SystemExit("Explicit --live required; only public scholarly APIs are called")
if not 1 <= args.count <= 400: raise SystemExit("count must be 1..400")
cases = [("自主机器人导航", "robot navigation"),
         ("Cancer immunotherapy", "cancer immunotherapy"),
         ("钙钛矿太阳能电池", "perovskite solar cells")]
report = {"count":args.count, "cloud_llm":False, "chinese_query_translation":"curated English terms, no LLM", "runs":[]}
for topic, term in cases:
    for name, adapter in (("arxiv", ax.fetch_arxiv), ("openalex", oa.fetch_openalex), ("europepmc", ep.fetch_europepmc)):
        for attempt in ("cold", "cached"):
            errors, trace = [], SearchTrace()
            print(f"LIVE SOURCE START topic={topic} source={name} mode={attempt}", flush=True)
            started = time.perf_counter(); papers = []
            try:
                with trace_scope(trace), base.source_http_scope(name):
                    if name == "openalex":
                        with oa.defer_abstracts(): papers = adapter([term], args.count, errors=errors, request_timeout=30)
                        oa.enrich_abstracts(papers, errors=errors)
                    else: papers = adapter([term], args.count, errors=errors, request_timeout=30)
            except base.SourceRateLimited as exc: errors.append((name,"rate_limited",f"HTTP {exc.status}"))
            row = dict(topic=topic, source=name, cache=attempt, seconds=time.perf_counter()-started,
                       returned=len(papers), abstracts=sum(bool(p.get("abstract")) for p in papers),
                       errors=errors, timing=trace.snapshot())
            report["runs"].append(row)
            (Path.cwd()/"sources_benchmark.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            print("LIVE SOURCE RESULT "+json.dumps({k:v for k,v in row.items() if k != "timing"},ensure_ascii=False),flush=True)
            if errors and not papers: break # don't repeat a failed request just to label it cached
print("SOURCE BENCHMARK FINISHED",flush=True)
