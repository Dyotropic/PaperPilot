"""OpenAlex 数据源（自 fetcher.py 迁入，逻辑不变）。

搜索分页结果与摘要补齐均走 diskcache（TTL 来自 config cache.ttl_hours），
摘要按唯一 work ID 批量补齐；检索流水线可延迟到该源召回结束后补齐。
"""

import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from paperpilot.agent_runtime import checkpoint, interruptible_wait

import requests

from paperpilot.config import load_config
from paperpilot.sources.base import (
    PaperSource, SourceRateLimited, _build_search_query, cache_ttl_seconds,
    open_cache, source_get,
)
from paperpilot.search_metrics import count
from paperpilot.search_filters import (
    SearchFilters, author_matches, coerce_filters, normalize_name, search_limits,
)

_OA_CACHE_DIR = "openalex"
_CACHE_TTL = cache_ttl_seconds()
# 引用列表/关键词属于几乎不可变的历史事实，TTL 独立于检索页缓存（至少 30 天）
_REFS_TTL = max(_CACHE_TTL, 30 * 24 * 3600)

_oa_cache = open_cache(_OA_CACHE_DIR)
_defer_abstracts = ContextVar("openalex_defer_abstracts", default=False)


@contextmanager
def defer_abstracts():
    """Defer enrichment only within this source job, never across searches."""
    token = _defer_abstracts.set(True)
    try:
        yield
    finally:
        _defer_abstracts.reset(token)


def _get_api_key() -> str:
    """OpenAlex API key（config data_sources.openalex_api_key，留空 = 无 key）。

    Key 用于账户鉴权/配额；实际可用额度以账户及服务端响应为准。

    每次调用现读 config.yaml（不用 import 期冻结的模块单例），
    设置页保存 Key 后立即生效，无需重启。
    """
    return str((load_config().get("data_sources", {}) or {}).get("openalex_api_key", "") or "").strip()


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
    if _oa_cache is None or not isinstance(w, dict):
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
            checkpoint()
            resp = source_get(url, params=params, headers=headers, timeout=15)
            # 429 退避 2s/5s/10s（遵循 Retry-After），与检索链路同策略
            for wait in (2, 5, 10):
                if resp.status_code != 429:
                    break
                retry_after = resp.headers.get("Retry-After", "")
                try:
                    interruptible_wait(min(float(retry_after), 30) if retry_after else wait)
                except ValueError:
                    interruptible_wait(wait)
                checkpoint()
                resp = source_get(url, params=params, headers=headers, timeout=15)
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
            interruptible_wait(0.1)  # 批间礼貌速率
        except requests.RequestException:
            # 网络失败：放弃该批继续（图谱降级为仅共现/时间线）
            if errors is not None:
                errors.append(("openalex", "network", "OpenAlex 引用补查网络失败"))
            continue
    return out


def _parse_openalex_work(w: dict) -> dict | None:
    if not isinstance(w, dict):
        return None
    title_value = w.get("title")
    title = title_value.strip() if isinstance(title_value, str) else ""
    if not title:
        return None
    authorship = w.get("authorships") or []
    if not isinstance(authorship, list):
        authorship = []
    author_names = [
        (a.get("author") or {}).get("display_name", "")
        for a in authorship
        if isinstance(a, dict) and isinstance(a.get("author"), dict)
        and (a.get("author") or {}).get("display_name", "")
    ]
    authors = ", ".join(author_names)
    year = w.get("publication_year") or None
    doi = w.get("doi") or None
    if doi:
        doi = doi.removeprefix("https://doi.org/")
    abstract = ""
    abstract_inverted = w.get("abstract_inverted_index")
    if abstract_inverted:
        try:
            abstract = _decode_inverted_index(abstract_inverted)
        except (ValueError, TypeError, IndexError, KeyError):
            pass  # Keep the paper; enrichment can retry the malformed field.
    # 新增字段
    paper_type = w.get("type")  # "review", "article", "book-chapter", ...
    cited_by = w.get("cited_by_count")
    primary_loc = w.get("primary_location") or {}
    if not isinstance(primary_loc, dict):
        primary_loc = {}
    source_info = primary_loc.get("source") or {}
    if not isinstance(source_info, dict):
        source_info = {}
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
        "_author_names": author_names,
    }


