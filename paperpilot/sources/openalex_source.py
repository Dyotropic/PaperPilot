"""OpenAlex 数据源（自 fetcher.py 迁入，逻辑不变）。

搜索分页结果与摘要补齐均走 diskcache（TTL 来自 config cache.ttl_hours），
重复检索几乎瞬时返回；摘要补齐并发 4 路。
"""

import socket
import time

import requests

from paperpilot.config import config
from paperpilot.sources.base import (
    PaperSource, SourceRateLimited, _build_search_query, cache_ttl_seconds,
    open_cache,
)

_OA_CACHE_DIR = "openalex"
_CACHE_TTL = cache_ttl_seconds()

_oa_cache = open_cache(_OA_CACHE_DIR)


def _get_api_key() -> str:
    """OpenAlex API key（config data_sources.openalex_api_key，留空 = 无 key）。

    OpenAlex 自 2026-02-13 起废除 mailto polite pool 改为 API key 制：
    无 key 每日仅 100 credits，免费 key 100,000 credits/天。
    """
    return str((config.get("data_sources", {}) or {}).get("openalex_api_key", "") or "").strip()


def _parse_openalex_work(w: dict) -> dict | None:
    title = (w.get("title") or "").strip()
    if not title:
        return None
    authorship = w.get("authorships") or []
    authors = ", ".join(
        a.get("author", {}).get("display_name", "")
        for a in authorship[:10]
    )
    year = w.get("publication_year") or None
    doi = w.get("doi") or None
    if doi:
        doi = doi.removeprefix("https://doi.org/")
    abstract = ""
    abstract_inverted = w.get("abstract_inverted_index")
    if abstract_inverted:
        abstract = _decode_inverted_index(abstract_inverted)
    # 新增字段
    paper_type = w.get("type")  # "review", "article", "book-chapter", ...
    cited_by = w.get("cited_by_count")
    primary_loc = w.get("primary_location") or {}
    source_info = primary_loc.get("source") or {}
    journal = source_info.get("display_name") or None
    oa_id = w.get("id") or None  # "https://openalex.org/W2023271753"

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "year": year,
        "source": "openalex",
        "url": primary_loc.get("landing_page_url") or None,
        "doi": doi,
        "type": paper_type,
        "cited_by_count": cited_by,
        "journal": journal,
        "openalex_id": oa_id,
    }


def _decode_inverted_index(inv: dict) -> str:
    max_pos = max(p[-1] for p in inv.values())
    words = [""] * (max_pos + 1)
    for word, positions in inv.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words)


def _fetch_openalex_raw(query: str, max_results: int = 30,
                        year_min: str = "", year_max: str = "") -> list[dict]:
    """Fetch papers from OpenAlex with a raw query string (internal helper)."""
    url = "https://api.openalex.org/works"
    papers = []
    per_page = min(50, max_results)
    pages = (max_results + per_page - 1) // per_page
    headers = {"User-Agent": "PaperPilot/1.0 (mailto:paperpilot@example.com)"}
    api_key = _get_api_key()
    # 构建年份筛选
    year_filter = None
    if year_min and year_max:
        year_filter = f"publication_year:{year_min}-{year_max}"
    elif year_min:
        year_filter = f"publication_year:>{int(year_min)-1}"
    elif year_max:
        year_filter = f"publication_year:<{int(year_max)+1}"
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(15)
    try:
        for page in range(1, pages + 1):
            params = {
                "search": query,
                "per_page": per_page,
                "page": page,
                "mailto": "paperpilot@example.com",
            }
            if api_key:
                params["api_key"] = api_key
            if year_filter:
                params["filter"] = year_filter
            ckey = f"page:{query}|{page}|{per_page}|{year_filter or ''}"
            results = None
            fetched = False
            if _oa_cache is not None:
                results = _oa_cache.get(ckey)
            if results is None:
                try:
                    resp = requests.get(url, params=params, headers=headers, timeout=15)
                    # 429 退避：2s/5s/10s，优先遵循 Retry-After 头；耗尽且一无所获 → 抛限流异常
                    for wait in (2, 5, 10):
                        if resp.status_code != 429:
                            break
                        retry_after = resp.headers.get("Retry-After", "")
                        try:
                            time.sleep(min(float(retry_after), 30) if retry_after else wait)
                        except ValueError:
                            time.sleep(wait)
                        resp = requests.get(url, params=params, headers=headers, timeout=15)
                    if resp.status_code == 429 and not papers:
                        hint = "请在设置页配置 OpenAlex API Key（2026-02 起无 key 每日仅 100 次额度）"
                        raise SourceRateLimited("openalex", 429, hint)
                    resp.raise_for_status()
                    data = resp.json()
                    results = data.get("results", [])
                    fetched = True
                    if _oa_cache is not None:
                        _oa_cache.set(ckey, results, expire=_CACHE_TTL)
                except requests.RequestException:
                    continue
            page_total = len(results)
            for i, w in enumerate(results):
                paper = _parse_openalex_work(w)
                if paper:
                    api_rel = w.get("relevance_score")
                    if api_rel is not None:
                        paper["api_score"] = float(api_rel)
                    else:
                        paper["api_score"] = 1.0 - (i / max(page_total, 1))
                    papers.append(paper)
            if len(papers) >= max_results:
                break
            if fetched:
                time.sleep(0.1)  # 网络请求后的礼貌速率
    finally:
        socket.setdefaulttimeout(old_timeout)
    # Normalize api_score to [0, 1] — OpenAlex relevance_score may exceed [0, 1]
    api_scores = [p.get("api_score") for p in papers if p.get("api_score") is not None]
    if api_scores:
        min_s, max_s = min(api_scores), max(api_scores)
        if max_s > 1.0 or min_s < 0.0:
            if max_s > min_s:
                for p in papers:
                    if p.get("api_score") is not None:
                        p["api_score"] = (p["api_score"] - min_s) / (max_s - min_s)
            else:
                for p in papers:
                    if p.get("api_score") is not None:
                        p["api_score"] = 0.5
    # Fill remaining None with position-based scores
    total = max(len(papers), 1)
    for i, p in enumerate(papers):
        if p.get("api_score") is None:
            p["api_score"] = 1.0 - (i / total)
    papers = papers[:max_results]
    papers = _fetch_missing_abstracts(papers)
    return papers


