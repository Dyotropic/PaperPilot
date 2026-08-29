"""知识图谱数据构建服务（纯数据组装，可离线单测）。

以课题内论文列表（library.get_project_papers 返回的 dict）为输入，
输出 ECharts graph 可直接消费的 {nodes, edges, warnings, stats} JSON。

三类数据三种来源：
  引用边   — 两级获取：检索时顺带缓存的 refs:{doi}（零请求）优先，
             未命中的走 openalex_source.get_work_refs 批量补查（每批 50 篇 1 次）；
  共现边   — OpenAlex keywords（同上缓存）优先，无覆盖论文用本地
             keywords.extract_keywords_local 提取摘要（结果同样入盘缓存）；
  时间线   — 论文年份字段，坐标在 Python 侧预计算。

本模块自身除 get_work_refs 外不发任何网络请求；调用方须在后台线程执行。
"""

import hashlib
import re

from paperpilot.sources.base import open_cache
from paperpilot.sources.openalex_source import get_work_refs

# ── 可调参数 ──
_MIN_SHARED_KEYWORDS = 2   # 共现边最少共同关键词数
_MAX_EDGES = 2000          # 边数上限（超出按权重截断，防大课题边爆炸）
_LOCAL_KW_TOPN = 8         # 本地兜底提取的关键词数
_TL_X_GAP = 260            # 时间线：相邻年份 x 间距
_TL_LANE_GAP = 170         # 时间线：车道 y 间距
_TL_LANES = 4              # 时间线：车道数

_kw_cache = open_cache("graph")


# ── 内部辅助 ──

_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})",
    re.IGNORECASE,
)


def _arxiv_doi_candidate(paper: dict) -> str:
    """arXiv 论文的 DOI 候选（10.48550/arxiv.{id}，arXiv 官方 DataCite DOI）。

    从 url 提取 arXiv id（含新旧两种格式），去版本号；无法提取返回空串。
    """
    if (paper.get("source") or "") != "arxiv":
        return ""
    m = _ARXIV_URL_RE.search(paper.get("url") or "")
    if not m:
        return ""
    arxiv_id = re.sub(r"v\d+$", "", m.group(1))
    return f"10.48550/arxiv.{arxiv_id.lower()}"


def _norm_kw_list(raw: list) -> list[str]:
    """关键词规范化：去空、去重（大小写不敏感，保留首次出现的原形）。"""
    seen, out = set(), []
    for kw in raw or []:
        kw = str(kw).strip()
        if not kw:
            continue
        low = kw.lower()
        if low not in seen:
            seen.add(low)
            out.append(kw)
    return out


def _local_keywords_cached(text: str) -> list[str]:
    """本地关键词提取（带盘缓存）。文本为空返回空表。"""
    text = (text or "").strip()
    if not text:
        return []
    key = f"kw:{hashlib.md5(text.encode('utf-8')).hexdigest()}"
    if _kw_cache is not None:
        cached = _kw_cache.get(key)
        if cached is not None:
            return list(cached)
    from paperpilot.keywords import extract_keywords_local
    kws = extract_keywords_local(text, top_n=_LOCAL_KW_TOPN)
    if _kw_cache is not None:
        _kw_cache.set(key, kws, expire=None)  # 提取结果确定性强，不设过期
    return kws


def _timeline_coords(nodes: list[dict]) -> None:
    """时间线预排布局：x=年份等距，同年按车道分列，写入 node 的 tx/ty。

    无年份论文排最左"未知年份"列。全程确定性（同输入同输出）。
    """
    with_year = [n for n in nodes if n.get("year")]
    if not with_year:
        for n in nodes:
            n["tx"], n["ty"] = -_TL_X_GAP, 0.0
        return
    min_year = min(n["year"] for n in with_year)

    by_year: dict[int, list[dict]] = {}
    for n in with_year:
        by_year.setdefault(int(n["year"]), []).append(n)
    for grp in by_year.values():
        grp.sort(key=lambda n: str(n.get("title") or ""))

    # 无年份论文列（最左）
    no_year = [n for n in nodes if not n.get("year")]
    no_year.sort(key=lambda n: str(n.get("title") or ""))
    _assign_lanes(no_year, x=-_TL_X_GAP)

    for year, grp in sorted(by_year.items()):
        _assign_lanes(grp, x=(year - min_year) * _TL_X_GAP)


