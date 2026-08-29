"""检索页（index 0）—— 课题输入 + 关键词拖拽三区 + 检索结果表 + 文献详情侧栏。"""
import asyncio
import os
import threading

import flet as ft

from pages.context import (
    ctx, THEMES, _border,
    FS_XS, FS_SM, FS_MD, FS_LG, FS_XL, FS_XXL, FS_HERO,
    FW_REGULAR, FW_MEDIUM, FW_SEMIBOLD, FW_BOLD,
    R_SM, R_MD, R_LG, R_XL, SP_XS, SP_SM, SP_MD, SP_LG, SP_XL, SP_XXL,
    text_primary, text_secondary, text_tertiary, border_color,
    seed_color, app_bg, surface, surface_hi, accent_container,
    subtle_shadow, card,
)
from pages.settings_page import (
    arxiv_switch, openalex_switch, europepmc_switch, max_results_slider,
    top_k_slider, ce_candidates_slider,
)
from paperpilot.keywords import extract_all_keywords, merge_keywords
from paperpilot.mt_translator import translate_terms
from pages.components import is_shift_pressed
from paperpilot.fetcher import (
    fetch_arxiv, fetch_openalex, fetch_europepmc, fetch_with_cascade,
    fetch_multi_primary, deduplicate, get_article_type_label, SourceRateLimited,
)
from paperpilot.indexer import rank_papers, unload_cross_encoder
from paperpilot import library
from paperpilot import repo_manager, downloader
from paperpilot.pdf_viewer import open_full_reader

state = ctx.state


# ── 检索页模块级状态 ──
_sort_ascending = False
_sort_column = "score"
_search_list = ft.Column(spacing=0)
_ai_scored = False
_last_sort_time = 0
_search_check_handler = None
detail_sidebar: ft.Container | None = None
_sidebar_busy = False


def _has_cjk(text: str) -> bool:
    """检测文本是否包含中日韩字符，用于区分中英文关键词。"""
    return any('一' <= c <= '鿿' for c in text)


_SOURCE_LABELS = {"arxiv": "arXiv", "openalex": "OpenAlex", "europepmc": "Europe PMC"}


def _rate_limited_names(errors: list) -> list[str]:
    """从源错误列表提取被限流的源显示名（保持稳定顺序）。"""
    order = ["openalex", "arxiv", "europepmc"]
    limited = {s for s, kind, _ in errors or [] if kind == "rate_limited"}
    return [_SOURCE_LABELS[s] for s in order if s in limited]


def _openalex_key_hint() -> str:
    """OpenAlex 被限流时的补充引导（未配置 key 时提示免费注册）。"""
    from paperpilot.sources.openalex_source import _get_api_key
    if not _get_api_key():
        return "（2026-02 起 OpenAlex 无 key 每日仅 100 次，可在设置页免费配置 API Key 提升至 10 万次/天）"
    return ""


def _add_src_error(src_errors: list, e: "SourceRateLimited") -> None:
    """记录限流错误（按 source+kind 去重）。"""
    if not any(s == e.source and k == "rate_limited" for s, k, _ in src_errors):
        src_errors.append((e.source, "rate_limited", e.message))


