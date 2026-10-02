"""arXiv SDK pagination with cached queries and cancellable HTTP rate control."""

import time
import threading
from copy import deepcopy
from paperpilot.search_metrics import timed_stage, count
from paperpilot.agent_runtime import checkpoint, interruptible_wait

import arxiv

from paperpilot.sources.base import (
    PaperSource, SourceRateLimited, _build_search_query, source_http_active,
    source_get, open_cache, cache_ttl_seconds,
)
from paperpilot.search_filters import SearchFilters, coerce_filters, search_limits

# arXiv API 频控：两次请求间隔 ≥ _ARXIV_RATE_LIMIT 秒
_ARXIV_LAST_CALL = 0.0
_ARXIV_RATE_LIMIT = 3.0  # https://info.arxiv.org/help/api/user-manual.html
_ARXIV_RATE_LOCK = threading.Lock()
_arxiv_cache = open_cache("arxiv")
_CACHE_TTL = cache_ttl_seconds()


def _wait_arxiv_rate_limit():
    """在 arXiv API 调用前等待，确保不触发限流。"""
    global _ARXIV_LAST_CALL
    # Reserve one global request slot, including across simultaneous searches.
    # Polling the lock preserves cancellation while another run waits its turn.
    while not _ARXIV_RATE_LOCK.acquire(timeout=.1):
        checkpoint()
    try:
        interruptible_wait(max(0, _ARXIV_RATE_LIMIT - (time.monotonic() - _ARXIV_LAST_CALL)))
        checkpoint()
        _ARXIV_LAST_CALL = time.monotonic()
    finally:
        _ARXIV_RATE_LOCK.release()


def _client(page_size, timeout):
    # The SDK still parses feeds and retries a failed page once. Our HTTP guard
    # owns rate control, avoiding a second, uncancellable SDK sleep/retry layer.
    client = arxiv.Client(page_size=page_size, num_retries=1, delay_seconds=0)
    original_get = client._session.get
    def guarded_get(*args, **kwargs):
        checkpoint()
        _wait_arxiv_rate_limit()
        kwargs.setdefault("timeout", timeout)
        if source_http_active():
            response = source_get(*args, **kwargs)
        else:
            count("http_requests", source="arxiv")
            with timed_stage("http", source="arxiv"):
                response = original_get(*args, **kwargs)
        checkpoint()
        if getattr(response, "status_code", None) == 403:
            raise SourceRateLimited("arxiv", 403)
        return response
    client._session.get = guarded_get
    parse_feed = getattr(client, "_parse_feed", None)
    if callable(parse_feed):
        def checked_feed(*args, **kwargs):
            feed = parse_feed(*args, **kwargs)
            if getattr(feed, "malformed", False):
                raise ValueError("Invalid arXiv feed")
            return feed
        client._parse_feed = checked_feed
    return client


def _cached_query(key):
    checkpoint()
    rows = _arxiv_cache.get(key) if _arxiv_cache is not None else None
    if rows is not None:
        count("query_cache_hits", source="arxiv")
        return deepcopy(rows)
    return None


def _record_failure(exc, papers, errors):
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    # SDK HTTPError exposes status; retain compatibility with older SDK errors.
    if status not in (403, 429):
        message = str(exc)
        status = 429 if "429" in message else (403 if "403" in message else None)
    if status in (403, 429) and not papers:
        raise SourceRateLimited("arxiv", status) from exc
    if errors is not None:
        kind = "rate_limited" if status in (403, 429) else (
            "invalid_response" if isinstance(exc, (ValueError, TypeError)) else "network")
        errors.append(("arxiv", kind,
                       f"arXiv 检索中断，已保留 {len(papers)} 篇结果"))


def _parse_arxiv_result(r) -> dict:
    author_names = [a.name for a in r.authors]
    authors = ", ".join(author_names)
    year = r.published.year if r.published else None
    doi = None
    if r.doi:
        doi = r.doi if r.doi.startswith("10.") else None
    return {
        "title": r.title.strip() if r.title else "",
        "authors": authors,
        "abstract": r.summary.strip() if r.summary else "",
        "year": year,
        "source": "arxiv",
        "url": r.entry_id or None,
        "doi": doi,
        "type": None,
        "cited_by_count": None,
        "journal": getattr(r, "journal_ref", None) or None,
        "openalex_id": None,
        "_author_names": author_names,
    }


def _filtered_query(query: str, filters: SearchFilters) -> str:
    clauses = [f"({query})"]
    if filters.author:
        clauses.append(f'au:"{filters.author.replace(chr(34), "")}"')
    if filters.journal:
        clauses.append(f'jr:"{filters.journal.replace(chr(34), "")}"')
    if filters.year_from is not None or filters.year_to is not None:
        start = filters.year_from or 1
        end = filters.year_to or 9999
        clauses.append(f"submittedDate:[{start:04d}01010000 TO {end:04d}12312359]")
    return " AND ".join(clauses)


