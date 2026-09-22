"""Exact paper lookup by DOI, arXiv identifier, or complete title."""

from __future__ import annotations

from html import unescape
import re
import unicodedata
from urllib.parse import quote, unquote, urlparse, urlunparse

import arxiv
import requests

from paperpilot.search_filters import search_limits
from paperpilot.sources.arxiv_source import _parse_arxiv_result, _wait_arxiv_rate_limit
from paperpilot.sources.europepmc_source import _EPMC_BASE, _parse_europepmc_result
from paperpilot.sources.openalex_source import _get_api_key, _parse_openalex_work


_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s]+", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(r"[a-z][a-z0-9.\-]*(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?", re.IGNORECASE)
_HTML_TAG_RE = re.compile(
    r"</?(?:i|b|em|strong|sub|sup|span|p|br|italic|bold)(?:\s+[^<>]{0,200})?/?>",
    re.IGNORECASE,
)


def normalize_doi(value: object) -> str:
    text = unquote(str(value or "").strip())
    text = re.sub(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", "", text,
                  flags=re.IGNORECASE)
    if not _DOI_RE.fullmatch(text):
        raise ValueError("DOI 格式无效；请输入完整 DOI，不要附加说明文字")
    return text.casefold()


def normalize_arxiv_id(value: object) -> str:
    text = unquote(str(value or "").strip())
    text = re.sub(r"^arxiv\s*:\s*", "", text, flags=re.IGNORECASE)
    parsed = urlparse(text)
    if re.match(r"^https?://", text, re.IGNORECASE):
        if parsed.scheme not in ("http", "https") or parsed.netloc.casefold() not in (
                "arxiv.org", "www.arxiv.org"):
            raise ValueError("仅支持 arxiv.org 的 abs/pdf 链接")
        match = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?/?", parsed.path,
                             flags=re.IGNORECASE)
        if not match:
            raise ValueError("arXiv 链接格式无效")
        text = match.group(1)
    if not (_ARXIV_NEW_RE.fullmatch(text) or _ARXIV_OLD_RE.fullmatch(text)):
        raise ValueError("arXiv ID 格式无效")
    version = re.search(r"v(\d+)$", text, re.IGNORECASE)
    if version and int(version.group(1)) < 1:
        raise ValueError("arXiv 版本号必须从 v1 开始")
    modern = _ARXIV_NEW_RE.fullmatch(text)
    if modern:
        month = int(text[2:4])
        if month < 1 or month > 12:
            raise ValueError("arXiv ID 中的月份必须在 01 到 12 之间")
    return text


def normalize_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", unescape(str(value or "")))
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return re.sub(r"[.。]+$", "", text).strip()