def _assign_lanes(group: list[dict], x: float) -> None:
    """把一组（已排序的）节点按序号轮流分配到 _TL_LANES 条车道。

    同车道内依次叠加 18px 偏移防止完全重合；整体 y 以 0 为中心对称。
    """
    sub: dict[int, int] = {}
    for i, n in enumerate(group):
        lane = i % _TL_LANES
        n["tx"] = x
        n["ty"] = (lane * _TL_LANE_GAP + sub.get(lane, 0) * 18.0
                   - (_TL_LANES - 1) * _TL_LANE_GAP / 2)
        sub[lane] = sub.get(lane, 0) + 1


# ── 主入口 ──

def build_graph_data(project_id: int, papers: list[dict],
                     on_progress=None) -> dict:
    """构建课题知识图谱数据。

    Args:
        project_id: 课题 id（透传到结果，便于调用方核对）
        papers: get_project_papers(project_id) 返回的论文 dict 列表
        on_progress: 可选回调 (stage: str) -> None，进度提示（异常安全）

    Returns:
        {
          "project_id": int,
          "nodes": [{id, label, title, authors, year, source, doi, url, pdf_path,
                     abstract, total_score, ai_score, status, keywords,
                     cite_in, cite_out, cooccur, tx, ty}, ...],
          "edges": [{source, target, kind: "cites"|"cooccur", weight, shared}, ...],
          "warnings": [str, ...],
          "stats": {n_papers, n_resolved, n_cite_edges, n_cooccur_edges,
                    n_edges_shown},
        }
    """
    def _progress(stage: str):
        if on_progress:
            try:
                on_progress(stage)
            except Exception:
                pass

    warnings: list[str] = []
    papers = [p for p in (papers or []) if p.get("id") is not None]
    stats = {"n_papers": len(papers), "n_resolved": 0,
             "n_cite_edges": 0, "n_cooccur_edges": 0, "n_edges_shown": 0}
    if not papers:
        return {"project_id": project_id, "nodes": [], "edges": [],
                "warnings": [], "stats": stats}

    _progress("准备节点")

    # 1. DOI 候选（论文自带 doi 优先，arXiv 构造候选）
    doi_of: dict[int, str] = {}   # paper.id -> 小写 doi 候选
    for p in papers:
        doi = str(p.get("doi") or "").strip().lower() or _arxiv_doi_candidate(p)
        if doi:
            doi_of[p["id"]] = doi

    # 2. 引用事实（两级：缓存优先，缺的批量补查；网络失败降级不中断）
    _progress("解析引用关系（本地缓存 / 必要时补查）")
    payloads: dict[str, dict] = {}
    if doi_of:
        errors: list = []
        try:
            payloads = get_work_refs(list(doi_of.values()), errors=errors)
        except Exception:  # 防御：图谱构建不因引用查询异常而整体失败
            payloads = {}
            errors.append(("openalex", "network", "OpenAlex 引用补查异常"))
        kinds = {k for _, k, _ in errors}
        if "rate_limited" in kinds:
            warnings.append("OpenAlex 被限流(429)，部分论文引用关系未能获取，"
                            "引用边可能不完整；可稍后重试或配置 API Key")
        elif "network" in kinds:
            warnings.append("网络异常，部分论文引用关系未能获取，引用边可能不完整")

    wid_of: dict[int, str] = {}   # paper.id -> OpenAlex W 短 id
    for p in papers:
        payload = payloads.get(doi_of.get(p["id"], ""))
        if payload and payload.get("openalex_id"):
            wid_of[p["id"]] = payload["openalex_id"]
    stats["n_resolved"] = len(wid_of)
    unresolved = len(papers) - len(wid_of)
    if unresolved and len(doi_of) < len(papers):
        warnings.append(
            f"{unresolved} 篇论文无 DOI 或未收录进 OpenAlex，不参与引用边"
            "（仍参与关键词共现与时间线）")
    elif unresolved and doi_of:
        warnings.append(
            f"{unresolved} 篇论文未解析到 OpenAlex 记录，不参与引用边")

    # 3. 节点
    nodes: list[dict] = []
    for p in papers:
        payload = payloads.get(doi_of.get(p["id"], "")) or {}
        kws = _norm_kw_list(payload.get("keywords") or [])
        if not kws:
            kws = _norm_kw_list(_local_keywords_cached(
                p.get("abstract") or p.get("title") or ""))
        nodes.append({
            "id": p["id"],
            "label": (p.get("title") or "").strip()[:60],
            "title": (p.get("title") or "").strip(),
            "authors": (p.get("authors") or "").strip(),
            "year": p.get("year"),
            "source": p.get("source") or "unknown",
            "doi": doi_of.get(p["id"], ""),
            "url": p.get("url"),
            "pdf_path": p.get("pdf_path"),
            "abstract": (p.get("abstract") or "").strip(),
            "total_score": p.get("total_score"),
            "ai_score": p.get("ai_score"),
            "status": p.get("status") or "unread",
            "keywords": kws,
            "cite_in": 0, "cite_out": 0, "cooccur": 0,
            "tx": 0.0, "ty": 0.0,
        })
    node_by_id = {n["id"]: n for n in nodes}

    # 4. 引用边：A 引用 B ⇔ B 的 W id ∈ A.referenced_works
    _progress("构建引用关系边")
    edges: list[dict] = []
    edge_keys: set[tuple] = set()
    for p in papers:
        payload = payloads.get(doi_of.get(p["id"], ""))
        if not payload:
            continue
        refs = set(payload.get("referenced_works") or [])
        if not refs:
            continue
        for q in papers:
            if q["id"] == p["id"]:
                continue
            q_wid = wid_of.get(q["id"])
            if q_wid and q_wid in refs:
                key = (p["id"], q["id"], "cites")
                if key not in edge_keys:
                    edge_keys.add(key)
                    edges.append({"source": p["id"], "target": q["id"],
                                  "kind": "cites", "weight": 1, "shared": []})
    stats["n_cite_edges"] = len(edges)

    # 5. 共现边：共同关键词 ≥ 2，边宽 ∝ 共同词数
    _progress("计算关键词共现")
    kw_sets = {n["id"]: {k.lower() for k in n["keywords"]} for n in nodes}
    kw_disp = {n["id"]: n["keywords"] for n in nodes}
    ids = [n["id"] for n in nodes]
    for i in range(len(ids)):
        si = kw_sets[ids[i]]
        if len(si) < _MIN_SHARED_KEYWORDS:
            continue
        for j in range(i + 1, len(ids)):
            shared = si & kw_sets[ids[j]]
            if len(shared) >= _MIN_SHARED_KEYWORDS:
                key = (ids[i], ids[j], "cooccur")
                if key in edge_keys:
                    continue
                edge_keys.add(key)
                # 共同词按节点关键词原形优先展示
                lower2disp = {k.lower(): k for k in kw_disp[ids[j]]}
                disp = sorted(shared, key=lambda s: (-(len(s)), s))[:3]
                edges.append({
                    "source": ids[i], "target": ids[j], "kind": "cooccur",
                    "weight": len(shared),
                    "shared": [lower2disp.get(s, s) for s in disp],
                })
    stats["n_cooccur_edges"] = sum(1 for e in edges if e["kind"] == "cooccur")

    # 6. 边截断（引用边优先保留，共现按权重降序）
    n_cites = stats["n_cite_edges"]
    if len(edges) > _MAX_EDGES:
        cooccur = sorted((e for e in edges if e["kind"] == "cooccur"),
                         key=lambda e: (-e["weight"], e["source"], e["target"]))
        kept_cooccur = cooccur[:max(0, _MAX_EDGES - n_cites)]
        edges = [e for e in edges if e["kind"] == "cites"] + kept_cooccur
        warnings.append(f"关系边过多，已按权重截断显示前 {_MAX_EDGES} 条")

    # 7. 度数（按最终保留的边统计，节点大小由前端映射）
    for e in edges:
        src, tgt = node_by_id.get(e["source"]), node_by_id.get(e["target"])
        if src is not None and tgt is not None:
            if e["kind"] == "cites":
                src["cite_out"] += 1
                tgt["cite_in"] += 1
            else:
                src["cooccur"] += 1
                tgt["cooccur"] += 1

    # 8. 时间线预排
    _progress("布局时间线")
    _timeline_coords(nodes)

    stats["n_edges_shown"] = len(edges)
    _progress("完成")
    return {"project_id": project_id, "nodes": nodes, "edges": edges,
            "warnings": warnings, "stats": stats}
