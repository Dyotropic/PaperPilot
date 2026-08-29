"""Europe PMC 数据源（自 fetcher.py 迁入，逻辑不变）。

生物医学 + 最新预印本，免 Key 免配置；按被引数降序（CITED desc）
排序以服务高引发现（智能推送铺垫），区别于相关度排序。
"""

import socket
import time

import requests

from paperpilot.sources.base import (
    PaperSource, SourceRateLimited, _build_search_query, cache_ttl_seconds,
    open_cache,
)

_EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_EPMC_CACHE_DIR = "europepmc"
# 排序：按被引数降序，服务高引发现（智能推送铺垫），区别于相关度排序
_EPMC_SORT = "CITED desc"

_CACHE_TTL = cache_ttl_seconds()
_epmc_cache = open_cache(_EPMC_CACHE_DIR)


def _parse_europepmc_result(r: dict) -> dict | None:
    """将 Europe PMC core 结果解析为统一 paper dict。"""
    title = (r.get("title") or "").strip()
    if not title:
        return None
    jinfo = r.get("journalInfo") or {}
    jtitle = (jinfo.get("journal") or {}).get("title")
    pub_year = jinfo.get("yearOfPublication") or r.get("pubYear")
    try:
        year = int(pub_year) if pub_year else None
    except (TypeError, ValueError):
        year = None
    doi = (r.get("doi") or "").strip() or None
    pmid = r.get("pmid")
    return {
        "title": title,
        "authors": r.get("authorString") or "",
        "abstract": (r.get("abstractText") or "").strip(),
        "year": year,
        "source": "europepmc",
        "url": f"https://europepmc.org/article/MED/{pmid}" if pmid else
               (f"https://doi.org/{doi}" if doi else None),
        "doi": doi,
        "type": None,
        "cited_by_count": r.get("citedByCount"),
        "journal": jtitle,
        "openalex_id": None,
    }


def _fetch_europepmc_raw(query: str, max_results: int = 30,
                         year_min: str = "", year_max: str = "") -> list[dict]:
    """Fetch papers from Europe PMC with a raw query string (internal helper)."""
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
                    year_min: str = "", year_max: str = "") -> list[dict]:
    """通过 Europe PMC API 检索论文（免 Key，生物医学+最新预印本）。

    排序按被引数降序（CITED desc），高引论文排前。
    """
    if not keywords:
        return []
    query = _build_search_query(keywords, logic=logic)
    return _fetch_europepmc_raw(query, max_results, year_min=year_min, year_max=year_max)


class EuropePMCSource(PaperSource):
    name = "europepmc"
    label = "Europe PMC"
    description = "生物医学 + 最新预印本，按被引数排序，免 Key 免配置"
    default_enabled = False
    raw_fetcher = staticmethod(_fetch_europepmc_raw)

    def fetch_raw(self, query: str, max_results: int = 30,
                  year_min: str = "", year_max: str = "") -> list[dict]:
        return _fetch_europepmc_raw(query, max_results, year_min=year_min, year_max=year_max)


def register() -> EuropePMCSource:
    src = EuropePMCSource()
    from paperpilot.sources.base import register_source
    register_source(src)
    return src


register()
