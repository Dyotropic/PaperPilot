"""论文数据获取接口。

各数据源的抓取实现已按 PHASE3_PLAN §2.2 迁入 paperpilot/sources/ 包
（base.py 抽象 + 注册表；arxiv_source / openalex_source / europepmc_source）。
本模块保留检索编排逻辑（级联策略 / 多主关键词 / 去重 / 类型标签 / 本地导入），
并重导出各源函数以保持既有导入路径兼容。

所有函数返回统一的 paper dict 格式：

    {
        "title": str,
        "authors": str,         # 逗号分隔
        "abstract": str,
        "year": int | None,
        "source": str,          # "arxiv" | "openalex" | "europepmc" | "local_pdf"
        "url": str | None,
        "doi": str | None,
        "api_score": float,     # 0.0-1.0, API 原始排序位置归一化
        "type": str | None,     # 文章类型 (OpenAlex: review/article/...; arXiv: None)
        "cited_by_count": int | None,  # 引用次数 (仅 OpenAlex/Europe PMC)
        "journal": str | None,  # 期刊/会议名 (OpenAlex source; arXiv journal_ref)
    }

新增数据源：实现 paperpilot.sources.base.PaperSource 并注册，
在 sources/__init__.py import 即可被 fetch_with_cascade / fetch_multi_primary 分发。
"""

import os
import re
from difflib import SequenceMatcher

import fitz

# ── 各源实现（迁自本文件，重导出保持兼容）──
from paperpilot.sources.arxiv_source import (  # noqa: F401
    ArxivSource,
    _fetch_arxiv_raw,
    _parse_arxiv_result,
    _wait_arxiv_rate_limit,
    fetch_arxiv,
)
from paperpilot.sources.openalex_source import (  # noqa: F401
    OpenAlexSource,
    _decode_inverted_index,
    _fetch_missing_abstracts,
    _fetch_openalex_raw,
    _oa_cache,
    _parse_openalex_work,
    fetch_openalex,
)
from paperpilot.sources.europepmc_source import (  # noqa: F401
    EuropePMCSource,
    _epmc_cache,
    _EPMC_BASE,
    _EPMC_SORT,
    _fetch_europepmc_raw,
    _parse_europepmc_result,
    fetch_europepmc,
)
from paperpilot.sources.base import (  # noqa: F401
    PaperSource,
    SourceRateLimited,
    _build_search_query,
    all_sources,
    get_source,
)

# source → raw 抓取函数映射（fetch_with_cascade 分发用）。
# 由数据源注册表自动构建，新源注册后无需改动本文件。
_FETCH_RAW: dict = {
    _src.name: _src.raw_fetcher
    for _src in all_sources()
    if _src.raw_fetcher is not None
}


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


def _record_source_error(errors: list | None, source: str, kind: str, msg: str) -> None:
    """记录源级错误（去重），供 UI 提示；errors=None 时静默（向后兼容）。"""
    if errors is None:
        return
    if not any(e[0] == source and e[1] == kind for e in errors):
        errors.append((source, kind, msg))


def fetch_with_cascade(
    primary_kw: list[str],
    secondary_kw: list[str],
    regular_kw: list[str],
    source: str = "arxiv",
    max_results: int = 30,
    min_results: int = 3,
    year_min: str = "",
    year_max: str = "",
    errors: list | None = None,
) -> tuple[list[dict], int]:
    """三级级联检索：核心AND → 主关键词AND → 全部OR。

    Args:
        errors: 可选错误收集列表（如传 OpenAlex 429）：
                [(source, "rate_limited"|"error", message), ...]
    """
    # source → 抓取函数映射（arxiv/openalex/europepmc）
    fetch_raw = _FETCH_RAW.get(source, _fetch_arxiv_raw)
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
        try:
            papers = fetch_raw(query, max_results, year_min=year_min, year_max=year_max)
        except SourceRateLimited as e:
            # 同源后续策略必然同样限流，记录后直接终止该源降级（保持"单源失败不拖垮整体"）
            _record_source_error(errors, e.source, "rate_limited", e.message)
            break
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
    errors: list | None = None,
) -> list[dict]:
    """多主关键词独立检索 + 合并加权。

    每个主关键词独立进行一次级联检索，结果合并去重。
    命中多个主关键词的论文获得 api_score 加权，自然排前。

    Args:
        primary_kw: 用户标记的主关键词列表
        secondary_kw: 副关键词
        regular_kw: 普通关键词
        source: "arxiv" / "openalex" / "europepmc"
        max_results: 最终返回的最大论文数
        min_results: 每路检索触发降级的结果数阈值
        year_min: 起始年份筛选（仅 OpenAlex 生效）
        year_max: 结束年份筛选（仅 OpenAlex 生效）
        errors: 可选错误收集列表（限流等），见 fetch_with_cascade

    Returns:
        papers 列表，含 api_score（多路命中已加权）
    """
    if not primary_kw:
        papers, _ = fetch_with_cascade(
            primary_kw=[], secondary_kw=secondary_kw,
            regular_kw=regular_kw, source=source,
            max_results=max_results, min_results=min_results,
            year_min=year_min, year_max=year_max, errors=errors)
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
            errors=errors,
        )
        # 已被限流：后续主关键词路必然同样失败，短路避免重复退避等待
        if errors and any(e[0] == source and e[1] == "rate_limited" for e in errors):
            break
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
    """去重：按 title 相似度合并重复论文（保留首次出现，阈值 0.9 不变）。

    归一化每篇只做一次；相同标题走集合 O(1) 判重；
    SequenceMatcher 前按数学界预筛：ratio = 2·min/(l1+l2) ≥ 0.9
    要求 hi ≤ (11/9)·lo，违反者不可能达阈值，直接跳过。
    """
    seen: list[dict] = []
    seen_norms: list[str] = []
    seen_set: set[str] = set()
    for paper in papers:
        t1 = _normalize_title(paper["title"])
        if t1 in seen_set:
            continue
        lo = len(t1)
        dup = False
        for t2 in seen_norms:
            hi = len(t2)
            a, b = (lo, hi) if lo <= hi else (hi, lo)
            if b * 9 > a * 11:  # 长度比超过 11:9 ⇒ ratio 上界 < 0.9
                continue
            if t1 == t2 or SequenceMatcher(None, t1, t2).ratio() >= 0.9:
                dup = True
                break
        if not dup:
            seen.append(paper)
            seen_norms.append(t1)
            seen_set.add(t1)
    return seen