def _decode_inverted_index(inv: dict) -> str:
    if not isinstance(inv, dict) or not inv:
        raise ValueError("Invalid abstract index")
    if any(not isinstance(word, str) or not isinstance(positions, list) or not positions
           or any(not isinstance(pos, int) or pos < 0 or pos > 100000 for pos in positions)
           for word, positions in inv.items()):
        raise ValueError("Invalid abstract positions")
    max_pos = max(max(p) for p in inv.values())
    words = [""] * (max_pos + 1)
    for word, positions in inv.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words)


def _append_error(errors: list | None, kind: str, message: str) -> None:
    if errors is not None and not any(s == "openalex" and k == kind and m == message
                                      for s, k, m in errors):
        errors.append(("openalex", kind, message))


def _resolve_openalex_entities(kind: str, name: str, *, timeout: float,
                               errors: list | None) -> list[str]:
    """Resolve an exact display name to all matching OpenAlex entity IDs."""
    wanted = normalize_name(name)
    ckey = f"entity:{kind}:{wanted}"
    cached = _oa_cache.get(ckey) if _oa_cache is not None else None
    if cached is not None:
        if isinstance(cached, dict):
            warning = cached.get("warning")
            if warning:
                _append_error(errors, cached.get("kind", "truncated"), warning)
            cached_ids = cached.get("ids") or []
            if isinstance(cached_ids, list):
                return list(cached_ids)
        elif isinstance(cached, list):
            return list(cached)
        _append_error(errors, "invalid_response",
                      f"OpenAlex {kind} 名称解析缓存格式错误")
        return []
    url = f"https://api.openalex.org/{kind}"
    params = {"search": name, "per_page": 50, "mailto": "paperpilot@example.com"}
    api_key = _get_api_key()
    if api_key:
        params["api_key"] = api_key
    try:
        checkpoint()
        response = source_get(url, params=params,
                                headers={"User-Agent": "PaperPilot/1.0"}, timeout=timeout)
        if response.status_code in (403, 429):
            _append_error(errors, "rate_limited",
                          f"OpenAlex {kind} 名称解析受限（HTTP {response.status_code}）")
            return []
        response.raise_for_status()
    except requests.RequestException:
        _append_error(errors, "network", f"OpenAlex {kind} 名称解析失败")
        return []
    try:
        payload = response.json()
    except (ValueError, TypeError):
        _append_error(errors, "invalid_response",
                      f"OpenAlex {kind} 名称解析响应无法解析")
        return []
    if not isinstance(payload, dict):
        _append_error(errors, "invalid_response", f"OpenAlex {kind} 名称解析响应格式错误")
        return []
    results = payload.get("results") or []
    if not isinstance(results, list):
        _append_error(errors, "invalid_response", f"OpenAlex {kind} 名称解析响应格式错误")
        return []
    def name_matches(item: dict) -> bool:
        display = item.get("display_name")
        if kind == "authors":
            return author_matches({"_author_names": [display]}, name)
        return normalize_name(display) == wanted
    ids = [str(item.get("id") or "").rsplit("/", 1)[-1]
           for item in results if isinstance(item, dict) and item.get("id")
           and name_matches(item)]
    ids = list(dict.fromkeys(ids))
    warning = ""
    warning_kind = "truncated"
    count = (payload.get("meta") or {}).get("count") if isinstance(payload.get("meta"), dict) else None
    if isinstance(count, int) and count > len(results):
        warning = f"OpenAlex {kind} 名称候选超过首批 50 条，解析范围有限"
        _append_error(errors, warning_kind, warning)
    if len(ids) > 10:
        warning = f"OpenAlex {kind} 同名实体超过 10 个，仅使用前 10 个候选"
        _append_error(errors, warning_kind, warning)
        ids = ids[:10]
    if not ids:
        label = "作者" if kind == "authors" else "期刊"
        warning = f"OpenAlex 未找到名称匹配的{label}实体"
        warning_kind = "no_match"
        _append_error(errors, warning_kind, warning)
    if _oa_cache is not None:
        _oa_cache.set(ckey, {"ids": ids, "warning": warning, "kind": warning_kind},
                      expire=_CACHE_TTL)
    return ids