def detect_exact_type(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("请输入 DOI、arXiv ID 或完整标题")
    if re.match(r"^isbn\s*:", text, re.IGNORECASE) or re.fullmatch(
            r"(?:97[89][ -]?)?\d(?:[\dXx][ -]?){8,12}", text):
        raise ValueError("当前精确查找不支持 ISBN，仅支持论文 DOI、arXiv ID 和完整标题")
    try:
        doi = normalize_doi(text)
        arxiv_doi = re.fullmatch(r"10\.48550/arxiv\.(.+)", doi, re.IGNORECASE)
        if arxiv_doi:
            return "arxiv", normalize_arxiv_id(arxiv_doi.group(1))
        return "doi", doi
    except ValueError:
        pass
    if re.match(r"^10\.", text, re.IGNORECASE):
        raise ValueError("DOI 格式无效；请输入完整 DOI")
    try:
        return "arxiv", normalize_arxiv_id(text)
    except ValueError:
        pass
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        raise ValueError("不支持任意网页链接；请输入 DOI、arXiv 链接/ID 或完整标题")
    if len(text) > 1000:
        raise ValueError("完整标题不能超过 1000 个字符")
    if not normalize_title(text):
        raise ValueError("完整标题不能为空")
    return "title", text


def _append_error(errors: list, source: str, kind: str, message: str) -> None:
    if not any(s == source and k == kind and m == message for s, k, m in errors):
        errors.append((source, kind, message))


def _arxiv_id_from_paper(paper: dict) -> str:
    value = str(paper.get("url") or "")
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?$", value,
                      flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _normalized_arxiv_identity(value: object) -> str:
    """Return a conservative arXiv identity, preserving an explicit version."""
    try:
        return normalize_arxiv_id(value).casefold()
    except ValueError:
        return ""


def _normalized_openalex_identity(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"(?:https?://openalex\.org/)?(W\d+?)/?", text,
                         flags=re.IGNORECASE)
    return match.group(1).casefold() if match else ""


def _normalized_stable_url(value: object) -> str:
    """Normalize a URL only after DOI/arXiv/OpenAlex identities were exhausted."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(), path,
                       "", parsed.query, ""))


def _fetch_arxiv_exact(kind: str, value: str, limit: int, pages: int, timeout: float,
                       errors: list) -> list[dict]:
    if limit <= 0:
        return []
    safe_value = value.replace("\\", " ").replace('"', " ")
    page_size = 1 if kind == "arxiv" else min(100, max(25, limit))
    candidate_cap = 1 if kind == "arxiv" else page_size * pages
    search = (arxiv.Search(id_list=[value], max_results=1)
              if kind == "arxiv" else
              arxiv.Search(query=f'ti:"{safe_value}"', max_results=candidate_cap,
                           sort_by=arxiv.SortCriterion.Relevance))
    client = arxiv.Client(page_size=page_size, num_retries=1, delay_seconds=3)
    original_get = client._session.get
    client._session.get = lambda *a, **kw: original_get(  # type: ignore[method-assign]
        *a, **({**kw, "timeout": timeout} if "timeout" not in kw else kw))
    papers: list[dict] = []
    seen: set[str] = set()
    scanned = 0
    try:
        _wait_arxiv_rate_limit()
        for result in client.results(search):
            scanned += 1
            paper = _parse_arxiv_result(result)
            identity = str(paper.get("url") or paper.get("title") or "").casefold()
            if identity in seen:
                continue
            seen.add(identity)
            if kind == "arxiv":
                found = _arxiv_id_from_paper(paper)
                requested_base = re.sub(r"v\d+$", "", value, flags=re.IGNORECASE).casefold()
                found_base = re.sub(r"v\d+$", "", found, flags=re.IGNORECASE).casefold()
                versioned = bool(re.search(r"v\d+$", value, flags=re.IGNORECASE))
                if found_base != requested_base or (versioned and found.casefold() != value.casefold()):
                    continue
            elif normalize_title(paper.get("title")) != normalize_title(value):
                continue
            paper["api_score"] = 1.0
            papers.append(paper)
            if len(papers) >= limit:
                break
    except Exception:
        _append_error(errors, "arxiv", "network", "arXiv 精确查找失败")
    finally:
        client._session.close()
    if kind == "title" and scanned >= candidate_cap and len(papers) < limit:
        _append_error(errors, "arxiv", "truncated",
                      f"arXiv 标题查找已达到 {pages} 页候选上限")
    return papers


def _openalex_params() -> tuple[dict, dict]:
    params = {"mailto": "paperpilot@example.com"}
    key = _get_api_key()
    if key:
        params["api_key"] = key
    return params, {"User-Agent": "PaperPilot/1.0 (mailto:paperpilot@example.com)"}


def _fetch_openalex_exact(kind: str, value: str, limit: int, pages: int,
                          timeout: float, errors: list) -> list[dict]:
    if limit <= 0:
        return []
    params, headers = _openalex_params()
    papers: list[dict] = []
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    exhausted = False
    try:
        if kind == "doi":
            encoded = quote("https://doi.org/" + value, safe="")
            response = requests.get(f"https://api.openalex.org/works/{encoded}",
                                    params=params, headers=headers, timeout=timeout)
            if response.status_code == 404:
                return []
            if response.status_code in (403, 429):
                _append_error(errors, "openalex", "rate_limited",
                              f"OpenAlex 精确查找受限（HTTP {response.status_code}）")
                return []
            response.raise_for_status()
            work = response.json()
            if not isinstance(work, dict):
                _append_error(errors, "openalex", "invalid_response",
                              "OpenAlex 精确查找响应格式错误")
                return []
            paper = _parse_openalex_work(work)
            found_doi = ""
            if paper and paper.get("doi"):
                try:
                    found_doi = normalize_doi(paper["doi"])
                except ValueError:
                    pass
            if paper and _is_paper_record(paper) and found_doi == value:
                if not paper.get("url") and paper.get("openalex_id"):
                    paper["url"] = paper["openalex_id"]
                paper["api_score"] = 1.0
                papers.append(paper)
            return papers
        cursor = "*"
        for _ in range(pages):
            if cursor in seen_cursors:
                _append_error(errors, "openalex", "invalid_response", "OpenAlex 返回重复游标")
                break
            seen_cursors.add(cursor)
            query_params = {**params, "search": value,
                            "per_page": min(100, max(25, limit)),
                            "cursor": cursor}
            response = requests.get("https://api.openalex.org/works", params=query_params,
                                    headers=headers, timeout=timeout)
            if response.status_code in (403, 429):
                _append_error(errors, "openalex", "rate_limited",
                              f"OpenAlex 精确查找受限（HTTP {response.status_code}）")
                break
            response.raise_for_status()
            data = response.json()
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                _append_error(errors, "openalex", "invalid_response", "OpenAlex 精确查找响应格式错误")
                break
            if not results:
                exhausted = True
                break
            for work in results:
                paper = _parse_openalex_work(work)
                if paper and _is_paper_record(paper) and normalize_title(paper.get("title")) == normalize_title(value):
                    if not paper.get("url") and paper.get("openalex_id"):
                        paper["url"] = paper["openalex_id"]
                    identity = _identity(paper)
                    if identity in seen_ids:
                        continue
                    seen_ids.add(identity)
                    paper["api_score"] = 1.0
                    papers.append(paper)
                    if len(papers) >= limit:
                        return papers
            meta = data.get("meta")
            if meta is not None and not isinstance(meta, dict):
                _append_error(errors, "openalex", "invalid_response",
                              "OpenAlex 精确查找分页元数据格式错误")
                break
            next_cursor = str((meta or {}).get("next_cursor") or "")
            if not next_cursor or next_cursor == cursor:
                exhausted = True
                break
            cursor = next_cursor
    except requests.RequestException:
        _append_error(errors, "openalex", "network", "OpenAlex 精确查找失败")
    except (ValueError, TypeError):
        _append_error(errors, "openalex", "invalid_response",
                      "OpenAlex 精确查找响应无法解析")
    if kind == "title" and not exhausted and len(papers) < limit:
        _append_error(errors, "openalex", "truncated",
                      f"OpenAlex 标题查找已达到 {pages} 页候选上限")
    return papers


def _fetch_epmc_exact(kind: str, value: str, limit: int, pages: int,
                      timeout: float, errors: list) -> list[dict]:
    if limit <= 0:
        return []
    safe = value.replace("\\", " ").replace('"', " ")
    query = f'DOI:"{safe}"' if kind == "doi" else f'TITLE:"{safe}"'
    cursor = "*"
    papers: list[dict] = []
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    exhausted = False
    try:
        for _ in range(pages):
            if cursor in seen_cursors:
                _append_error(errors, "europepmc", "invalid_response", "Europe PMC 返回重复游标")
                break
            seen_cursors.add(cursor)
            params = {"query": query, "format": "json", "resultType": "core",
                      "pageSize": min(1000, max(25, limit)), "cursorMark": cursor}
            response = requests.get(_EPMC_BASE, params=params,
                                    headers={"User-Agent": "PaperPilot/1.0"}, timeout=timeout)
            if response.status_code in (403, 429):
                _append_error(errors, "europepmc", "rate_limited",
                              f"Europe PMC 精确查找受限（HTTP {response.status_code}）")
                break
            response.raise_for_status()
            data = response.json()
            result_list = data.get("resultList") if isinstance(data, dict) else None
            results = result_list.get("result") if isinstance(result_list, dict) else None
            if not isinstance(results, list):
                _append_error(errors, "europepmc", "invalid_response", "Europe PMC 精确查找响应格式错误")
                break
            if not results:
                exhausted = True
                break
            for item in results:
                if not isinstance(item, dict):
                    _append_error(errors, "europepmc", "invalid_response",
                                  "Europe PMC 跳过了格式错误的结果")
                    continue
                if not _is_epmc_paper_item(item):
                    continue
                try:
                    paper = _parse_europepmc_result(item)
                except (AttributeError, TypeError, ValueError):
                    _append_error(errors, "europepmc", "invalid_response",
                                  "Europe PMC 跳过了格式错误的结果")
                    continue
                matched = False
                if paper and kind == "doi" and paper.get("doi"):
                    try:
                        matched = normalize_doi(paper["doi"]) == value
                    except ValueError:
                        pass
                elif paper and kind == "title":
                    matched = normalize_title(paper.get("title")) == normalize_title(value)
                if matched:
                    identity = _identity(paper)
                    if identity in seen_ids:
                        continue
                    seen_ids.add(identity)
                    paper["api_score"] = 1.0
                    papers.append(paper)
                    if len(papers) >= limit:
                        return papers
            next_cursor = str(data.get("nextCursorMark") or "")
            if not next_cursor or next_cursor == cursor:
                exhausted = True
                break
            cursor = next_cursor
    except requests.RequestException:
        _append_error(errors, "europepmc", "network", "Europe PMC 精确查找失败")
    except (ValueError, TypeError):
        _append_error(errors, "europepmc", "invalid_response",
                      "Europe PMC 精确查找响应无法解析")
    if kind == "title" and not exhausted and len(papers) < limit:
        _append_error(errors, "europepmc", "truncated",
                      f"Europe PMC 标题查找已达到 {pages} 页候选上限")
    return papers


def _identity(paper: dict) -> str:
    if paper.get("doi"):
        try:
            return "doi:" + normalize_doi(paper["doi"])
        except ValueError:
            pass
    arxiv_id = _arxiv_id_from_paper(paper)
    if arxiv_id:
        normalized_arxiv = _normalized_arxiv_identity(arxiv_id)
        if normalized_arxiv:
            return "arxiv:" + normalized_arxiv
    openalex_id = _normalized_openalex_identity(
        paper.get("openalex_id") or paper.get("url"))
    if openalex_id:
        return "openalex:" + openalex_id
    stable_url = _normalized_stable_url(paper.get("url"))
    if stable_url:
        return "url:" + stable_url
    return "meta:" + "|".join((normalize_title(paper.get("title")),
                                 str(paper.get("year") or "")))


def _is_paper_record(paper: dict) -> bool:
    return str(paper.get("type") or "").casefold() not in {
        "book", "book-chapter", "book-part", "book-section", "dataset",
        "reference-entry", "paratext", "monograph", "edited-book",
    }


def _is_epmc_paper_item(item: dict) -> bool:
    """Reject records explicitly classified as books or other non-paper works."""
    pub_types = item.get("pubTypeList") or {}
    raw_types = pub_types.get("pubType", []) if isinstance(pub_types, dict) else []
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    if not isinstance(raw_types, list):
        return True
    normalized = {str(value).strip().casefold() for value in raw_types}
    excluded = {"book", "book chapter", "book-chapter", "monograph",
                "dataset", "reference entry", "reference-entry"}
    return not bool(normalized & excluded)


def _deduplicate_exact(papers: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for paper in papers:
        identity = _identity(paper)
        if identity in merged:
            current = merged[identity]
            for key, value in paper.items():
                if value and not current.get(key):
                    current[key] = value
            continue
        paper["_exact_match"] = True
        paper["_exact_identity"] = identity
        merged[identity] = paper
    return list(merged.values())


def exact_search(value: str, query_type: str = "auto", *,
                 use_arxiv: bool = True, use_openalex: bool = True,
                 use_europepmc: bool = True, max_results: int | None = None,
                 max_pages: int | None = None,
                 request_timeout: float | None = None) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Return exact matches and source-level errors without ranking or translation."""
    from paperpilot.config import config
    search_cfg = config.get("search", {}) or {}
    raw_limit = search_cfg.get("exact_max_results", 25) if max_results is None else max_results
    if isinstance(raw_limit, bool):
        raise ValueError("精确查找数量上限必须是整数")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("精确查找数量上限必须是整数") from exc
    if limit < 0:
        raise ValueError("精确查找数量上限不能为负数")
    limit = min(100, limit)
    if query_type == "auto":
        kind, normalized = detect_exact_type(value)
    elif query_type == "doi":
        normalized = normalize_doi(value)
        arxiv_doi = re.fullmatch(r"10\.48550/arxiv\.(.+)", normalized, re.IGNORECASE)
        if arxiv_doi:
            kind, normalized = "arxiv", normalize_arxiv_id(arxiv_doi.group(1))
        else:
            kind = "doi"
    elif query_type == "arxiv":
        kind, normalized = "arxiv", normalize_arxiv_id(value)
    elif query_type == "title":
        kind, normalized = "title", str(value or "").strip()
        if len(normalized) > 1000:
            raise ValueError("完整标题不能超过 1000 个字符")
        if not normalize_title(normalized) or not any(c.isalnum() for c in normalized):
            raise ValueError("完整标题不能为空")
    else:
        raise ValueError("查找类型必须是 auto、doi、arxiv 或 title")

    # 0 是有效的显式上限：完成输入校验后立即返回，绝不发起网络请求。
    if limit == 0:
        return [], []
    pages, timeout = search_limits(max_pages, request_timeout)

    errors: list[tuple[str, str, str]] = []
    papers: list[dict] = []
    if kind == "arxiv":
        if not use_arxiv:
            _append_error(errors, "arxiv", "disabled", "arXiv 数据源已关闭，请先在设置中启用")
        else:
            papers.extend(_fetch_arxiv_exact(kind, normalized, 1, pages, timeout, errors))
    else:
        if use_openalex:
            papers.extend(_fetch_openalex_exact(kind, normalized, limit, pages, timeout, errors))
        if use_europepmc:
            papers.extend(_fetch_epmc_exact(kind, normalized, limit, pages, timeout, errors))
        if kind == "title" and use_arxiv:
            papers.extend(_fetch_arxiv_exact(kind, normalized, limit, pages, timeout, errors))
        if not use_openalex and not use_europepmc and not (kind == "title" and use_arxiv):
            _append_error(errors, "exact", "disabled",
                          ("DOI 查找需要启用 OpenAlex 或 Europe PMC 数据源"
                           if kind == "doi" else
                           "完整标题查找需要至少启用一个支持的数据源"))
    papers = _deduplicate_exact(papers)[:limit]
    if kind == "title" and len(papers) >= limit:
        _append_error(errors, "exact", "truncated",
                      f"完整标题查找最多显示 {limit} 条精确匹配，结果可能未穷尽")
    return papers, errors
