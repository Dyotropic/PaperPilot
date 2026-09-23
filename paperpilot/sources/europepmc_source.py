"""Europe PMC 数据源（自 fetcher.py 迁入，逻辑不变）。

生物医学 + 最新预印本，免 Key 免配置；按被引数降序（CITED desc）
排序以服务高引发现（智能推送铺垫），区别于相关度排序。
"""

import socket
import time
import re

import requests

from paperpilot.sources.base import (
    PaperSource, SourceRateLimited, _build_search_query, cache_ttl_seconds,
    open_cache,
)
from paperpilot.search_filters import SearchFilters, coerce_filters, search_limits

_EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_EPMC_CACHE_DIR = "europepmc"
# 排序：按被引数降序，服务高引发现（智能推送铺垫），区别于相关度排序
_EPMC_SORT = "CITED desc"

_CACHE_TTL = cache_ttl_seconds()
_epmc_cache = open_cache(_EPMC_CACHE_DIR)


def _parse_europepmc_result(r: dict) -> dict | None:
    """将 Europe PMC core 结果解析为统一 paper dict。"""
    if not isinstance(r, dict):
        return None
    title_value = r.get("title")
    title = title_value.strip() if isinstance(title_value, str) else ""
    if not title:
        return None
    jinfo = r.get("journalInfo") or {}
    if not isinstance(jinfo, dict):
        jinfo = {}
    journal_info = jinfo.get("journal") or {}
    if not isinstance(journal_info, dict):
        journal_info = {}
    jtitle = journal_info.get("title")
    pub_year = r.get("pubYear") or jinfo.get("yearOfPublication")
    try:
        year = int(pub_year) if pub_year else None
    except (TypeError, ValueError):
        year = None
    doi = (r.get("doi") or "").strip() or None
    pmid = r.get("pmid")
    author_names: list[str] = []
    for item in (r.get("authorList") or {}).get("author", []) or []:
        if not isinstance(item, dict):
            continue
        first = str(item.get("firstName") or "").strip()
        last = str(item.get("lastName") or "").strip()
        full = str(item.get("fullName") or "").strip()
        if first and last:
            author_names.extend((f"{first} {last}", f"{last} {first}"))
        if full:
            author_names.append(full)
    if not author_names:
        author_names = [a.strip() for a in str(r.get("authorString") or "").split(",") if a.strip()]
    source_id = str(r.get("source") or "").strip()
    record_id = str(r.get("id") or "").strip()
    stable_url = None
    if source_id and record_id and re.fullmatch(r"[A-Za-z0-9._-]+", source_id) \
            and re.fullmatch(r"[A-Za-z0-9._-]+", record_id):
        stable_url = f"https://europepmc.org/article/{source_id}/{record_id}"
    return {
        "title": title,
        "authors": r.get("authorString") or "",
        "abstract": (r.get("abstractText") or "").strip(),
        "year": year,
        "source": "europepmc",
        "url": f"https://europepmc.org/article/MED/{pmid}" if pmid else
               (f"https://doi.org/{doi}" if doi else stable_url),
        "doi": doi,
        "type": None,
        "cited_by_count": r.get("citedByCount"),
        "journal": jtitle,
        "openalex_id": None,
        "_author_names": list(dict.fromkeys(author_names)),
    }


def _quoted_field(value: str) -> str:
    return str(value).replace("\\", " ").replace('"', " ").strip()


def _epmc_filtered_query(query: str, filters: SearchFilters) -> str:
    clauses = [f"({query})"]
    if filters.year_from is not None and filters.year_to is not None:
        clauses.append(f"PUB_YEAR:[{filters.year_from} TO {filters.year_to}]")
    elif filters.year_from is not None:
        clauses.append(f"PUB_YEAR:[{filters.year_from} TO 9999]")
    elif filters.year_to is not None:
        clauses.append(f"PUB_YEAR:[1 TO {filters.year_to}]")
    if filters.author:
        clauses.append(f'AUTH:"{_quoted_field(filters.author)}"')
    if filters.journal:
        clauses.append(f'JOURNAL:"{_quoted_field(filters.journal)}"')
    return " AND ".join(clauses)