def _fetch_openalex_filtered(query: str, max_results: int, filters: SearchFilters,
                             *, errors: list | None, max_pages: int | None,
                             request_timeout: float | None) -> list[dict]:
    if max_results <= 0:
        return []
    pages, timeout = search_limits(max_pages, request_timeout)
    filter_parts: list[str] = []
    if filters.year_from is not None and filters.year_to is not None:
        filter_parts.append(f"publication_year:{filters.year_from}-{filters.year_to}")
    elif filters.year_from is not None:
        filter_parts.append(f"publication_year:>{filters.year_from - 1}")
    elif filters.year_to is not None:
        filter_parts.append(f"publication_year:<{filters.year_to + 1}")
    if filters.author:
        ids = _resolve_openalex_entities("authors", filters.author,
                                        timeout=timeout, errors=errors)
        if not ids:
            return []
        filter_parts.append("authorships.author.id:" + "|".join(ids))
    if filters.journal:
        ids = _resolve_openalex_entities("sources", filters.journal,
                                        timeout=timeout, errors=errors)
        if not ids:
            return []
        filter_parts.append("primary_location.source.id:" + "|".join(ids))

    url = "https://api.openalex.org/works"
    headers = {"User-Agent": "PaperPilot/1.0 (mailto:paperpilot@example.com)"}
    api_key = _get_api_key()
    cursor = "*"
    seen_cursors: set[str] = set()
    papers: list[dict] = []
    seen_ids: set[str] = set()
    per_page = min(100, max(25, max_results))
    for _ in range(pages):
        checkpoint()
        if cursor in seen_cursors:
            _append_error(errors, "incomplete", "OpenAlex 返回重复游标，分页已停止")
            break
        seen_cursors.add(cursor)
        params = {"search": query, "per_page": per_page, "cursor": cursor,
                  "mailto": "paperpilot@example.com"}
        if filter_parts:
            params["filter"] = ",".join(filter_parts)
        if api_key:
            params["api_key"] = api_key
        ckey = f"filtered:{query}|{filters.cache_key}|{cursor}|{per_page}"
        cached = _oa_cache.get(ckey) if _oa_cache is not None else None
        if cached is not None:
            count("page_cache_hit", source="openalex")
            data = cached
            fetched = False
            if not isinstance(data, dict):
                _append_error(errors, "invalid_response",
                              "OpenAlex 缓存响应格式错误，分页已停止")
                break
        else:
            try:
                checkpoint()
                response = source_get(url, params=params, headers=headers, timeout=timeout)
                if response.status_code in (403, 429):
                    if not papers:
                        raise SourceRateLimited("openalex", response.status_code)
                    _append_error(errors, "rate_limited",
                                  f"OpenAlex 分页受限，已保留 {len(papers)} 篇合规结果")
                    break
                response.raise_for_status()
            except SourceRateLimited:
                raise
            except requests.RequestException:
                _append_error(errors, "network",
                              f"OpenAlex 分页失败，已保留 {len(papers)} 篇合规结果")
                break
            try:
                data = response.json()
            except (ValueError, TypeError):
                _append_error(errors, "invalid_response",
                              f"OpenAlex 响应无法解析，已保留 {len(papers)} 篇合规结果")
                break
            if not isinstance(data, dict):
                _append_error(errors, "invalid_response",
                              f"OpenAlex 响应格式错误，已保留 {len(papers)} 篇合规结果")
                break
            fetched = True
        results = data.get("results") or []
        if not isinstance(results, list):
            _append_error(errors, "invalid_response", "OpenAlex 结果列表格式错误")
            break
        if not results:
            break
        meta = data.get("meta")
        if meta is not None and not isinstance(meta, dict):
            _append_error(errors, "invalid_response", "OpenAlex 分页元数据格式错误")
            break
        if fetched and _oa_cache is not None:
            _oa_cache.set(ckey, data, expire=_CACHE_TTL)
        for item in results:
            if not isinstance(item, dict):
                _append_error(errors, "invalid_response", "OpenAlex 跳过了格式错误的结果")
                continue
            if fetched:
                _cache_work_refs(item)
            paper = _parse_openalex_work(item)
            if paper and filters.matches(paper):
                identity = str(paper.get("doi") or paper.get("openalex_id") or
                               (paper.get("title"), paper.get("year"))).casefold()
                if identity in seen_ids:
                    continue
                seen_ids.add(identity)
                rel = item.get("relevance_score")
                try:
                    score = float(rel) if rel is not None else None
                except (TypeError, ValueError, OverflowError):
                    score = None
                    _append_error(errors, "invalid_response", "OpenAlex 跳过了无效相关度分数")
                if score is not None and not math.isfinite(score):
                    score = None
                    _append_error(errors, "invalid_response", "OpenAlex 跳过了无效相关度分数")
                paper["api_score"] = score if score is not None else max(
                    0.0, 1.0 - len(papers) / max(max_results, 1))
                papers.append(paper)
                if len(papers) >= max_results:
                    break
        if len(papers) >= max_results:
            break
        next_cursor = str((meta or {}).get("next_cursor") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        if fetched:
            interruptible_wait(0.1)
    if len(papers) < max_results:
        _append_error(errors, "incomplete",
                      f"OpenAlex 筛选后得到 {len(papers)}/{max_results} 篇，已耗尽结果或达到分页上限")
    papers = _normalize_api_scores(papers[:max_results])
    return _fetch_missing_abstracts(papers, errors=errors)


def _normalize_api_scores(papers: list[dict]) -> list[dict]:
    api_scores = [p.get("api_score") for p in papers if p.get("api_score") is not None]
    if api_scores:
        min_s, max_s = min(api_scores), max(api_scores)
        if max_s > 1.0 or min_s < 0.0:
            if max_s > min_s:
                for paper in papers:
                    if paper.get("api_score") is not None:
                        paper["api_score"] = (paper["api_score"] - min_s) / (max_s - min_s)
            else:
                for paper in papers:
                    if paper.get("api_score") is not None:
                        paper["api_score"] = 0.5
    total = max(len(papers), 1)
    for index, paper in enumerate(papers):
        if paper.get("api_score") is None:
            paper["api_score"] = 1.0 - index / total
    return papers


def _fetch_openalex_raw(query: str, max_results: int = 30,
                        year_min: str = "", year_max: str = "", *,
                        filters: SearchFilters | None = None,
                        errors: list | None = None,
                        max_pages: int | None = None,
                        request_timeout: float | None = None) -> list[dict]:
    """Fetch papers from OpenAlex with a raw query string (internal helper)."""
    effective_filters = coerce_filters(filters, year_min, year_max)
    if effective_filters is not None:
        return _fetch_openalex_filtered(
            query, max_results, effective_filters, errors=errors,
            max_pages=max_pages, request_timeout=request_timeout)
    if max_results <= 0:
        return []
    _, timeout = search_limits(max_pages, request_timeout)
    url = "https://api.openalex.org/works"
    papers = []
    per_page = min(100, max_results)
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
            if results is not None:
                count("page_cache_hit", source="openalex")
        if results is None:
            try:
                checkpoint()
                resp = source_get(url, params=params, headers=headers, timeout=timeout)
                # 429 退避：2s/5s/10s，优先遵循 Retry-After 头；耗尽且一无所获 → 抛限流异常
                for wait in (2, 5, 10):
                    if resp.status_code != 429:
                        break
                    retry_after = resp.headers.get("Retry-After", "")
                    try:
                        interruptible_wait(min(float(retry_after), 30) if retry_after else wait)
                    except ValueError:
                        interruptible_wait(wait)
                    checkpoint()
                    resp = source_get(url, params=params, headers=headers, timeout=timeout)
                if resp.status_code in (403, 429) and not papers:
                    hint = "OpenAlex 请求受限，请检查 API Key 与账户配额，或稍后重试"
                    raise SourceRateLimited("openalex", resp.status_code, hint)
                if resp.status_code in (403, 429):
                    _append_error(errors, "rate_limited", "OpenAlex 分页受限，已保留检索结果")
                    break
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ValueError("Invalid results")
                results = data.get("results", [])
                fetched = True
                if _oa_cache is not None:
                    _oa_cache.set(ckey, results, expire=_CACHE_TTL)
            except requests.RequestException:
                _append_error(errors, "network", "OpenAlex 分页网络失败，已保留检索结果")
                break
            except (ValueError, TypeError):
                _append_error(errors, "invalid_response", "OpenAlex 分页响应错误，已保留检索结果")
                break
        if not isinstance(results, list):
            _append_error(errors, "invalid_response", "OpenAlex 分页缓存错误")
            break
        if not results:
            break
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
                    # Preserve the old 50-item fallback score groups when the
                    # API omits relevance_score, despite 100-item HTTP pages.
                    paper["api_score"] = 1.0 - ((i % 50) / max(min(50, page_total - (i // 50) * 50), 1))
                papers.append(paper)
        if len(papers) >= max_results:
            break
        if fetched:
            interruptible_wait(0.1)  # 网络请求后的礼貌速率
    papers = _normalize_api_scores(papers[:max_results])
    papers = _fetch_missing_abstracts(papers, errors=errors)
    return papers


def _fetch_missing_abstracts(papers: list[dict], errors: list | None = None) -> list[dict]:
    if _defer_abstracts.get():
        return papers
    return enrich_abstracts(papers, errors=errors)


def enrich_abstracts(papers: list[dict], errors: list | None = None) -> list[dict]:
    """Batch unique IDs (100/request); cache confirmed absence, never failures.

    Only an explicit null abstract field confirms absence. Missing IDs, omitted
    fields, malformed JSON and transport errors remain eligible for a retry.
    Existing positive ``abs:https://openalex.org/W...`` cache entries stay valid.
    """
    groups: dict[str, list[dict]] = {}
    for paper in papers:
        checkpoint()
        work_id = _norm_oa_id(paper.get("openalex_id"))
        if re.fullmatch(r"W\d+", work_id):
            groups.setdefault(work_id, []).append(paper)
    missing = []
    filled = 0
    for work_id, rows in groups.items():
        existing = next((p.get("abstract") for p in rows
                         if (p.get("abstract") or "").strip()), "")
        ckey = f"abs:https://openalex.org/{work_id}"
        cached = _oa_cache.get(ckey) if _oa_cache is not None else None
        text = existing or (cached if isinstance(cached, str) else "")
        if text:
            for paper in rows:
                if not (paper.get("abstract") or "").strip():
                    paper["abstract"] = text
                    filled += 1
            if not existing:
                count("abstract_cache_hit", source="openalex")
        elif isinstance(cached, dict) and cached.get("absent") is True:
            count("abstract_absent_hit", source="openalex")
        else:
            missing.append(work_id)
    if errors and any(source == "openalex" and kind == "rate_limited"
                      for source, kind, _ in errors):
        return papers  # Cached enrichment is safe; do not hit a limited host again.
    api_key = _get_api_key() if missing else ""
    # A separate, bounded lifetime allows newly indexed abstracts to appear.
    try:
        absent_ttl = max(0, min(86400, float((load_config().get("search") or {}).get(
            "openalex_absent_ttl_seconds", 3600))))
    except (TypeError, ValueError):
        absent_ttl = 3600
    for offset in range(0, len(missing), 100):
        checkpoint()
        batch = missing[offset:offset + 100]
        params = {"filter": "openalex:" + "|".join(batch), "per_page": 100,
                  "select": "id,doi,abstract_inverted_index,referenced_works,keywords"}
        if api_key:
            params["api_key"] = api_key
        try:
            count("abstract_batch", source="openalex")
            response = source_get("https://api.openalex.org/works", params=params,
                                  headers={"User-Agent": "PaperPilot/1.0"}, timeout=15)
            if response.status_code in (403, 429):
                _append_error(errors, "rate_limited", "OpenAlex 摘要补查受限，已保留检索结果")
                break
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("Invalid work list")
        except requests.RequestException:
            _append_error(errors, "network", "OpenAlex 摘要补查网络失败，已保留检索结果")
            continue
        except (ValueError, TypeError):
            _append_error(errors, "invalid_response", "OpenAlex 摘要补查响应错误，已保留检索结果")
            continue
        for work in payload["results"]:
            checkpoint()
            if not isinstance(work, dict):
                continue
            work_id = _norm_oa_id(work.get("id"))
            if work_id not in batch or "abstract_inverted_index" not in work:
                continue
            inv = work["abstract_inverted_index"]
            _cache_work_refs(work)
            ckey = f"abs:https://openalex.org/{work_id}"
            if inv is None:
                if _oa_cache is not None and absent_ttl > 0:
                    _oa_cache.set(ckey, {"absent": True}, expire=absent_ttl)
                count("abstract_confirmed_absent", source="openalex")
                continue
            try:
                text = _decode_inverted_index(inv)
                if not text.strip():
                    raise ValueError("Empty abstract")
            except (ValueError, TypeError, IndexError, KeyError):
                _append_error(errors, "invalid_response", "OpenAlex 摘要数据错误，已保留检索结果")
                continue
            checkpoint()
            for paper in groups[work_id]:
                if not (paper.get("abstract") or "").strip():
                    paper["abstract"] = text
                    filled += 1
            if _oa_cache is not None:
                _oa_cache.set(ckey, text, expire=_CACHE_TTL)
        checkpoint()
    count("abstract_filled", filled, source="openalex")
    return papers


def fetch_openalex(keywords: list[str], max_results: int = 30,
                   logic: str = "OR",
                   year_min: str = "", year_max: str = "", *,
                   filters: SearchFilters | None = None,
                   errors: list | None = None,
                   max_pages: int | None = None,
                   request_timeout: float | None = None) -> list[dict]:
    """通过 OpenAlex API 检索论文，使用已配置的 API Key（若有）。"""
    if not keywords:
        return []
    query = _build_search_query(keywords, logic=logic)
    return _fetch_openalex_raw(
        query, max_results, year_min=year_min, year_max=year_max,
        filters=filters, errors=errors, max_pages=max_pages,
        request_timeout=request_timeout)


class OpenAlexSource(PaperSource):
    name = "openalex"
    label = "OpenAlex"
    description = "正式发表论文聚合索引，含引用数与期刊信息，免 Key"
    default_enabled = True
    raw_fetcher = staticmethod(_fetch_openalex_raw)

    def fetch_raw(self, query: str, max_results: int = 30,
                  year_min: str = "", year_max: str = "", **kwargs) -> list[dict]:
        return _fetch_openalex_raw(query, max_results, year_min=year_min,
                                   year_max=year_max, **kwargs)


def register() -> OpenAlexSource:
    src = OpenAlexSource()
    from paperpilot.sources.base import register_source
    register_source(src)
    return src


register()