def _fetch_arxiv_filtered(query: str, max_results: int, filters: SearchFilters,
                          *, errors: list | None, max_pages: int | None,
                          request_timeout: float | None) -> list[dict]:
    if max_results <= 0:
        return []
    pages, timeout = search_limits(max_pages, request_timeout)
    page_size = min(400, max(10, max_results))
    effective_query = _filtered_query(query, filters)
    ckey = f"filtered-v1:{effective_query}|{filters.cache_key}|{max_results}|{page_size}|{pages}"
    cached = _cached_query(ckey)
    if cached is not None:
        if len(cached) < max_results and errors is not None:
            errors.append(("arxiv", "incomplete", f"arXiv 筛选后得到 {len(cached)}/{max_results} 篇"))
        return cached
    search = arxiv.Search(
        query=effective_query,
        max_results=page_size * pages,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    client = _client(page_size, timeout)
    papers: list[dict] = []
    seen_ids: set[str] = set()
    try:
        for result in client.results(search):
            checkpoint()
            paper = _parse_arxiv_result(result)
            identity = str(paper.get("url") or paper.get("doi") or paper.get("title") or "").casefold()
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            if filters.matches(paper):
                paper["api_score"] = max(0.0, 1.0 - len(papers) / max(max_results, 1))
                papers.append(paper)
                if len(papers) >= max_results:
                    break
        checkpoint()
        if papers and _arxiv_cache is not None:
            _arxiv_cache.set(ckey, papers, expire=_CACHE_TTL)
    except Exception as exc:
        _record_failure(exc, papers, errors)
    finally:
        client._session.close()
    if len(papers) < max_results and errors is not None:
        errors.append(("arxiv", "incomplete",
                       f"arXiv 筛选后得到 {len(papers)}/{max_results} 篇，已达到分页或数据上限"))
    return papers


def _fetch_arxiv_raw(query: str, max_results: int = 30,
                     year_min: str = "", year_max: str = "", *,
                     filters: SearchFilters | None = None,
                     errors: list | None = None,
                     max_pages: int | None = None,
                     request_timeout: float | None = None) -> list[dict]:
    """Fetch sequential SDK pages, retaining partial results on page failure."""
    if max_results <= 0:
        return []
    effective_filters = coerce_filters(filters, year_min, year_max)
    if effective_filters is not None:
        return _fetch_arxiv_filtered(
            query, max_results, effective_filters, errors=errors,
            max_pages=max_pages, request_timeout=request_timeout)

    _, timeout = search_limits(max_pages, request_timeout)
    ckey = f"search-v1:{query}|{max_results}|relevance"
    cached = _cached_query(ckey)
    if cached is not None:
        return cached
    client = _client(min(400, max_results), timeout)
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    papers: list[dict] = []
    complete = False
    try:
        for result in client.results(search):
            checkpoint()
            papers.append(_parse_arxiv_result(result))
        complete = True
    except Exception as exc:
        _record_failure(exc, papers, errors)
    finally:
        client._session.close()
    total = max(len(papers), 1)
    for i, p in enumerate(papers):
        p["api_score"] = 1.0 - (i / total)
    checkpoint()
    if complete and papers and _arxiv_cache is not None:
        _arxiv_cache.set(ckey, papers, expire=_CACHE_TTL)
    return papers


def fetch_arxiv(keywords: list[str], max_results: int = 30,
                logic: str = "OR",
                year_min: str = "", year_max: str = "", *,
                filters: SearchFilters | None = None,
                errors: list | None = None,
                max_pages: int | None = None,
                request_timeout: float | None = None) -> list[dict]:
    """通过 arXiv API 检索论文。

    Args:
        keywords: 关键词列表
        max_results: 最大返回数
        logic: "OR"（宽召回，默认）或 "AND"（核心词全部命中）

    Returns:
        paper dict 列表，含 api_score 字段（0.0-1.0，API 排序位置归一化）
    """
    if not keywords:
        return []
    query = _build_search_query(keywords, logic=logic)
    return _fetch_arxiv_raw(
        query, max_results, year_min=year_min, year_max=year_max,
        filters=filters, errors=errors, max_pages=max_pages,
        request_timeout=request_timeout)


class ArxivSource(PaperSource):
    name = "arxiv"
    label = "arXiv"
    description = "预印本平台，覆盖 CS / 物理 / 数学等，免 Key"
    default_enabled = True
    raw_fetcher = staticmethod(_fetch_arxiv_raw)

    def fetch_raw(self, query: str, max_results: int = 30,
                  year_min: str = "", year_max: str = "", **kwargs) -> list[dict]:
        return _fetch_arxiv_raw(query, max_results, year_min=year_min,
                                year_max=year_max, **kwargs)


def register() -> ArxivSource:
    src = ArxivSource()
    from paperpilot.sources.base import register_source
    register_source(src)
    return src


register()