def _run_pipeline(max_per: int, year_min: str, year_max: str,
                  use_arxiv: bool, use_openalex: bool, use_europepmc: bool,
                  top_k: int, ce_candidates: int):
    """在后台线程中运行完整的搜索流水线。

    所有 Flet 控件值由主线程读取后传入，避免跨线程访问控件。
    返回 (papers, scores, src_errors)；src_errors 收集各源限流/错误信息。
    """
    import time as _time
    _t0 = _time.time()
    print(f"[PaperPilot] === 流水线启动 === max_per={max_per}, arxiv={use_arxiv}, "
          f"openalex={use_openalex}, top_k={top_k}, ce_candidates={ce_candidates}", flush=True)

    papers = []
    src_errors: list = []

    # 0. 年份筛选
    if year_min or year_max:
        print(f"[PaperPilot] 年份筛选: {year_min or '—'} ~ {year_max or '—'}", flush=True)

    # 1. 翻译课题描述
    state.status_text = "翻译课题描述..."
    desc_en_query = None
    desc = state.topic_desc.strip()
    desc_en_terms = translate_terms([desc])
    desc_en = [t for t in desc_en_terms if t and not _has_cjk(t)]
    if desc_en:
        desc_en_query = desc_en[0]
        print(f"[PaperPilot] 课题描述翻译: {desc_en_query[:80]}...")
    elif not _has_cjk(desc):
        desc_en_query = desc
        print(f"[PaperPilot] 课题描述原文即英文: {desc_en_query[:80]}...")

    # 2. 翻译三层关键词
    state.status_text = "翻译关键词..."
    primary_en_list = [t for t in translate_terms(state.primary_keywords)
                       if t and not _has_cjk(t)]
    secondary_en = [t for t in translate_terms(state.secondary_keywords)
                    if t and not _has_cjk(t)]
    regular_en = [t for t in translate_terms(state.regular_keywords)
                  if t and not _has_cjk(t)]

    primary_kw_list = primary_en_list  # 所有主关键词作为 AND 核心

    print(f"\n[PaperPilot] 开始检索")
    print(f"[PaperPilot] 主关键词: {primary_kw_list}")
    print(f"[PaperPilot] 副关键词: {secondary_en}")
    print(f"[PaperPilot] 普通关键词: {regular_en}")

    # 3. arXiv 检索（单次级联，避免多路并发触发限流）
    if use_arxiv:
        state.status_text = "arXiv 抓取中..."
        try:
            arxiv_papers, arxiv_level = fetch_with_cascade(
                primary_kw=primary_kw_list,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="arxiv",
                max_results=max_per,
                min_results=3,
                year_min=year_min,
                year_max=year_max,
                errors=src_errors,
            )
            print(f"[PaperPilot] arXiv 返回: {len(arxiv_papers)} 篇 (level={arxiv_level})")
            papers += arxiv_papers
        except Exception as e:
            print(f"[PaperPilot] arXiv 失败: {e}")

        if desc_en_query:
            try:
                desc_papers = fetch_arxiv([desc_en_query], max_results=max_per, logic="OR",
                                          year_min=year_min, year_max=year_max)
                print(f"[PaperPilot] arXiv（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except SourceRateLimited as e:
                _add_src_error(src_errors, e)
            except Exception as e:
                print(f"[PaperPilot] arXiv（描述）失败: {e}")

    if use_openalex:
        state.status_text = "OpenAlex 抓取中..."
        try:
            oa_papers = fetch_multi_primary(
                primary_kw=primary_kw_list,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="openalex",
                max_results=max_per,
                min_results=3,
                year_min=year_min,
                year_max=year_max,
                errors=src_errors,
            )
            print(f"[PaperPilot] OpenAlex 返回: {len(oa_papers)} 篇 ({len(primary_kw_list)}路主关键词)")
            papers += oa_papers
        except Exception as e:
            print(f"[PaperPilot] OpenAlex 失败: {e}")

        if desc_en_query:
            try:
                desc_papers = fetch_openalex([desc_en_query], max_results=max_per, logic="OR",
                                             year_min=year_min, year_max=year_max)
                print(f"[PaperPilot] OpenAlex（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except SourceRateLimited as e:
                _add_src_error(src_errors, e)
            except Exception as e:
                print(f"[PaperPilot] OpenAlex（描述）失败: {e}")

    if use_europepmc:
        state.status_text = "Europe PMC 抓取中..."
        try:
            epmc_papers = fetch_multi_primary(
                primary_kw=primary_kw_list,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="europepmc",
                max_results=max_per,
                min_results=3,
                year_min=year_min,
                year_max=year_max,
                errors=src_errors,
            )
            print(f"[PaperPilot] Europe PMC 返回: {len(epmc_papers)} 篇 ({len(primary_kw_list)}路主关键词)")
            papers += epmc_papers
        except Exception as e:
            print(f"[PaperPilot] Europe PMC 失败: {e}")

        if desc_en_query:
            try:
                desc_papers = fetch_europepmc([desc_en_query], max_results=max_per, logic="OR",
                                              year_min=year_min, year_max=year_max)
                print(f"[PaperPilot] Europe PMC（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except SourceRateLimited as e:
                _add_src_error(src_errors, e)
            except Exception as e:
                print(f"[PaperPilot] Europe PMC（描述）失败: {e}")

    # 4. 去重
    state.status_text = "去重中..."
    papers = deduplicate(papers)
    print(f"[PaperPilot] 去重后: {len(papers)} 篇")

    if not papers:
        print("[PaperPilot] 未找到论文")
        return [], [], src_errors

    # 5. 排序打分（首次会加载 942MB 语义模型，约需 10-30 秒）
    state.status_text = f"语义精排中（{len(papers)} 篇）..."
    query_for_scoring = desc_en_query if desc_en_query else state.topic_desc
    scores = rank_papers(
        query=query_for_scoring,
        papers=papers,
        top_k=top_k,
        ce_candidates=ce_candidates,
        primary_kw=primary_en_list,
        secondary_kw=secondary_en,
        regular_kw=regular_en,
    )
    return papers, scores, src_errors


# ── 检索结果表头 / 排序 / 渲染 ──

def _build_search_header():
    """构建检索结果表头（支持点击排序）。"""
    def _hdr(label, column, width=None, expand=None):
        arrow = ""
        if column and _sort_column == column:
            arrow = " ▲" if not _sort_ascending else " ▼"
        return ft.Container(
            content=ft.TextButton(
                content=ft.Text(f"{label}{arrow}", size=FS_SM,
                                weight=FW_SEMIBOLD, color=text_secondary()),
                on_click=lambda e, c=column: sort_table(c) if c else None,
                style=ft.ButtonStyle(padding=ft.padding.Padding(left=SP_XS, top=10, right=SP_XS, bottom=10)),
            ),
            width=width, expand=expand,
            padding=ft.padding.Padding(left=SP_XS, right=SP_XS),
            bgcolor=surface_hi(),
            border=ft.border.Border(bottom=ft.BorderSide(1, border_color())),
        )

    if _ai_scored:
        cols = [_hdr("", None, width=38),   # 复选框占位
                _hdr("#", None, width=34),
                _hdr("标题", "title", expand=4),
                _hdr("作者", "authors", expand=2),
                _hdr("年份", "year", width=52),
                _hdr("来源", None, width=60),
                _hdr("引用", "citations", width=46),
                _hdr("CE得分", "score", width=58),
                _hdr("AI得分", None, width=52),
                _hdr("类型", None, width=54)]
    else:
        cols = [_hdr("", None, width=38),   # 复选框占位
                _hdr("#", None, width=34),
                _hdr("标题", "title", expand=4),
                _hdr("作者", "authors", expand=2),
                _hdr("年份", "year", width=52),
                _hdr("来源", None, width=66),
                _hdr("引用", "citations", width=50),
                _hdr("得分", "score", width=62),
                _hdr("类型", None, width=56)]
    return ft.Row(cols, spacing=0)


def sort_table(column: str):
    global _sort_ascending, _sort_column, _last_sort_time
    import time
    now = time.time()
    if now - _last_sort_time < 0.25:  # 250ms 防抖
        return
    _last_sort_time = now

    if _sort_column == column:
        _sort_ascending = not _sort_ascending
    else:
        _sort_column = column
        _sort_ascending = False

    key_map = {"title": "title", "authors": "authors", "year": "year",
               "citations": "cited_by_count", "score": "score"}
    key = key_map.get(column, "score")
    reverse = _sort_ascending

    if key == "cited_by_count":
        state.scores = sorted(state.scores, key=lambda x: x[0].get(key) or 0, reverse=not reverse)
    elif key == "score":
        if _ai_scored:
            state.scores = sorted(state.scores,
                key=lambda x: x[0].get("ai_score", 0), reverse=reverse)
        else:
            state.scores = sorted(state.scores, key=lambda x: x[1], reverse=reverse)
    else:
        state.scores = sorted(state.scores, key=lambda x: (
            x[0].get(key, "") or ""
        ), reverse=not reverse)

    refresh_results_table()


def _type_badge(paper: dict) -> ft.Container:
    """构建文章类型标签（小色块 + 文字）。"""
    label = get_article_type_label(paper)
    type_colors = {
        "综述": ft.Colors.AMBER,
        "研究论文": ft.Colors.BLUE,
        "书籍章节": ft.Colors.TEAL,
        "书籍": ft.Colors.PURPLE,
        "学位论文": ft.Colors.ORANGE,
        "其他": ft.Colors.OUTLINE,
    }
    color = type_colors.get(label, ft.Colors.OUTLINE)
    return ft.Container(
        ft.Text(label, size=13, color=color, weight=ft.FontWeight.W_600),
        border=_border(color),
        border_radius=4,
        padding=ft.padding.Padding(left=4, top=1, right=4, bottom=1),
    )


def refresh_results_table():
    global _search_list
    scored = state.scores
    ctx.search_checkboxes.clear()
    ctx.search_last_checked_idx = None  # 新一批结果重置 Shift 范围勾选锚点
    rows = [_build_search_header()]
    for i, (paper, score) in enumerate(scored):
        year_str = str(paper.get("year") or "—")
        cit = paper.get("cited_by_count")
        cit_str = str(cit) if cit is not None else "—"
        source_label = {"arxiv": "arXiv", "openalex": "OpenAlex", "europepmc": "EPMC", "local_pdf": "本地"}
        src = source_label.get(paper.get("source", ""), paper.get("source", ""))

        score_color = (
            ft.Colors.GREEN if score >= 0.4
            else ft.Colors.ORANGE if score >= 0.2
            else ft.Colors.OUTLINE
        )

        def _cell(content, width=None, expand=None):
            return ft.Container(
                content=ft.Text(content, size=13, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                width=width, expand=expand,
                padding=ft.padding.Padding(left=4, top=6, right=4, bottom=6),
            )

        def _badge_cell(paper, width=56):
            badge = _type_badge(paper)
            return ft.Container(
                content=badge, width=width,
                padding=ft.padding.Padding(left=4, top=2, right=4, bottom=2),
            )

        def _ce_cell(value, width):
            return ft.Container(
                content=ft.Text(f"{value:.3f}", size=13, color=score_color,
                                weight=ft.FontWeight.W_600),
                width=width,
                padding=ft.padding.Padding(left=4, top=6, right=4, bottom=6),
            )

        def _ai_cell(paper):
            ai_score = paper.get("ai_score")
            if ai_score is None:
                return ft.Container(width=52)
            reason = paper.get("ai_reason", {})
            tooltip_lines = []
            if reason.get("reason_relevance"):
                tooltip_lines.append(f"相关性：{reason['reason_relevance']}")
            if reason.get("reason_method"):
                tooltip_lines.append(f"方法：{reason['reason_method']}")
            if reason.get("reason_novelty"):
                tooltip_lines.append(f"创新：{reason['reason_novelty']}")
            if reason.get("reason_recency"):
                tooltip_lines.append(f"时效：{reason['reason_recency']}")
            if reason.get("overall"):
                tooltip_lines.append(f"总评：{reason['overall']}")
            tooltip = "\n".join(tooltip_lines) if tooltip_lines else None
            ai_color = (
                ft.Colors.GREEN if ai_score >= 70
                else ft.Colors.ORANGE if ai_score >= 40
                else ft.Colors.OUTLINE
            )
            return ft.Container(
                content=ft.Text(f"{ai_score}", size=13, color=ai_color,
                                weight=ft.FontWeight.W_700),
                width=52,
                padding=ft.padding.Padding(left=4, top=6, right=4, bottom=6),
                tooltip=tooltip,
            )

        if _ai_scored:
            cells = [
                _cell(str(i + 1), width=34),
                _cell(paper.get("title", "")[:80], expand=4),
                _cell((paper.get("authors") or "")[:40], expand=2),
                _cell(year_str, width=52),
                _cell(src, width=60),
                _cell(cit_str, width=46),
                _ce_cell(score, 58),
                _ai_cell(paper),
                _badge_cell(paper, width=54),
            ]
        else:
            cells = [
                _cell(str(i + 1), width=34),
                _cell(paper.get("title", "")[:80], expand=4),
                _cell((paper.get("authors") or "")[:40], expand=2),
                _cell(year_str, width=52),
                _cell(src, width=66),
                _cell(cit_str, width=50),
                _ce_cell(score, 62),
                _badge_cell(paper),
            ]

        is_checked = i in ctx.search_selected_ids
        cb = ft.Checkbox(
            value=is_checked,
            on_change=lambda e, idx=i: _search_check_handler(e, idx),
            scale=0.85,
        )
        ctx.search_checkboxes.append(cb)
        cells.insert(0, ft.Container(content=cb, width=34,
                     padding=ft.padding.Padding(left=4)))

        row = ft.Container(
            content=ft.Row(cells, spacing=0),
            on_click=lambda e, p=paper: show_paper_detail(p),
            border=ft.border.Border(
                bottom=ft.BorderSide(1, border_color())),
        )
        rows.append(row)

    _search_list.controls = rows
    _search_list.update()


# ── 文献详情侧栏 ──

def show_paper_detail(paper: dict):
    """在右侧侧边栏展示论文详情。"""
    global _sidebar_busy
    sb = detail_sidebar
    if sb is None or _sidebar_busy:
        return
    _sidebar_busy = True
    sb._title.value = paper.get("title", "")
    source = {"arxiv": "arXiv", "openalex": "OpenAlex", "europepmc": "EPMC", "local_pdf": "本地"}.get(
        paper.get("source", ""), paper.get("source", "")
    )
    type_label = get_article_type_label(paper)
    meta_parts = [
        f"作者: {paper.get('authors', '未知')}",
        f"年份: {paper.get('year', '—')}",
        f"来源: {source}",
        f"类型: {type_label}",
    ]
    journal = paper.get("journal")
    if journal:
        meta_parts.append(f"期刊: {journal}")
    cit = paper.get("cited_by_count")
    if cit is not None:
        meta_parts.append(f"引用次数: {cit}")
    sb._meta.value = "  |  ".join(meta_parts)
    sb._abstract.value = paper.get("abstract", "") or "（无摘要）"
    # ── 可点击链接 ──
    links = []
    read_btn = ft.TextButton("阅读原文", icon=ft.Icons.OPEN_IN_BROWSER)
    import_btn = ft.TextButton("导入PDF", icon=ft.Icons.UPLOAD,
                               visible=True)
    # 保存当前 paper 引用，供回调闭包使用
    _current_paper = paper

    def _on_read(e, p=_current_paper):
        read_btn.disabled = True
        read_btn.text = "正在检查..."
        read_btn.icon = ft.Icons.HOURGLASS_EMPTY
        read_btn.update()

        import_result = {"path": None}

        def _bg_try():
            """后台线程：优先查 repo 缓存，未命中则下载并缓存到 repo。"""
            try:
                from pathlib import Path as _P

                # 1. 先查 repo_manager 缓存
                cached = None
                try:
                    cached = repo_manager.get_cached_pdf(p)
                except Exception as ex:
                    print(f"[_on_read] step=cache_lookup error={type(ex).__name__}: {ex}", flush=True)
                if cached and _P(cached).is_file():
                    import_result["ok"] = True
                    import_result["action"] = ("pdf", cached)
                    import_result["done"] = True
                    return

                # 2. 下载 PDF
                print(f"[_on_read] step=download title={p.get('title', '')[:80]}", flush=True)
                pdf_path = None
                try:
                    from paperpilot.downloader import cache_pdf as _dl_cache_pdf
                    pdf_path = _dl_cache_pdf(p)
                except Exception as ex:
                    print(f"[_on_read] step=download error={type(ex).__name__}: {ex}", flush=True)
                if pdf_path and _P(pdf_path).is_file():
                    # 3. 存入 repo_manager 缓存（LRU 管理）
                    repo_path = None
                    try:
                        repo_path = repo_manager.cache_pdf(p, pdf_path)
                    except Exception as ex:
                        print(f"[_on_read] step=repo_cache error={type(ex).__name__}: {ex}", flush=True)
                    final_path = repo_path if repo_path else pdf_path
                    import_result["ok"] = True
                    import_result["action"] = ("pdf", final_path)
                    import_result["done"] = True
                    return
            except Exception as ex:
                import_result["error"] = str(ex)
                print(f"[_on_read] bg_try error={type(ex).__name__}: {ex}", flush=True)
                import traceback
                traceback.print_exc()

            import_result["ok"] = False
            import_result["done"] = True

        import threading as _th
        _th.Thread(target=_bg_try, daemon=True).start()

        async def _poll_read():
            import asyncio as _a
            while not import_result.get("done"):
                await _a.sleep(0.5)

            read_btn.text = "阅读原文"
            read_btn.icon = ft.Icons.OPEN_IN_BROWSER
            read_btn.disabled = False
            read_btn.update()

            if import_result.get("ok"):
                # 自动获取成功 → 打开阅读器
                action = import_result.get("action")
                if action:
                    atype, apath = action
                    if atype == "pdf":
                        p["pdf_path"] = apath
                    theme = THEMES[state.theme_name]["seed"]
                    dm = state.dark_mode
                    threading.Thread(
                        target=open_full_reader, args=(p,),
                        kwargs={"theme_seed": theme, "dark_mode": dm},
                        daemon=True,
                    ).start()
                return

            # 自动获取失败 → 弹出对话框
            _show_manual_download_dialog(p)

        ctx.page.run_task(_poll_read)

    read_btn.on_click = _on_read

    async def _on_import(e, p=_current_paper):
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
            print("[_on_import] AllowSetForegroundWindow(-1) OK", flush=True)
        except Exception as ex:
            print(f"[_on_import] AllowSetForegroundWindow failed: {ex}", flush=True)

        import_result = {"selected": None, "done": False}

        def _bg_pick():
            print("[_bg_pick] started", flush=True)
            try:
                import subprocess, tempfile, os as _os
                script = (
                    'Add-Type -AssemblyName System.Windows.Forms\n'
                    'Add-Type -TypeDefinition @"\n'
                    'using System; using System.Runtime.InteropServices;\n'
                    'public class FH{\n'
                    '  [DllImport("user32.dll")]public static extern void keybd_event(byte a,byte b,uint c,UIntPtr d);\n'
                    '  [DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);\n'
                    '}\n'
                    '"@ -ErrorAction SilentlyContinue\n'
                    '[FH]::keybd_event(0x12,0,0,[UIntPtr]::Zero)\n'
                    '[FH]::keybd_event(0x12,0,2,[UIntPtr]::Zero)\n'
                    '$owner=New-Object System.Windows.Forms.Form\n'
                    '$owner.Size=New-Object System.Drawing.Size(0,0)\n'
                    "$owner.StartPosition='Manual'\n"
                    '$owner.Location=New-Object System.Drawing.Point(-32000,-32000)\n'
                    "$owner.FormBorderStyle='None'\n"
                    '$owner.ShowInTaskbar=$false\n'
                    '$owner.TopMost=$true\n'
                    '$owner.Show()\n'
                    '[void][FH]::SetForegroundWindow($owner.Handle)\n'
                    '[System.Windows.Forms.Application]::DoEvents()\n'
                    '$f=New-Object System.Windows.Forms.OpenFileDialog\n'
                    "$f.Filter='PDF Files (*.pdf)|*.pdf'\n"
                    "$f.Title='选择下载好的 PDF 文件'\n"
                    "if($f.ShowDialog($owner) -eq 'OK'){Write-Output $f.FileName}\n"
                    '$owner.Close();$owner.Dispose()\n'
                    ''
                )
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ps1", delete=False, encoding="utf-8-sig"
                )
                tmp.write(script)
                tmp.close()
                try:
                    r = subprocess.run(
                        ["powershell", "-ExecutionPolicy", "Bypass", "-File", tmp.name],
                        capture_output=True, text=True, timeout=120,
                    )
                    print(f"[_bg_pick] rc={r.returncode} stdout='{r.stdout.strip()[:100]}' stderr='{(r.stderr or '')[:200]}'", flush=True)
                    selected = r.stdout.strip()
                    if selected and _os.path.isfile(selected):
                        import_result["selected"] = selected
                finally:
                    try:
                        _os.unlink(tmp.name)
                    except OSError:
                        pass
            except Exception as ex:
                print(f"[_bg_pick] error: {ex}", flush=True)
            import_result["done"] = True

        import threading as _th
        _th.Thread(target=_bg_pick, daemon=True).start()

        import asyncio as _a
        while not import_result["done"]:
            await _a.sleep(0.3)

        selected = import_result["selected"]
        if not selected:
            return

        dest = repo_manager.cache_pdf(p, selected)
        if not dest:
            dest = selected  # 缓存失败，保留原始路径

        from paperpilot import library as _lib
        doi_for_update = p.get("doi")
        if doi_for_update:
            _lib.set_paper_pdf_path(doi=doi_for_update, pdf_path=str(dest))
            p["pdf_path"] = str(dest)

        theme = THEMES[state.theme_name]["seed"]
        dm = state.dark_mode
        _th.Thread(
            target=open_full_reader, args=(p,),
            kwargs={"theme_seed": theme, "dark_mode": dm},
            daemon=True,
        ).start()

    import_btn.on_click = _on_import

    def _show_manual_download_dialog(p):
        import webbrowser as _wb
        from paperpilot.downloader import pdf_direct_url
        doi = p.get("doi", "")
        # 优先用猜测 PDF 直链：浏览器可通过反爬挑战，通常直接触发下载
        browser_url = pdf_direct_url(p) or (f"https://doi.org/{doi}" if doi else p.get("url", ""))

        def _go_download(e):
            if browser_url:
                _wb.open(browser_url)
            dlg.open = False
            dlg.update()

        def _cancel(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("无法自动获取全文"),
            content=ft.Text(
                "该论文的出版商拦截了程序化下载（反爬挑战），但浏览器通常可以。\n\n"
                "点击「用浏览器下载」，在浏览器中完成下载后，\n"
                "回到此处点击「导入PDF」选择文件即可。\n\n"
                f"论文 DOI: {doi or '无'}"
            ),
            actions=[
                ft.TextButton("取消", on_click=_cancel),
                ft.FilledButton("用浏览器下载", on_click=_go_download),
            ],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    links.append(read_btn)
    links.append(import_btn)

    doi = paper.get("doi")
    if doi:
        import webbrowser
        doi_url = f"https://doi.org/{doi}"
        links.append(ft.TextButton("DOI", icon=ft.Icons.LINK,
                                    on_click=lambda e, u=doi_url: webbrowser.open(u)))
    sb._links.controls = links
    sb.visible = True
    sb.update()
    _sidebar_busy = False


# ── 检索页 ──

def build_search_page(ctx):
    global detail_sidebar, _search_check_handler

    topic_name_field = ft.TextField(
        label="课题名称", hint_text="例如：钙钛矿太阳能电池稳定性",
        prefix_icon=ft.Icons.TITLE, expand=True,
    )
    topic_desc_field = ft.TextField(
        label="课题描述", hint_text="输入 1-3 句描述研究方向，用于论文匹配",
        prefix_icon=ft.Icons.DESCRIPTION, multiline=True, min_lines=3, max_lines=5,
        expand=True,
    )

    # ── 三个拖拽区 ──
    primary_zone_row = ft.Row(wrap=True, spacing=6)
    secondary_zone_row = ft.Row(wrap=True, spacing=6)
    regular_zone_row = ft.Row(wrap=True, spacing=6)

    def _make_draggable_chip(kw: str, zone: str, icon, color):
        """创建可拖拽的关键词 Chip。"""
        chip = ft.Chip(
            label=ft.Text(kw, size=FS_MD, color=text_primary()),
            leading=ft.Icon(icon, size=14, color=color) if icon else None,
            bgcolor=accent_container() if zone == "primary" else surface_hi(),
            on_delete=lambda e, k=kw: _on_delete_keyword(k),
        )
        return ft.Draggable(
            content=chip,
            data={"kw": kw, "from": zone},
            group="kw",
            content_feedback=ft.Chip(
                label=ft.Text(kw),
                bgcolor=surface_hi(),
            ),
        )

    def _on_delete_keyword(kw: str):
        """从所有区域中删除关键词。"""
        state.primary_keywords = [k for k in state.primary_keywords if k != kw]
        state.secondary_keywords = [k for k in state.secondary_keywords if k != kw]
        state.regular_keywords = [k for k in state.regular_keywords if k != kw]
        state.keywords = [k for k in state.keywords if k != kw]
        refresh_all_zones()

    def _make_on_accept(target_zone: str):
        """创建 DragTarget on_accept 回调。"""
        def on_accept(e: ft.DragTargetEvent):
            if e.src is None:
                return
            kw = e.src.data["kw"]
            from_zone = e.src.data["from"]
            if from_zone == target_zone:
                return
            # 从原区域移除
            for attr in ["primary_keywords", "secondary_keywords", "regular_keywords"]:
                lst = getattr(state, attr)
                if kw in lst:
                    lst.remove(kw)
                    break
            # 添加到目标区域
            if target_zone == "primary":
                state.primary_keywords.append(kw)
            elif target_zone == "secondary":
                state.secondary_keywords.append(kw)
            else:
                state.regular_keywords.append(kw)
            refresh_all_zones()
        return on_accept

    _drop_border = ft.BorderSide(2, ft.Colors.PRIMARY)

    def _make_zone(label: str, hint: str, chip_row: ft.Row,
                   zone_name: str, icon, color):
        """构建拖拽区：标题 + DragTarget。"""
        def on_will_accept(e: ft.DragTargetEvent):
            if e.src is None:
                return False
            if e.src.data and e.src.data.get("from") != zone_name:
                zone_container.border = ft.Border(
                    _drop_border, _drop_border, _drop_border, _drop_border
                )
                zone_container.update()
                return True
            return False

        def on_leave(e: ft.DragTargetEvent):
            zone_container.border = _border(border_color())
            zone_container.update()

        zone_container = ft.Container(
            content=chip_row,
            border=_border(border_color()),
            border_radius=R_MD,
            padding=SP_MD,
            bgcolor=surface(),
        )

        drag_target = ft.DragTarget(
            content=zone_container,
            group="kw",
            on_will_accept=on_will_accept,
            on_accept=_make_on_accept(zone_name),
            on_leave=on_leave,
        )

        return ft.Column([
            ft.Row([
                ft.Icon(icon, size=16, color=color) if icon else ft.Text(""),
                ft.Text(label, size=FS_MD, weight=FW_SEMIBOLD, color=text_primary()),
            ], spacing=SP_XS),
            drag_target,
            ft.Text(hint, size=FS_XS, color=text_tertiary()),
        ], spacing=SP_XS)

    def refresh_all_zones():
        """刷新三个拖拽区的 Chip 显示。"""
        zones = [
            ("primary",   state.primary_keywords,   primary_zone_row,
             ft.Icons.STAR, ft.Colors.AMBER, "拖拽关键词至此设为「主关键词」"),
            ("secondary", state.secondary_keywords, secondary_zone_row,
             ft.Icons.ARROW_FORWARD, ft.Colors.PRIMARY, "拖拽关键词至此设为「副关键词」"),
            ("regular",   state.regular_keywords,   regular_zone_row,
             None, None, "拖拽关键词至此设为「普通关键词」"),
        ]
        for zone_name, keywords, row, icon, color, hint in zones:
            row.controls.clear()
            for kw in keywords:
                row.controls.append(_make_draggable_chip(kw, zone_name, icon, color))
            if not keywords:
                row.controls.append(
                    ft.Text(hint, size=13, color=ft.Colors.OUTLINE)
                )
            try:
                row.update()
            except RuntimeError:
                pass  # 控件尚未挂载到页面，跳过更新

    manual_kw_field = ft.TextField(
        label="手动添加关键词", hint_text="输入后回车添加",
        prefix_icon=ft.Icons.ADD, expand=True,
    )
    progress_bar = ft.ProgressBar(visible=False, expand=True)
    status_text = ft.Text("", size=13)

    # ── 文献详情侧边栏 ──
    sb_title = ft.Text("", size=FS_XXL, weight=FW_SEMIBOLD, selectable=True, color=text_primary())
    sb_meta = ft.Text("", size=FS_MD, selectable=True, color=text_secondary())
    sb_abstract = ft.Text("", size=FS_MD, selectable=True, color=text_primary())
    sb_links = ft.Row([], spacing=SP_SM)

    def on_close_sidebar(e):
        global _sidebar_busy
        if _sidebar_busy:
            return
        _sidebar_busy = True
        detail_sidebar.visible = False
        detail_sidebar.update()
        _sidebar_busy = False

    sidebar = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Text("文献详情", size=FS_XL, weight=FW_SEMIBOLD, color=text_primary()),
                ft.IconButton(icon=ft.Icons.CLOSE, on_click=on_close_sidebar,
                              icon_color=text_secondary()),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Divider(height=1, color=border_color()),
            sb_title,
            sb_meta,
            ft.Divider(height=1, color=border_color()),
            ft.Text("摘要", size=FS_LG, weight=FW_MEDIUM, color=text_secondary()),
            ft.Container(content=sb_abstract, expand=True),
            ft.Divider(height=1, color=border_color()),
            sb_links,
        ], spacing=SP_SM),
        width=400,
        right=0,
        top=0,
        bottom=0,
        padding=ft.padding.Padding(left=SP_LG, top=SP_MD, right=SP_LG, bottom=SP_MD),
        border=_border(border_color()),
        border_radius=R_LG,
        bgcolor=surface(),
        shadow=subtle_shadow(),
        visible=False,
    )
    sidebar._title = sb_title
    sidebar._meta = sb_meta
    sidebar._abstract = sb_abstract
    sidebar._links = sb_links
    detail_sidebar = sidebar

    # ── 检索结果区域 ──
    def _summary_text():
        return (
            f"{state.topic_name}  |  检索到 {len(state.scores)} 篇论文  |  "
            f"关键词：{', '.join(state.keywords[:5])}"
        )

    summary = ft.Text(_summary_text(), size=14)

    # ── 检索结果多选（始终可见）──
    ctx.search_selected_ids.clear()

    search_select_count = ft.Text("未选中", size=13)
    search_compare_btn = ft.OutlinedButton(
        content=ft.Text("对比分析"),
        icon=ft.Icons.COMPARE,
        tooltip="对比分析选中的论文（至少 2 篇）",
        visible=False,
        disabled=True,
        style=ft.ButtonStyle(padding=ft.padding.Padding(left=14, top=6, right=14, bottom=6)),
    )
    search_select_all_cb = ft.Checkbox(label="全选")

    def _update_search_count():
        n = len(ctx.search_selected_ids)
        if ctx.search_select_count_ref:
            ctx.search_select_count_ref.value = f"已选 {n} 篇" if n else "未选中"
            ctx.search_select_count_ref.update()
        if ctx.search_compare_btn:
            ctx.search_compare_btn.visible = (n >= 2)
            try:
                ctx.search_compare_btn.update()
            except RuntimeError:
                pass
        save_to_library_btn.content = ft.Text("保存选中" if n else "保存到文献库")
        try:
            save_to_library_btn.update()
        except RuntimeError:
            pass
        # 同步到 Agent 自动上下文
        if n:
            ctx.agent_paper_selection = [state.scores[i][0] for i in sorted(ctx.search_selected_ids) if i < len(state.scores)]
        else:
            ctx.agent_paper_selection.clear()

    def _on_search_select_all(e):
        checked = e.control.value
        if checked:
            ctx.search_selected_ids.update(range(len(state.scores)))
        else:
            ctx.search_selected_ids.clear()
        # 直接翻转已有复选框，不重建整表
        for cb in ctx.search_checkboxes:
            cb.value = checked
            cb.update()
        _update_search_count()

    def _on_search_check_one(e, idx: int):
        if e.control.value:
            ctx.search_selected_ids.add(idx)
            # Shift+点击：勾选上次点击行到本次行之间的全部行（Gmail 式范围勾选）。
            # 检索结果复选框按 scores 顺序渲染，ctx.search_checkboxes[k] ↔ 索引 k；
            # 锚点不移动，便于从同一锚点连续扩展区间。
            last = getattr(ctx, "search_last_checked_idx", None)
            if is_shift_pressed() and last is not None and last != idx:
                i1, i2 = sorted((last, idx))
                for i in range(i1, i2 + 1):
                    ctx.search_selected_ids.add(i)
                    if i < len(ctx.search_checkboxes):
                        ctx.search_checkboxes[i].value = True
                try:
                    ctx.page.update()  # 区间内的勾选框同步显示为已勾选
                except Exception:
                    pass
        else:
            ctx.search_selected_ids.discard(idx)
        ctx.search_last_checked_idx = idx
        _update_search_count()

    ctx.search_select_count_ref = search_select_count
    _search_check_handler = _on_search_check_one

    search_select_all_cb.on_change = _on_search_select_all

    def _on_search_compare(e):
        """对比分析检索结果选中的论文。"""
        if len(ctx.search_selected_ids) < 2:
            return
        sel = [state.scores[i][0] for i in sorted(ctx.search_selected_ids) if i < len(state.scores)]
        ctx.trigger_compare_papers(sel, source="search")

    search_compare_btn.on_click = _on_search_compare
    ctx.search_compare_btn = search_compare_btn

    ai_limit_dd = ft.Dropdown(
        options=[
            ft.dropdown.Option("50", "50 篇"),
            ft.dropdown.Option("20", "20 篇"),
            ft.dropdown.Option("10", "10 篇"),
        ],
        value="50",
        width=80,
        visible=False,
        text_size=13,
        content_padding=ft.padding.Padding(left=8, right=8),
    )

    ai_score_btn = ft.OutlinedButton(
        content=ft.Text("AI 精排"),
        icon=ft.Icons.PSYCHOLOGY,
        tooltip="AI 基于摘要打分排序",
        visible=False,
        on_click=None,  # 稍后绑定
        style=ft.ButtonStyle(padding=ft.padding.Padding(left=16, top=8, right=16, bottom=8)),
    )

    _ai_score_status = ft.Text("", size=13, visible=False)

    def on_ai_score(e):
        """AI 精排：筛选有摘要的论文 → 批量打分 → 重排序。"""
        global _ai_scored
        if not ctx.ai_service.is_available:
            ai_score_btn.tooltip = "需要配置 DeepSeek API Key"
            ai_score_btn.update()
            if e is None:
                ctx.send_agent_message("AI 服务不可用，请检查 config.yaml 中的 API Key 配置。", role="system")
            return

        # 收集论文
        all_papers = [p for p, _ in state.scores]
        if not all_papers:
            return

        limit = int(ai_limit_dd.value)

        # 收集候选论文，保留原始 state.scores 索引
        candidates_idx: list[tuple[int, dict]] = []

        if ctx.search_selected_ids:
            # 手动选中：全量打分，上限50
            for i in sorted(ctx.search_selected_ids):
                if i >= len(state.scores):
                    continue
                candidates_idx.append((i, state.scores[i][0]))
                if len(candidates_idx) >= 50:
                    break
        else:
            # 未选中：取前 N 篇，N 由下拉框控制
            for i, (p, _) in enumerate(state.scores):
                candidates_idx.append((i, p))
                if len(candidates_idx) >= limit:
                    break

        candidates = [p for _, p in candidates_idx]

        # debug: 记录摘要长度分布
        import logging as _log
        _log.getLogger(__name__).info(
            "on_ai_score: called from %s, total=%d, limit=%d, candidates=%d, "
            "abstract_lens=%s, topic=%s",
            "agent" if e is None else "manual",
            len(all_papers), limit, len(candidates),
            [len((p.get("abstract") or "").strip()) for p in candidates[:5]],
            (state.topic_desc or state.topic_name)[:60],
        )

        if not candidates_idx:
            _ai_score_status.value = "没有可评分的论文"
            _ai_score_status.visible = True
            _ai_score_status.update()
            return

        source_label = "已选" if ctx.search_selected_ids else f"前 {limit}"
        ai_score_btn.disabled = True
        ai_score_btn.content = ft.Text("AI 评分中...")
        _ai_score_status.value = f"正在分析{source_label} {len(candidates_idx)} 篇论文..."
        _ai_score_status.visible = True
        ai_score_btn.update()
        _ai_score_status.update()

        # 后台线程
        _done = threading.Event()
        _results: list[dict] = []

        def _run():
            nonlocal _results
            try:
                _results = ctx.ai_service.score_papers(
                    state.topic_desc or state.topic_name, candidates,
                    max_papers=int(ai_limit_dd.value))
            except Exception as ex:
                _results = []
                _log.getLogger(__name__).warning(f"AI score_papers error: {ex}")
            finally:
                _done.set()

        threading.Thread(target=_run, daemon=True).start()

        async def _poll():
            import asyncio
            while not _done.is_set():
                await asyncio.sleep(0.3)

            ai_score_btn.disabled = False
            ai_score_btn.content = ft.Text("AI 精排")

            if not _results:
                _ai_score_status.value = "AI 评分失败：网络超时 / API 繁忙 / 返回格式异常，可减少评分篇数后重试"
                _ai_score_status.update()
                ai_score_btn.update()
                return

            # 将 AI 分数合并到 state.scores
            pos_to_orig = {pos: orig_i for pos, (orig_i, _) in enumerate(candidates_idx)}
            score_map = {}
            for r in _results:
                pos = r.get("index", -1)
                if pos in pos_to_orig:
                    score_map[pos_to_orig[pos]] = r

            new_scores = []
            for orig_i, (p, ce_score) in enumerate(state.scores):
                if orig_i in score_map:
                    r = score_map[orig_i]
                    p["ai_score"] = r["ai_score"]
                    p["ai_reason"] = r["ai_reason"]
                new_scores.append((p, ce_score))  # 保留原始 CE 分数不变

            state.scores = new_scores
            global _ai_scored, _sort_column, _sort_ascending
            _ai_scored = True
            _sort_column = "score"
            _sort_ascending = False

            _ai_score_status.value = f"AI 精排完成：已评分 {len(_results)} 篇"
            refresh_results_table()
            _ai_score_status.update()
            ai_score_btn.update()

        ctx.page.run_task(_poll)

    ai_score_btn.on_click = on_ai_score

    save_to_library_btn = ft.OutlinedButton(
        content=ft.Text("保存到文献库"),
        icon=ft.Icons.SAVE,
        visible=False,
    )

    def on_save_to_library(e):
        """弹出对话框，选择课题保存检索结果。有选中时保存选中，否则保存全部。"""
        print("[save_to_library] clicked!", flush=True)
        try:
            projects = library.get_all_projects()
        except Exception as ex:
            print(f"[save_to_library] ERROR: {type(ex).__name__}: {ex}", flush=True)
            return
        project_options = [ft.dropdown.Option(str(p.id), p.name) for p in projects]
        project_dd = ft.Dropdown(
            options=project_options,
            hint_text="选择已有课题",
            expand=True,
        )
        new_name_field = ft.TextField(
            label="或新建课题",
            hint_text="输入新课题名称",
            visible=False,
        )
        new_desc_field = ft.TextField(
            label="课题描述",
            hint_text=state.topic_desc[:200],
            visible=False,
        )

        def on_mode_change(e):
            is_new = e.control.value == "new"
            project_dd.visible = not is_new
            new_name_field.visible = is_new
            new_desc_field.visible = is_new
            project_dd.update()
            new_name_field.update()
            new_desc_field.update()

        save_mode = ft.RadioGroup(
            content=ft.Row([
                ft.Radio(value="existing", label="已有课题"),
                ft.Radio(value="new", label="新建课题"),
            ]),
            value="existing",
            on_change=on_mode_change,
        )

        result_text = ft.Text("", size=13)

        def do_save(e):
            nonlocal projects
            save_mode_val = save_mode.value
            project_name = ""
            if save_mode_val == "existing" and project_dd.value:
                pid = int(project_dd.value)
                # 查找课题名
                for p in projects:
                    if str(p.id) == project_dd.value:
                        project_name = p.name
                        break
            elif save_mode_val == "new" and new_name_field.value.strip():
                project_name = new_name_field.value.strip()
                try:
                    proj = library.create_project(
                        project_name,
                        new_desc_field.value.strip() or state.topic_desc,
                    )
                    repo_manager.save_catalog(proj.name, {"papers": {}})
                    pid = proj.id
                except ValueError as ve:
                    result_text.value = str(ve)
                    result_text.color = ft.Colors.ERROR
                    result_text.update()
                    return
            else:
                result_text.value = "请选择课题或输入新课题名称"
                result_text.color = ft.Colors.ERROR
                result_text.update()
                return

            # 有选中时保存选中，否则保存全部
            if ctx.search_selected_ids:
                sel_papers = [state.scores[i][0] for i in ctx.search_selected_ids if i < len(state.scores)]
                sel_scores = [(state.scores[i][0], state.scores[i][1]) for i in ctx.search_selected_ids if i < len(state.scores)]
                n, _ = library.save_papers_to_project(pid, sel_papers, sel_scores)
                papers_to_import = sel_papers
                ctx.search_selected_ids.clear()
            else:
                papers_to_import = [s[0] for s in state.scores]
                n, _ = library.save_papers_to_project(pid, papers_to_import, state.scores)

            # ── PDF 导入 + DB 更新（DOI 优先，标题回退）──
            def _update_db_pdf(paper: dict, pdf_path: str) -> bool:
                """将 pdf_path 写入数据库。DOI 匹配失败时回退到标题匹配。"""
                doi = paper.get("doi") or ""
                if doi and library.set_paper_pdf_path(doi, pdf_path):
                    return True
                title = paper.get("title") or ""
                if title:
                    year = paper.get("year")
                    return library.set_paper_pdf_path_by_title(title, pdf_path, year)
                return False

            # 同步导入已有缓存
            imported = 0
            for paper in papers_to_import:
                pdf = paper.get("pdf_path", "")
                if not pdf or not os.path.isfile(str(pdf)):
                    pdf = repo_manager.get_cached_pdf(paper)
                if pdf and os.path.isfile(str(pdf)):
                    paper["pdf_path"] = pdf
                    repo_path = repo_manager.import_pdf(paper, project_name)
                    if repo_path:
                        _update_db_pdf(paper, repo_path)
                    imported += 1

            # 后台自动下载未缓存的论文 PDF（静默，无弹窗）
            _papers_need_dl = [
                p for p in papers_to_import
                if not (p.get("pdf_path") and os.path.isfile(str(p.get("pdf_path"))))
            ]
            if _papers_need_dl:
                def _auto_download():
                    dl_ok = 0
                    for paper in _papers_need_dl:
                        try:
                            cache_path = downloader.cache_pdf(paper)
                            if cache_path and os.path.isfile(cache_path):
                                paper["pdf_path"] = cache_path
                                repo_path = repo_manager.import_pdf(paper, project_name)
                                if repo_path:
                                    _update_db_pdf(paper, repo_path)
                                dl_ok += 1
                        except Exception:
                            pass
                    if dl_ok:
                        print(f"[auto-dl] Downloaded {dl_ok}/{len(_papers_need_dl)} papers for '{project_name}'", flush=True)
                        if ctx.refresh_paper_list is not None:
                            try:
                                ctx.refresh_paper_list(pid)
                            except Exception:
                                pass
                threading.Thread(target=_auto_download, daemon=True).start()

            result_text.value = f"已保存 {n} 篇论文到文献库" + (f"，{imported} 篇 PDF 已导入" if imported else "")
            if _papers_need_dl:
                result_text.value += f"（{len(_papers_need_dl)} 篇后台下载中...）"
            result_text.color = ft.Colors.GREEN
            result_text.update()

            # 刷新检索结果表格（绿标）和文献库论文列表
            refresh_results_table()
            if ctx.refresh_paper_list is not None:
                try:
                    ctx.refresh_paper_list(pid)
                except Exception:
                    pass

            dlg.open = False
            dlg.update()

        def close_dlg(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("保存到文献库"),
            content=ft.Column([
                ft.Text(f"将 {len(ctx.search_selected_ids) if ctx.search_selected_ids else len(state.scores)} 篇检索结果保存到："),
                save_mode,
                project_dd,
                new_name_field,
                new_desc_field,
                result_text,
            ], spacing=12, tight=True, height=280),
            actions=[
                ft.TextButton("取消", on_click=close_dlg),
                ft.FilledButton("保存", on_click=do_save),
            ],
        )

        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    save_to_library_btn.on_click = on_save_to_library

    # ── 检索结果区（与检索表单同一张卡片内，检索完成后显示）──
    results_section = ft.Column([
        ft.Divider(height=1, color=border_color()),
        ft.Text("检索结果", size=FS_XL, weight=FW_BOLD, color=text_primary()),
        summary,
        ft.Row([
            search_select_all_cb,
            search_select_count,
            search_compare_btn,
            ai_limit_dd,
            ai_score_btn,
            _ai_score_status,
            save_to_library_btn,
        ], alignment=ft.MainAxisAlignment.END, spacing=SP_SM),
        ft.Divider(height=1, color=border_color()),
        _search_list,
    ], spacing=SP_MD, visible=False)

    def on_extract(e):
        desc = topic_desc_field.value.strip()
        if not desc:
            status_text.value = "请先输入检索描述"
            status_text.update()
            return
        status_text.value = "正在提取关键词..."
        status_text.update()

        _done = threading.Event()
        _weighted: list = []
        _err: str | None = None

        def _run():
            nonlocal _err
            try:
                _weighted.extend(extract_all_keywords(desc, top_n=8))
            except Exception as ex:
                _err = str(ex)
            finally:
                _done.set()

        threading.Thread(target=_run, daemon=True).start()

        async def _poll():
            import asyncio
            while not _done.is_set():
                await asyncio.sleep(0.2)
            if _err:
                status_text.value = f"提取失败: {_err}"
            else:
                core_kw = [kw for kw, w in _weighted if w >= 1.0]
                regular_kw = [kw for kw, w in _weighted if 0 < w < 1.0]
                state.primary_keywords = []
                state.secondary_keywords = core_kw
                state.regular_keywords = regular_kw
                state.keywords = [kw for kw, _ in _weighted]
                refresh_all_zones()
                status_text.value = f"已提取 {len(state.keywords)} 个关键词"
            status_text.update()

        ctx.page.run_task(_poll)

    def on_add_keyword(e):
        kw = (e.control.value or "").strip()
        if kw:
            state.secondary_keywords.append(kw)
            state.keywords = merge_keywords(state.keywords, [kw])
            manual_kw_field.value = ""
            manual_kw_field.update()
            refresh_all_zones()

    manual_kw_field.on_submit = on_add_keyword

    def on_start_search(e):
        import logging as _logging
        _logging.getLogger(__name__).info("[on_start_search] called, topic_desc=%r, keywords=%s",
                    topic_desc_field.value.strip()[:60], state.keywords[:5] if state.keywords else "EMPTY")
        if not topic_desc_field.value.strip():
            _logging.getLogger(__name__).warning("[on_start_search] BLOCKED: empty topic_desc")
            status_text.value = "请先输入检索描述"
            status_text.update()
            return
        if not state.keywords:
            _logging.getLogger(__name__).warning("[on_start_search] BLOCKED: empty keywords")
            status_text.value = "请先提取关键词"
            status_text.update()
            return

        import threading

        state.topic_name = topic_name_field.value.strip() or "未命名检索"
        state.topic_desc = topic_desc_field.value.strip()
        state.is_searching = True
        state.papers = []
        state.scores = []

        progress_bar.visible = True
        search_btn.disabled = True
        status_text.value = "正在抓取论文..."
        progress_bar.update()
        search_btn.update()
        status_text.update()

        # 在主线程读取所有 Flet 控件值，避免后台线程跨线程访问控件
        _max_per = int(max_results_slider.value)
        _year_min = ""
        _year_max = ""
        _use_arxiv = arxiv_switch.value
        _use_openalex = openalex_switch.value
        _use_europepmc = europepmc_switch.value
        _top_k = int(top_k_slider.value)
        _ce_candidates = int(ce_candidates_slider.value)

        # 线程间共享结果
        _result: dict = {}       # {"papers": ..., "scores": ...} or {"error": ...}
        _done = threading.Event()

        def _run_in_thread():
            """在独立线程中执行流水线，避免 run_in_executor 嵌套回调丢失。"""
            try:
                papers, scores, src_errors = _run_pipeline(
                    max_per=_max_per, year_min=_year_min, year_max=_year_max,
                    use_arxiv=_use_arxiv, use_openalex=_use_openalex,
                    use_europepmc=_use_europepmc,
                    top_k=_top_k, ce_candidates=_ce_candidates,
                )
                _result["papers"] = papers
                _result["scores"] = scores
                _result["errors"] = src_errors
            except Exception as ex:
                _result["error"] = ex
            finally:
                _done.set()

        threading.Thread(target=_run_in_thread, daemon=True).start()

        async def _poll():
            import traceback
            last_status = status_text.value
            while not _done.is_set():
                await asyncio.sleep(0.5)
                # 仅状态变化时才刷新 UI，避免事件循环拥塞
                if state.status_text != last_status:
                    last_status = state.status_text
                    status_text.value = state.status_text
                    status_text.update()

            # 流水线完成，执行一次性 UI 更新
            try:
                src_errors = _result.get("errors") or []
                limited = _rate_limited_names(src_errors)
                if "error" in _result:
                    state.status_text = f"检索失败: {_result['error']}"
                    traceback.print_exception(
                        type(_result["error"]), _result["error"],
                        _result["error"].__traceback__)
                elif not _result.get("papers"):
                    if limited:
                        key_hint = _openalex_key_hint() if "OpenAlex" in limited else ""
                        state.status_text = (f"未找到论文：{'、'.join(limited)} "
                                             f"被限流(429)，请稍后重试{key_hint}")
                    else:
                        state.status_text = "未找到相关论文"
                    state.papers = []
                    state.scores = []
                    results_section.visible = True
                    _search_list.controls.clear()
                    _search_list.update()
                    summary.value = _summary_text()
                    summary.update()
                else:
                    state.papers = _result["papers"]
                    state.scores = _result["scores"]
                    state.status_text = f"完成！共 {len(_result['scores'])} 篇"
                    if limited:
                        key_hint = _openalex_key_hint() if "OpenAlex" in limited else ""
                        state.status_text += (f"  ⚠ {'、'.join(limited)} 限流(429)，"
                                              f"本次结果可能不完整{key_hint}")
                    results_section.visible = True
                    save_to_library_btn.visible = True
                    ai_score_btn.visible = ctx.ai_service.is_available
                    ai_limit_dd.visible = ctx.ai_service.is_available
                    global _ai_scored
                    _ai_scored = False
                    refresh_results_table()
                    summary.value = _summary_text()
                    summary.update()
                unload_cross_encoder()
            finally:
                state.is_searching = False
                progress_bar.visible = False
                search_btn.disabled = False
                status_text.value = state.status_text
                # 表单区控件只需一次批量更新
                progress_bar.update()
                search_btn.update()
                status_text.update()
                if ctx.page:
                    ctx.page.update()

        ctx.page.run_task(_poll)

    search_btn = ft.FilledButton(
        content=ft.Text("开始检索"), icon=ft.Icons.SEARCH, on_click=on_start_search,
        style=ft.ButtonStyle(padding=ft.padding.Padding(left=32, top=16, right=32, bottom=16)),
    )

    # 初始化分区（恢复已有状态）
    refresh_all_zones()

    # ── 页面布局：检索表单 + 检索结果合并为一张卡片，整体可滚动 ──
    search_card = card(
        ft.Column([
            ft.Text("PaperPilot", size=FS_HERO, weight=FW_BOLD, color=text_primary()),
            ft.Text("智能文献检索与筛选", size=FS_MD, color=text_secondary()),
            ft.Divider(height=1, color=border_color()),
            ft.Text("检索信息", size=FS_XL, weight=FW_SEMIBOLD, color=text_primary()),
            topic_name_field,
            topic_desc_field,
            ft.Row([
                ft.FilledTonalButton(
                    content=ft.Text("提取关键词"), icon=ft.Icons.AUTO_AWESOME,
                    on_click=on_extract,
                ),
                manual_kw_field,
            ], spacing=SP_SM),
            _make_zone("主关键词", "拖拽关键词至此设为「主关键词」",
                       primary_zone_row, "primary",
                       ft.Icons.STAR, ft.Colors.AMBER),
            _make_zone("副关键词", "拖拽关键词至此设为「副关键词」",
                       secondary_zone_row, "secondary",
                       ft.Icons.ARROW_FORWARD, seed_color()),
            _make_zone("普通关键词", "拖拽关键词至此设为「普通关键词」",
                       regular_zone_row, "regular",
                       None, None),
            ft.Divider(height=1, color=border_color()),
            ft.Row([search_btn, progress_bar], spacing=SP_MD),
            status_text,
            results_section,
        ], spacing=SP_MD, scroll=ft.ScrollMode.AUTO, expand=True),
        padding=SP_XL,
        expand=True,
    )

    left_side = ft.Column([
        search_card,
    ], spacing=0, expand=True)

    # ── 注册检索页回调，供 Agent [ACTION:xxx] 标记使用 ──
    ctx.search_actions = {
        "on_start_search": on_start_search,
        "on_ai_score": on_ai_score,
        "on_save_to_library": on_save_to_library,
        "on_extract": on_extract,
        "topic_name": topic_name_field,
        "topic_desc": topic_desc_field,
        "ai_limit_dd": ai_limit_dd,
        "refresh_all_zones": refresh_all_zones,
        "refresh_results_table": refresh_results_table,
        "update_search_count": _update_search_count,
    }

    return ft.Stack([
        left_side,
        sidebar,
    ], expand=True)
