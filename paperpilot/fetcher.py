"""论文数据获取接口。

所有函数返回统一的 paper dict 格式：

    {
        "title": str,
        "authors": str,         # 逗号分隔
        "abstract": str,
        "year": int | None,
        "source": str,          # "arxiv" | "openalex" | "local_pdf"
        "url": str | None,
        "doi": str | None,
        "api_score": float,     # 0.0-1.0, API 原始排序位置归一化
        "type": str | None,     # 文章类型 (OpenAlex: review/article/...; arXiv: None)
        "cited_by_count": int | None,  # 引用次数 (仅 OpenAlex)
        "journal": str | None,  # 期刊/会议名 (OpenAlex source; arXiv journal_ref)
    }
"""

import os
import re
import socket
import time
from difflib import SequenceMatcher
from pathlib import Path

import arxiv
import fitz
import requests

from paperpilot.config import config

# ── OpenAlex 响应缓存（diskCache，TTL 24h）──
# 搜索分页结果与摘要补齐都可缓存，重复检索几乎瞬时返回。
_OA_CACHE_DIR = Path(config.get("cache", {}).get("dir", "./cache/api")) / "openalex"
_CACHE_TTL = int(config.get("cache", {}).get("ttl_hours", 24)) * 3600

try:
    from diskcache import Cache as _Cache
    _oa_cache = _Cache(str(_OA_CACHE_DIR))
except Exception:
    _oa_cache = None  # diskcache 不可用时降级为不缓存


def _build_search_query(keywords: list[str], logic: str = "OR") -> str:
    """Build quoted-phrase query for search APIs (arXiv, OpenAlex).

    Args:
        keywords: list of keyword phrases
        logic: "OR" (default, broad recall) or "AND" (strict, all must match)
    """
    joiner = " AND " if logic == "AND" else " OR "
    return joiner.join(f'"{kw}"' for kw in keywords)


def _build_mixed_query(and_kw: list[str], or_kw: list[str]) -> str:
    """Build mixed boolean query: must-match terms (AND) + should-match terms (OR).

    Examples:
        and_kw=["core1","core2"], or_kw=["reg1","reg2"]
        → "core1" AND "core2" AND ("reg1" OR "reg2")

        and_kw=[], or_kw=["kw1","kw2"]
        → "kw1" OR "kw2"

        and_kw=["primary"], or_kw=[]
        → "primary"
    """
    if and_kw and or_kw:
        and_part = " AND ".join(f'"{kw}"' for kw in and_kw)
        or_part = " OR ".join(f'"{kw}"' for kw in or_kw)
        return f'{and_part} AND ({or_part})'
    elif and_kw:
        return " AND ".join(f'"{kw}"' for kw in and_kw)
    elif or_kw:
        return " OR ".join(f'"{kw}"' for kw in or_kw)
    return ""


def _parse_arxiv_result(r) -> dict:
    authors = ", ".join(a.name for a in r.authors[:10])
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
    }


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


def _fetch_arxiv_raw(query: str, max_results: int = 30,
                     year_min: str = "", year_max: str = "") -> list[dict]:
    """Fetch papers from arXiv with a raw query string (internal helper).

    Uses ThreadPoolExecutor + timeout to guard against the arxiv library's
    underlying requests.Session which has no default timeout and can hang.
    """
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

    total = max(len(papers), 1)
    for i, p in enumerate(papers):
        p["api_score"] = 1.0 - (i / total)
    return papers


def fetch_arxiv(keywords: list[str], max_results: int = 30,
                logic: str = "OR",
                year_min: str = "", year_max: str = "") -> list[dict]:
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
    return _fetch_arxiv_raw(query, max_results, year_min=year_min, year_max=year_max)


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


# ── 文章类型标签映射 ──

_TYPE_LABELS: dict[str, str] = {
    "review": "综述",
    "article": "研究论文",
    "book-chapter": "书籍章节",
    "book": "书籍",
    "dissertation": "学位论文",
    "other": "其他",
}

_REVIEW_KEYWORDS = [
    "survey", "review", "meta-analysis", "meta analysis",
    "systematic review", "literature review", "state of the art",
    "state-of-the-art", "综述", "述评", "回顾", "进展",
]


def get_article_type_label(paper: dict) -> str:
    """返回论文类型的中文标签。

    OpenAlex: 使用 API 返回的 type 字段（权威来源）。
    arXiv/本地PDF: 标题关键词推断，默认 "研究论文"。
    """
    paper_type = paper.get("type")
    if paper_type and paper_type in _TYPE_LABELS:
        return _TYPE_LABELS[paper_type]

    title = (paper.get("title") or "").lower()
    for kw in _REVIEW_KEYWORDS:
        if kw in title:
            return "综述"

    return "研究论文"


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
                    for retry in range(3):
                        if resp.status_code != 429:
                            break
                        time.sleep(1 * (retry + 1))
                        resp = requests.get(url, params=params, headers=headers, timeout=15)
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


