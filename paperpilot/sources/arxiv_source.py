"""arXiv 数据源（自 fetcher.py 迁入，逻辑不变）。

arXiv API 频控保守取 5s/次；arxiv 库底层 requests.Session 无默认超时，
用 ThreadPoolExecutor + future.result(timeout=45) 包裹防挂死。
"""

import time

import arxiv

from paperpilot.sources.base import PaperSource, SourceRateLimited, _build_search_query
from paperpilot.search_filters import SearchFilters, coerce_filters, search_limits

# arXiv API 频控：两次请求间隔 ≥ _ARXIV_RATE_LIMIT 秒
_ARXIV_LAST_CALL = 0.0
_ARXIV_RATE_LIMIT = 5.0  # arXiv 官方建议 ≤1 req/s，保守取 5s


def _wait_arxiv_rate_limit():
    """在 arXiv API 调用前等待，确保不触发限流。"""
    global _ARXIV_LAST_CALL
    elapsed = time.time() - _ARXIV_LAST_CALL
    if elapsed < _ARXIV_RATE_LIMIT:
        wait = _ARXIV_RATE_LIMIT - elapsed
        print(f"[arXiv] 频控等待 {wait:.1f}s...", flush=True)
        time.sleep(wait)
    _ARXIV_LAST_CALL = time.time()


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
    page_size = min(100, max(10, max_results))
    search = arxiv.Search(
        query=_filtered_query(query, filters),
        max_results=page_size * pages,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    client = arxiv.Client(page_size=page_size, num_retries=1, delay_seconds=3)
    original_get = client._session.get
    client._session.get = lambda *a, **kw: original_get(  # type: ignore[method-assign]
        *a, **({**kw, "timeout": timeout} if "timeout" not in kw else kw))
    papers: list[dict] = []
    seen_ids: set[str] = set()
    try:
        _wait_arxiv_rate_limit()
        for result in client.results(search):
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
    except Exception as exc:
        message = str(exc)
        if "429" in message or "403" in message:
            status = 429 if "429" in message else 403
            if not papers:
                raise SourceRateLimited("arxiv", status) from exc
            kind = "rate_limited"
        else:
            kind = "network"
        if errors is not None:
            errors.append(("arxiv", kind, f"arXiv 检索中断，已保留 {len(papers)} 篇合规结果"))
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
    """Fetch papers from arXiv with a raw query string (internal helper).

    Uses ThreadPoolExecutor + timeout to guard against the arxiv library's
    underlying requests.Session which has no default timeout and can hang.
    """
    effective_filters = coerce_filters(filters, year_min, year_max)
    if effective_filters is not None:
        return _fetch_arxiv_filtered(
            query, max_results, effective_filters, errors=errors,
            max_pages=max_pages, request_timeout=request_timeout)

    import concurrent.futures
    import random

    _wait_arxiv_rate_limit()

    print(f"[arXiv] 开始抓取: query={query[:80]}... max={max_results}", flush=True)

    client = arxiv.Client(num_retries=2, delay_seconds=3)
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    papers: list[dict] = []
    last_rate_status = 0  # 最后一次限流/被拒状态码（429/403），循环耗尽后用于抛异常

    for attempt in range(2):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                lambda: list(client.results(search))
            )
            results = future.result(timeout=45)
            for r in results:
                papers.append(_parse_arxiv_result(r))
            break
        except concurrent.futures.TimeoutError:
            print(f"[arXiv] 超时(45s) attempt {attempt+1}/2", flush=True)
            if attempt < 1:
                time.sleep(3)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "403" in msg:
                last_rate_status = 429 if "429" in msg else 403
                wait = (2 ** attempt) * 5 + random.uniform(0, 3)
                print(f"[arXiv] 限流(attempt {attempt+1}/2)，等待 {wait:.0f}s...", flush=True)
                time.sleep(wait)
            else:
                print(f"[arXiv] 错误: {e}", flush=True)
                break
        finally:
            executor.shutdown(wait=False)
    else:
        print(f"[arXiv] 请求超时/失败，返回空结果", flush=True)

    # 重试耗尽仍被限流/拒绝 → 抛异常（编排层捕获降级，UI 明确提示）
    if not papers and last_rate_status:
        raise SourceRateLimited("arxiv", last_rate_status)

    total = max(len(papers), 1)
    for i, p in enumerate(papers):
        p["api_score"] = 1.0 - (i / total)
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
