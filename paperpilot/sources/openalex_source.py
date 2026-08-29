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
# 引用列表/关键词属于几乎不可变的历史事实，TTL 独立于检索页缓存（至少 30 天）
_REFS_TTL = max(_CACHE_TTL, 30 * 24 * 3600)

_oa_cache = open_cache(_OA_CACHE_DIR)


def _get_api_key() -> str:
    """OpenAlex API key（config data_sources.openalex_api_key，留空 = 无 key）。

    OpenAlex 自 2026-02-13 起废除 mailto polite pool 改为 API key 制：
    无 key 每日仅 100 credits，免费 key 100,000 credits/天。
    """
    return str((config.get("data_sources", {}) or {}).get("openalex_api_key", "") or "").strip()


def _normalize_doi(doi) -> str:
    """DOI 规范化：小写、去 https://doi.org/ 前缀；空值返回空串。"""
    if not doi:
        return ""
    d = str(doi).strip().lower()
    if d.startswith("https://doi.org/"):
        d = d[len("https://doi.org/"):]
    elif d.startswith("http://doi.org/"):
        d = d[len("http://doi.org/"):]
    return d


def _norm_oa_id(v) -> str:
    """OpenAlex ID 规范化：兼容 URL 与裸 W-id 两种形态，统一为 W 开头短 id。"""
    if not v:
        return ""
    return str(v).strip().rsplit("/", 1)[-1]


def _extract_work_refs_payload(w: dict) -> dict | None:
    """从 OpenAlex work JSON 提取图谱引用缓存载荷（无 DOI 返回 None）。

    载荷字段：doi（小写规范形）、openalex_id（W 短 id）、
    referenced_works（W 短 id 列表）、keywords（display_name 列表）。
    """
    doi = _normalize_doi(w.get("doi"))
    if not doi:
        return None
    keywords = [
        (k.get("display_name") or "").strip()
        for k in (w.get("keywords") or [])
        if isinstance(k, dict) and (k.get("display_name") or "").strip()
    ]
    return {
        "doi": doi,
        "openalex_id": _norm_oa_id(w.get("id")),
        "referenced_works": [_norm_oa_id(r) for r in (w.get("referenced_works") or []) if r],
        "keywords": keywords,
    }


def _cache_work_refs(w: dict) -> None:
    """检索解析时顺带缓存 work 的引用列表与关键词（图谱第一级数据源，零额外请求）。"""
    if _oa_cache is None:
        return
    payload = _extract_work_refs_payload(w)
    if payload:
        _oa_cache.set(f"refs:{payload['doi']}", payload, expire=_REFS_TTL)


def get_work_refs(dois: list, errors: list | None = None) -> dict:
    """按 DOI 批量获取 openalex_id / referenced_works / keywords（cache-aside）。

    图谱引用边的第二级数据源：先查 refs:{doi} 缓存，未命中的每批 ≤50 个 DOI
    一次请求补查（select 最小字段集），结果写回缓存。
    OpenAlex 未收录的 DOI 不做负缓存（下次构建仍会重查，一批请求开销可忽略）。

    Args:
        dois: 原始 DOI 列表（大小写 / https 前缀均可）
        errors: 可选收集列表 ("openalex", "rate_limited"|"network", 信息)；
                提供时限流/网络失败不抛异常而是记录后返回已得结果，
                未提供时持续 429 抛 SourceRateLimited（其余网络失败仍静默跳过）

    Returns:
        {小写doi: {"doi","openalex_id","referenced_works","keywords"}}（仅含查到的 DOI）
    """
    norm, seen = [], set()
    for d in dois:
        nd = _normalize_doi(d)
        if nd and nd not in seen:
            seen.add(nd)
            norm.append(nd)

    out: dict = {}
    missing: list = []
    for nd in norm:
        cached = _oa_cache.get(f"refs:{nd}") if _oa_cache is not None else None
        if cached is not None:
            out[nd] = cached
        else:
            missing.append(nd)

    api_key = _get_api_key()
    headers = {"User-Agent": "PaperPilot/1.0 (mailto:paperpilot@example.com)"}
    url = "https://api.openalex.org/works"
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(15)
    try:
        for i in range(0, len(missing), 50):
            batch = missing[i:i + 50]
            params = {
                "filter": "doi:" + "|".join(batch),
                "select": "id,doi,referenced_works,keywords",
                "per_page": 50,
                "mailto": "paperpilot@example.com",
            }
            if api_key:
                params["api_key"] = api_key
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                # 429 退避 2s/5s/10s（遵循 Retry-After），与检索链路同策略
                for wait in (2, 5, 10):
                    if resp.status_code != 429:
                        break
                    retry_after = resp.headers.get("Retry-After", "")
                    try:
                        time.sleep(min(float(retry_after), 30) if retry_after else wait)
                    except ValueError:
                        time.sleep(wait)
                    resp = requests.get(url, params=params, headers=headers, timeout=15)
                if resp.status_code == 429:
                    hint = "OpenAlex 被限流(429)，未能补查部分论文的引用关系"
                    if errors is not None:
                        errors.append(("openalex", "rate_limited", hint))
                        break
                    raise SourceRateLimited("openalex", 429, hint)
                resp.raise_for_status()
                for w in resp.json().get("results", []):
                    payload = _extract_work_refs_payload(w)
                    if not payload:
                        continue
                    out[payload["doi"]] = payload
                    if _oa_cache is not None:
                        _oa_cache.set(f"refs:{payload['doi']}", payload,
                                      expire=_REFS_TTL)
                time.sleep(0.1)  # 批间礼貌速率
            except requests.RequestException:
                # 网络失败：放弃该批继续（图谱降级为仅共现/时间线）
                if errors is not None:
                    errors.append(("openalex", "network", "OpenAlex 引用补查网络失败"))
                continue
    finally:
        socket.setdefaulttimeout(old_timeout)
    return out


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
                if fetched:
                    _cache_work_refs(w)  # 图谱第一级：检索响应本就带引用/关键词，顺带入库
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
                _cache_work_refs(w)  # 全量 work JSON 顺带缓存引用/关键词
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