def fetch_with_cascade(
    primary_kw: list[str],
    secondary_kw: list[str],
    regular_kw: list[str],
    source: str = "arxiv",
    max_results: int = 30,
    min_results: int = 3,
    year_min: str = "",
    year_max: str = "",
) -> tuple[list[dict], int]:
    """三级级联检索：核心AND → 主关键词AND → 全部OR。"""
    fetch_raw = _fetch_arxiv_raw if source == "arxiv" else _fetch_openalex_raw
    all_kw = primary_kw + secondary_kw + regular_kw
    all_core = primary_kw + secondary_kw

    strategies = []

    # Strategy 0: all core AND + regular OR
    if all_core:
        q0 = _build_mixed_query(and_kw=all_core, or_kw=regular_kw)
        strategies.append((0, q0))

    # Strategy 1: all primary AND + all others OR (only if primary is set)
    if primary_kw:
        others = secondary_kw + regular_kw
        q1 = _build_mixed_query(and_kw=primary_kw, or_kw=others)
        strategies.append((1, q1))

    # Strategy 2: all OR (safety net)
    q2 = _build_mixed_query(and_kw=[], or_kw=all_kw)
    strategies.append((2, q2))

    for level, query in strategies:
        if not query:
            continue
        papers = fetch_raw(query, max_results, year_min=year_min, year_max=year_max)
        if len(papers) >= min_results or level == strategies[-1][0]:
            return papers, level

    return [], -1


def fetch_multi_primary(
    primary_kw: list[str],
    secondary_kw: list[str],
    regular_kw: list[str],
    source: str = "arxiv",
    max_results: int = 30,
    min_results: int = 3,
    year_min: str = "",
    year_max: str = "",
) -> list[dict]:
    """多主关键词独立检索 + 合并加权。

    每个主关键词独立进行一次级联检索，结果合并去重。
    命中多个主关键词的论文获得 api_score 加权，自然排前。

    Args:
        primary_kw: 用户标记的主关键词列表
        secondary_kw: 副关键词
        regular_kw: 普通关键词
        source: "arxiv" 或 "openalex"
        max_results: 最终返回的最大论文数
        min_results: 每路检索触发降级的结果数阈值
        year_min: 起始年份筛选（仅 OpenAlex 生效）
        year_max: 结束年份筛选（仅 OpenAlex 生效）

    Returns:
        papers 列表，含 api_score（多路命中已加权）
    """
    if not primary_kw:
        papers, _ = fetch_with_cascade(
            primary_kw=[], secondary_kw=secondary_kw,
            regular_kw=regular_kw, source=source,
            max_results=max_results, min_results=min_results,
            year_min=year_min, year_max=year_max)
        return papers

    seen: dict[str, tuple[dict, int]] = {}
    per_kw = max(max_results // len(primary_kw), min_results)

    # 每路主关键词独立搜索时，副关键词放入 OR 组而非 AND 组
    # 避免 Strategy 0 的主+副多路 AND 过于严格导致结果太少
    merged_regular = secondary_kw + regular_kw

    for pk in primary_kw:
        papers, level = fetch_with_cascade(
            primary_kw=[pk],
            secondary_kw=[],
            regular_kw=merged_regular,
            source=source,
            max_results=per_kw,
            min_results=min_results,
            year_min=year_min,
            year_max=year_max,
        )
        for p in papers:
            pid = (p.get("title", "") + "|" + p.get("source", "") + "|"
                   + str(p.get("year", ""))).lower()
            if pid in seen:
                prev, hits = seen[pid]
                if p.get("api_score", 0) > prev.get("api_score", 0):
                    seen[pid] = (p, hits + 1)
                else:
                    seen[pid] = (prev, hits + 1)
            else:
                seen[pid] = (p, 1)

    max_hits = max(h[1] for h in seen.values()) if seen else 1
    result = []
    for pid, (paper, hits) in seen.items():
        if max_hits > 1:
            boost = 1.0 + (hits / max_hits) * 0.3
            paper["api_score"] = min(paper.get("api_score", 0.5) * boost, 1.0)
        result.append(paper)

    result.sort(key=lambda p: p.get("api_score", 0), reverse=True)
    return result[:max_results]


def _extract_text_from_page(page, max_chars: int = 3000) -> str:
    blocks = page.get_text("blocks")
    blocks = sorted(blocks, key=lambda b: (b[1], b[0]))
    return " ".join(b[4] for b in blocks if b[6] == 0)[:max_chars]


def _guess_title(text: str) -> str:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines[:5]:
        if len(line) > 10:
            return line[:500]
    return lines[0][:500] if lines else ""


def _guess_abstract(text: str) -> str:
    pattern = r"(?i)abstract[\s\-\—:]*\n?"
    match = re.search(pattern, text)
    if match:
        start = match.end()
        rest = text[start:].strip()
        return rest[:2000]
    return text[:2000]


def import_local_pdfs(folder_path: str) -> list[dict]:
    """导入本地 PDF 文件夹，用 PyMuPDF 提取标题和摘要。

    Args:
        folder_path: PDF 文件夹路径

    Returns:
        paper dict 列表，source="local_pdf"
    """
    papers = []
    for filename in os.listdir(folder_path):
        if not filename.lower().endswith(".pdf"):
            continue
        filepath = os.path.join(folder_path, filename)
        try:
            doc = fitz.open(filepath)
            text = ""
            for p in doc:
                text += p.get_text()
                if len(text) > 5000:
                    break
            doc.close()
            title = _guess_title(text)
            abstract = _guess_abstract(text)
            papers.append({
                "title": title,
                "authors": "",
                "abstract": abstract,
                "year": None,
                "source": "local_pdf",
                "url": None,
                "doi": None,
                "type": None,
                "cited_by_count": None,
                "journal": None,
                "openalex_id": None,
            })
        except Exception:
            continue
    return papers


def _normalize_title(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower().strip())


def deduplicate(papers: list[dict]) -> list[dict]:
    """去重：按 title 相似度合并重复论文。"""
    seen = []
    for paper in papers:
        t1 = _normalize_title(paper["title"])
        dup = False
        for existing in seen:
            t2 = _normalize_title(existing["title"])
            if t1 == t2 or SequenceMatcher(None, t1, t2).ratio() >= 0.9:
                dup = True
                break
        if not dup:
            seen.append(paper)
    return seen