def _append_error(errors: list | None, kind: str, message: str) -> None:
    if errors is not None and not any(s == "europepmc" and k == kind and m == message
                                      for s, k, m in errors):
        errors.append(("europepmc", kind, message))


def _fetch_europepmc_filtered(query: str, max_results: int, filters: SearchFilters,
                              *, errors: list | None, max_pages: int | None,
                              request_timeout: float | None) -> list[dict]:
    if max_results <= 0:
        return []
    pages, timeout = search_limits(max_pages, request_timeout)
    page_size = min(1000, max(25, max_results))
    cursor = "*"
    seen_cursors: set[str] = set()
    papers: list[dict] = []
    seen_ids: set[str] = set()
    effective_query = _epmc_filtered_query(query, filters)
    for _ in range(pages):
        if cursor in seen_cursors:
            _append_error(errors, "incomplete", "Europe PMC 返回重复游标，分页已停止")
            break
        seen_cursors.add(cursor)
        params = {"query": effective_query, "format": "json", "resultType": "core",
                  "pageSize": page_size, "sort": _EPMC_SORT, "cursorMark": cursor}
        ckey = f"filtered:{effective_query}|{filters.cache_key}|{cursor}|{page_size}|{_EPMC_SORT}"
        data = _epmc_cache.get(ckey) if _epmc_cache is not None else None
        if data is not None and not isinstance(data, dict):
            _append_error(errors, "invalid_response",
                          "Europe PMC 缓存响应格式错误，分页已停止")
            break
        if data is None:
            try:
                response = requests.get(_EPMC_BASE, params=params,
                                        headers={"User-Agent": "PaperPilot/1.0"}, timeout=timeout)
                if response.status_code in (403, 429):
                    if not papers:
                        raise SourceRateLimited("europepmc", response.status_code)
                    _append_error(errors, "rate_limited",
                                  f"Europe PMC 分页受限，已保留 {len(papers)} 篇合规结果")
                    break
                response.raise_for_status()
            except SourceRateLimited:
                raise
            except requests.RequestException:
                _append_error(errors, "network",
                              f"Europe PMC 分页失败，已保留 {len(papers)} 篇合规结果")
                break
            try:
                data = response.json()
            except (ValueError, TypeError):
                _append_error(errors, "invalid_response",
                              f"Europe PMC 响应无法解析，已保留 {len(papers)} 篇合规结果")
                break
            if not isinstance(data, dict):
                _append_error(errors, "invalid_response",
                              f"Europe PMC 响应格式错误，已保留 {len(papers)} 篇合规结果")
                break
        result_list = data.get("resultList") or {}
        if not isinstance(result_list, dict):
            _append_error(errors, "invalid_response", "Europe PMC 结果列表格式错误")
            break
        results = result_list.get("result") or []
        if not isinstance(results, list):
            _append_error(errors, "invalid_response", "Europe PMC 结果列表格式错误")
            break
        if results and _epmc_cache is not None and data is not None:
            _epmc_cache.set(ckey, data, expire=_CACHE_TTL)
        if not results:
            break
        for item in results:
            if not isinstance(item, dict):
                _append_error(errors, "invalid_response",
                              "Europe PMC 跳过了格式错误的结果")
                continue
            paper = _parse_europepmc_result(item)
            if not paper:
                continue
            identity = str(paper.get("doi") or item.get("id") or item.get("pmid") or
                           (paper["title"], paper.get("year"))).casefold()
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            if filters.matches(paper):
                paper["api_score"] = max(0.0, 1.0 - len(papers) / max(max_results, 1))
                papers.append(paper)
                if len(papers) >= max_results:
                    break
        if len(papers) >= max_results:
            break
        next_cursor = str(data.get("nextCursorMark") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    if len(papers) < max_results:
        _append_error(errors, "incomplete",
                      f"Europe PMC 筛选后得到 {len(papers)}/{max_results} 篇，已耗尽结果或达到分页上限")
    return papers


def _fetch_europepmc_raw(query: str, max_results: int = 30,
                         year_min: str = "", year_max: str = "", *,
                         filters: SearchFilters | None = None,
                         errors: list | None = None,
                         max_pages: int | None = None,
                         request_timeout: float | None = None) -> list[dict]:
    """Fetch papers from Europe PMC with a raw query string (internal helper)."""
    effective_filters = coerce_filters(filters, year_min, year_max)
    if effective_filters is not None:
        return _fetch_europepmc_filtered(
            query, max_results, effective_filters, errors=errors,
            max_pages=max_pages, request_timeout=request_timeout)
    papers: list[dict] = []
    page_size = min(1000, max_results)
    params = {
        "query": query,
        "format": "json",
        "resultType": "core",  # core 才含 abstractText
        "pageSize": page_size,
        "sort": _EPMC_SORT,
    }
    if year_max or year_min:
        # EPMC 年份过滤（FIRST_PDATE 区间）
        if year_min and year_max:
            params["filter"] = f"FIRST_PDATE:[{year_min}-01-01 TO {year_max}-12-31]"
        elif year_min:
            params["filter"] = f"FIRST_PDATE:[{year_min}-01-01 TO *]"
        elif year_max:
            params["filter"] = f"FIRST_PDATE:[1900-01-01 TO {year_max}-12-31]"

    ckey = f"search:{query}|{page_size}|{params.get('filter', '')}|{_EPMC_SORT}"
    cached = None
    if _epmc_cache is not None:
        cached = _epmc_cache.get(ckey)
    if cached is not None:
        data = cached
        fetched = False
    else:
        data = None
        fetched = False
        last_status = 0
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(15)
        try:
            for attempt in range(3):
                try:
                    resp = requests.get(_EPMC_BASE, params=params,
                                        headers={"User-Agent": "PaperPilot/1.0"},
                                        timeout=15)
                    last_status = resp.status_code
                    if resp.status_code == 429:
                        time.sleep(1 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    body = resp.json()
                    data = (body.get("resultList") or {}).get("result") or []
                    fetched = True
                    if _epmc_cache is not None:
                        _epmc_cache.set(ckey, data, expire=_CACHE_TTL)
                    break
                except requests.RequestException:
                    time.sleep(1 * (attempt + 1))
                    continue
        finally:
            socket.setdefaulttimeout(old_timeout)
        # 3 次重试后仍 429 → 抛限流异常（编排层捕获降级，UI 明确提示）
        if data is None and last_status == 429:
            raise SourceRateLimited("europepmc", 429)

    if data is None:
        return []

    total = max(len(data), 1)
    collected: list[dict] = []
    for i, item in enumerate(data):
        paper = _parse_europepmc_result(item)
        if paper:
            paper["api_score"] = 1.0 - (i / total)
            collected.append(paper)
        if len(collected) >= max_results:
            break
    return collected[:max_results]


def fetch_europepmc(keywords: list[str], max_results: int = 30,
                    logic: str = "OR",
                    year_min: str = "", year_max: str = "", *,
                    filters: SearchFilters | None = None,
                    errors: list | None = None,
                    max_pages: int | None = None,
                    request_timeout: float | None = None) -> list[dict]:
    """通过 Europe PMC API 检索论文（免 Key，生物医学+最新预印本）。

    排序按被引数降序（CITED desc），高引论文排前。
    """
    if not keywords:
        return []
    query = _build_search_query(keywords, logic=logic)
    return _fetch_europepmc_raw(
        query, max_results, year_min=year_min, year_max=year_max,
        filters=filters, errors=errors, max_pages=max_pages,
        request_timeout=request_timeout)


class EuropePMCSource(PaperSource):
    name = "europepmc"
    label = "Europe PMC"
    description = "生物医学 + 最新预印本，按被引数排序，免 Key 免配置"
    default_enabled = False
    raw_fetcher = staticmethod(_fetch_europepmc_raw)

    def fetch_raw(self, query: str, max_results: int = 30,
                  year_min: str = "", year_max: str = "", **kwargs) -> list[dict]:
        return _fetch_europepmc_raw(query, max_results, year_min=year_min,
                                    year_max=year_max, **kwargs)


def register() -> EuropePMCSource:
    src = EuropePMCSource()
    from paperpilot.sources.base import register_source
    register_source(src)
    return src


register()