def _fetch_missing_abstracts(papers: list[dict]) -> list[dict]:
    """对缺少摘要的 OpenAlex 论文，并行请求完整摘要（带缓存）。

    OpenAlex 搜索结果常截断摘要，需用 works/{id} 端点获取完整数据。
    并发 4 路，较原串行 ~10 req/s 更快且更礼貌；重复检索命中缓存。
    """
    to_fetch = [
        p for p in papers
        if p.get("openalex_id") and not (p.get("abstract") or "").strip()
    ]

    if not to_fetch:
        return papers

    headers = {"User-Agent": "PaperPilot/1.0 (mailto:paperpilot@example.com)"}
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(10)

    def _fetch_one(paper: dict) -> bool:
        oa_id = paper["openalex_id"]
        ckey = f"abs:{oa_id}"
        if _oa_cache is not None:
            cached = _oa_cache.get(ckey)
            if cached:
                paper["abstract"] = cached
                return True
        try:
            resp = requests.get(oa_id, headers=headers, timeout=10)
            if resp.status_code == 200:
                w = resp.json()
                inv = w.get("abstract_inverted_index")
                if inv:
                    text = _decode_inverted_index(inv)
                    paper["abstract"] = text
                    if _oa_cache is not None:
                        _oa_cache.set(ckey, text, expire=_CACHE_TTL)
                    return True
        except requests.RequestException:
            pass
        return False

    count = 0
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            count = sum(1 for ok in executor.map(_fetch_one, to_fetch) if ok)
    finally:
        socket.setdefaulttimeout(old_timeout)

    if count:
        print(f"[OpenAlex] 补齐 {count} 篇摘要", flush=True)
    return papers


def fetch_openalex(keywords: list[str], max_results: int = 30,
                   logic: str = "OR",
                   year_min: str = "", year_max: str = "") -> list[dict]:
    """通过 OpenAlex API 检索论文（免 Key）。"""
    if not keywords:
        return []
    query = _build_search_query(keywords, logic=logic)
    return _fetch_openalex_raw(query, max_results, year_min=year_min, year_max=year_max)


class OpenAlexSource(PaperSource):
    name = "openalex"
    label = "OpenAlex"
    description = "正式发表论文聚合索引，含引用数与期刊信息，免 Key"
    default_enabled = True
    raw_fetcher = staticmethod(_fetch_openalex_raw)

    def fetch_raw(self, query: str, max_results: int = 30,
                  year_min: str = "", year_max: str = "") -> list[dict]:
        return _fetch_openalex_raw(query, max_results, year_min=year_min, year_max=year_max)


def register() -> OpenAlexSource:
    src = OpenAlexSource()
    from paperpilot.sources.base import register_source
    register_source(src)
    return src


register()
