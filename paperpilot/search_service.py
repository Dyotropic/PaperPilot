"""Concurrent sources, sequential work inside each source, stable merge order."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import copy_context
from contextlib import nullcontext
import threading

from paperpilot.agent_runtime import checkpoint
from paperpilot.sources.base import SourceRateLimited, source_http_scope
from paperpilot.search_metrics import timed_stage, count

SOURCE_LABELS = dict(arxiv="arXiv", openalex="OpenAlex", europepmc="Europe PMC")
_FATAL = {"rate_limited", "network", "no_match", "invalid_response"}


def collect_sources(*, sources, primary_kw, secondary_kw, regular_kw, description,
                    max_per, year_min, year_max, filters, cascade, multi_primary,
                    description_fetchers, parallel=True, on_progress=None):
    """Return (papers, errors); workers never mutate page state or a shared error list.

    Callables are supplied by the UI so existing source adapters and caller mocks
    keep the same public interfaces. Context copies preserve cancellation/usage.
    """
    sources = tuple(s for s in SOURCE_LABELS if s in sources)
    if not sources:
        return [], []
    phases, phase_lock = dict.fromkeys(sources, "等待"), threading.Lock()

    def phase(source, text):
        with phase_lock:
            phases[source] = text

    def notify():
        if on_progress is not None:
            with phase_lock:
                text = " · ".join(f"{SOURCE_LABELS[s]} {phases[s]}" for s in sources)
            on_progress(text)

    def worker(source):
        from paperpilot.sources.openalex_source import defer_abstracts, enrich_abstracts
        papers, errors = [], []
        scope = defer_abstracts() if source == "openalex" else nullcontext()
        with timed_stage("source", source=source), source_http_scope(source):
            with scope:
                checkpoint()
                phase(source, "关键词召回中")
                try:
                    with timed_stage("keyword_recall", source=source):
                        kwargs = dict(primary_kw=list(primary_kw), secondary_kw=list(secondary_kw),
                                      regular_kw=list(regular_kw), source=source,
                                      max_results=max_per, min_results=3, year_min=year_min,
                                      year_max=year_max, errors=errors, filters=filters)
                        if source == "arxiv":
                            papers, _ = cascade(**kwargs)
                        else:
                            papers = multi_primary(**kwargs)
                except SourceRateLimited as exc:
                    errors.append((source, "rate_limited", exc.message))
                except Exception:
                    errors.append((source, "error", f"{SOURCE_LABELS[source]} 检索失败"))
                checkpoint()
                # Keep the existing description recall contract. A new total cap
                # would change recall quality and is deliberately not introduced.
                stopped = any(kind in _FATAL for _, kind, _ in errors)
                if description and not stopped and (filters is None or len(papers) < max_per):
                    phase(source, "描述召回中")
                    try:
                        with timed_stage("description_recall", source=source):
                            extra = description_fetchers[source](
                                [description], max_results=max_per, logic="OR",
                                year_min=year_min, year_max=year_max,
                                filters=filters, errors=errors)
                            papers = list(papers) + extra
                    except SourceRateLimited as exc:
                        errors.append((source, "rate_limited", exc.message))
                    except Exception:
                        errors.append((source, "error", f"{SOURCE_LABELS[source]} 描述检索失败"))
            checkpoint()
            if source == "openalex":
                phase(source, "摘要补齐中")
                with timed_stage("abstracts", source=source):
                    enrich_abstracts(papers, errors=errors)
            checkpoint()
            count("recalled", len(papers), source=source)
            phase(source, f"完成 {len(papers)} 篇")
            return papers, errors

    results = {}
    if not parallel or len(sources) == 1:
        for source in sources:
            phase(source, "启动")
            notify()
            results[source] = worker(source)
            notify()
    else:
        pool = ThreadPoolExecutor(max_workers=len(sources), thread_name_prefix="PaperPilot-Search")
        futures = {}
        try:
            for source in sources:
                checkpoint()
                futures[pool.submit(copy_context().run, worker, source)] = source
            pending = set(futures)
            while pending:
                checkpoint()
                notify()
                done, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    results[futures[future]] = future.result()
        finally:
            # Keep the search owned until its already-running HTTP calls drain.
            # Each request has a timeout; checkpoints block subsequent operations.
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
    checkpoint()
    notify()
    papers, errors = [], []
    for source in sources:
        rows, source_errors = results[source]
        papers.extend(rows)
        errors.extend(source_errors)
    return papers, errors
