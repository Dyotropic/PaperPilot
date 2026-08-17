"""文献库页（index 1）—— 课题列表 + 论文列表 + 状态筛选 + 阅读 + 导出。"""
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
from paperpilot import library
from paperpilot import repo_manager, downloader
from paperpilot.local_import import scan_folder, extract_pdfs
from paperpilot.indexer import rank_papers, unload_cross_encoder
from paperpilot.pdf_viewer import open_full_reader, is_full_reader_available
from paperpilot.ai_service import save_deep_read_json, get_full_text_for_paper
from paperpilot.library import save_deep_read_notes

state = ctx.state


def build_library_page(ctx):
    """文献管理页面：课题列表 + 论文列表 + 状态筛选 + 阅读 + 导出。"""
    # 页面局部状态
    _selected_project_id = None
    _project_papers: list[dict] = []
    _status_filter = "all"
    _sort_mode = "ce"  # "ce" 或 "ai"

    selected_project_title = ft.Text(
        "请选择一个课题", size=14, weight=ft.FontWeight.W_500,
        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True,
    )
    paper_count_text = ft.Text("", size=13)

    def refresh_project_list():
        """从数据库刷新课题列表，填充左侧导航的课题子菜单。"""
        projects = library.get_all_projects()
        sub = ctx.library_project_submenu
        if sub is None:
            return
        sub.controls.clear()
        if not projects:
            sub.controls.append(
                ft.Text("暂无课题", size=FS_XS, color=text_tertiary(),
                        padding=ft.padding.Padding(left=SP_MD, top=4, bottom=4))
            )
        else:
            for proj in projects:
                is_active = ctx.selected_project_id == proj.id
                item = ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.FOLDER, size=14,
                                color=seed_color() if is_active else text_tertiary()),
                        ft.Text(proj.name, size=FS_SM,
                               weight=FW_SEMIBOLD if is_active else FW_REGULAR,
                               color=text_primary() if is_active else text_secondary(),
                               max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                    ], spacing=SP_SM),
                    on_click=lambda e, pid=proj.id: ctx.library_select_project(pid),
                    border_radius=R_SM,
                    bgcolor=accent_container() if is_active else None,
                    padding=ft.padding.Padding(left=SP_MD, top=7, right=SP_MD, bottom=7),
                    ink=True,
                )
                sub.controls.append(item)
        try:
            sub.update()
        except RuntimeError:
            pass

    # 暴露给 page_switcher，切到文献页时自动刷新
    ctx.refresh_library = refresh_project_list

    def on_new_project(e):
        """新建课题对话框。"""
        name_field = ft.TextField(label="课题名称", hint_text="例如：钙钛矿太阳能电池")
        desc_field = ft.TextField(label="课题描述", hint_text="输入1-3句描述", multiline=True, min_lines=2, max_lines=4)
        msg = ft.Text("", size=13)

        def do_create(e):
            if not name_field.value.strip():
                msg.value = "请输入课题名称"
                msg.color = ft.Colors.ERROR
                msg.update()
                return
            try:
                proj = library.create_project(name_field.value.strip(), desc_field.value.strip())
                repo_manager.save_catalog(proj.name, {"papers": {}})
                msg.value = f"已创建「{proj.name}」"
                msg.color = ft.Colors.GREEN
                msg.update()
                refresh_project_list()
                dlg.open = False
                dlg.update()
            except ValueError as ve:
                msg.value = str(ve)
                msg.color = ft.Colors.ERROR
                msg.update()

        def close_dlg(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("新建课题"),
            content=ft.Column([name_field, desc_field, msg], spacing=12, tight=True, height=200),
            actions=[ft.TextButton("取消", on_click=close_dlg), ft.FilledButton("创建", on_click=do_create)],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    def on_edit_project(e):
        """编辑当前选中课题（名称 + 描述）。"""
        pid = _selected_project_id
        if pid is None:
            return
        proj = library.get_project(pid)
        if not proj:
            return

        def do_save(e):
            new_name = name_field.value.strip()
            if not new_name:
                msg.value = "名称不能为空"
                msg.color = ft.Colors.ERROR
                msg.update()
                return
            new_desc = desc_field.value.strip()
            if library.update_project(pid, name=new_name, description=new_desc):
                selected_project_title.value = new_name
                ctx.set_agent_project(pid, new_name, new_desc)
                refresh_project_list()
                selected_project_title.update()
                dlg.open = False
                dlg.update()

        name_field = ft.TextField(value=proj.name, label="课题名称", autofocus=True)
        desc_field = ft.TextField(
            value=proj.description or "",
            label="课题描述",
            hint_text="输入1-3句描述研究方向，用于论文匹配",
            multiline=True, min_lines=2, max_lines=4,
        )
        msg = ft.Text("", size=13)

        def close_dlg(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("编辑课题"),
            content=ft.Column([name_field, desc_field, msg], spacing=12, tight=True, height=240),
            actions=[ft.TextButton("取消", on_click=close_dlg), ft.FilledButton("保存", on_click=do_save)],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    def on_delete_project(e):
        """删除当前选中课题。"""
        nonlocal _selected_project_id
        pid = _selected_project_id
        if pid is None:
            return

        def do_delete(e):
            nonlocal _selected_project_id
            proj = library.get_project(pid)
            if proj:
                repo_manager.move_project_to_recycle(proj.name)
            library.delete_project(pid)
            _selected_project_id = None
            ctx.selected_project_id = None
            ctx.set_agent_project(None)
            selected_project_title.value = "请选择一个课题"
            paper_count_text.value = ""
            refresh_project_list()
            refresh_paper_list()
            selected_project_title.update()
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Text("删除课题将同时删除其论文记录，PDF 文件将移到回收站（7 天后自动清理）。"),
            actions=[ft.TextButton("取消", on_click=lambda e: (setattr(dlg, 'open', False), dlg.update())),
                     ft.FilledButton("确认删除", on_click=do_delete)],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    # ── 右侧：论文列表 ──
    _filter_options = [
        ("全部", "all"),
        ("未读", "unread"),
        ("略读", "skimmed"),
        ("精读", "deep_read"),
    ]
    _filter_chips: list[ft.Container] = []

    def _build_filter_chips():
        """构建状态筛选标签（避免 SegmentedButton 的 set 序列化问题）。"""
        chips = []
        for label, val in _filter_options:
            is_selected = _status_filter == val
            chip = ft.Container(
                content=ft.Text(label, size=FS_MD,
                               weight=FW_SEMIBOLD if is_selected else FW_REGULAR,
                               color=seed_color() if is_selected else text_secondary()),
                padding=ft.padding.Padding(left=SP_MD, right=SP_MD, top=6, bottom=6),
                border_radius=R_XL,
                bgcolor=accent_container() if is_selected else surface_hi(),
                border=_border(border_color()) if not is_selected else None,
                on_click=lambda e, v=val: on_status_filter_click(v),
                ink=True,
            )
            chips.append(chip)
        return chips

    def _refresh_filter_chips():
        """刷新筛选标签的高亮状态。"""
        nonlocal _filter_chips
        for i, (label, val) in enumerate(_filter_options):
            is_selected = _status_filter == val
            _filter_chips[i].bgcolor = accent_container() if is_selected else surface_hi()
            _filter_chips[i].border = _border(border_color()) if not is_selected else None
            _filter_chips[i].content.color = seed_color() if is_selected else text_secondary()
            _filter_chips[i].content.weight = FW_SEMIBOLD if is_selected else FW_REGULAR

    status_filter_row = ft.Row([], spacing=4)

    _library_list = ft.ListView(expand=True, spacing=0)

    # ── 分页 ──
    PAGE_SIZE = 100
    _pagination_page = 0
    _pagination_text = ft.Text("", size=13)
    _pagination_row = ft.Row(visible=False, spacing=8)

    empty_hint = ft.Text("", size=13, color=ft.Colors.OUTLINE)

    # ── 多选（始终可见）──
    _selected_ids: set[int] = set()
    _compare_btn = None  # 文献库"对比分析"按钮引用

    multi_select_bar = ft.Row(visible=True, spacing=8)
    multi_select_count = ft.Text("未选中", size=13)
    compare_btn = ft.OutlinedButton(
        content=ft.Text("对比分析"),
        icon=ft.Icons.COMPARE,
        tooltip="对比分析选中的论文（至少 2 篇）",
        visible=False,
        disabled=True,
        style=ft.ButtonStyle(padding=ft.padding.Padding(left=14, top=6, right=14, bottom=6)),
    )

    def _build_library_header():
        """构建文献库列表表头。"""
        def _hdr(label, width=None, expand=None):
            return ft.Container(
                content=ft.Text(label, size=FS_SM, weight=FW_SEMIBOLD,
                                color=text_secondary()),
                width=width, expand=expand,
                padding=ft.padding.Padding(left=SP_SM, right=SP_SM, top=10, bottom=10),
                bgcolor=surface_hi(),
                border=ft.border.Border(bottom=ft.BorderSide(1, border_color())),
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
            )
        cols = [_hdr("", width=38),   # 复选框占位
                _hdr("#", width=30),
                _hdr("标题", expand=3),
                _hdr("作者", expand=2),
                _hdr("年份", width=44),
                _hdr("AI", width=42),
                _hdr("CE", width=48),
                _hdr("状态", width=60),
                _hdr("操作", width=120)]
        return ft.Row(cols, spacing=0)

    def on_select_all(e):
        if e.control.value:
            _selected_ids.update(p["project_paper_id"] for p in _project_papers)
        else:
            _selected_ids.clear()
        refresh_paper_list()

    def on_check_one(e, pp_id: int):
        if e.control.value:
            _selected_ids.add(pp_id)
        else:
            _selected_ids.discard(pp_id)
        update_count()

    def update_count():
        nonlocal _compare_btn
        n = len(_selected_ids)
        multi_select_count.value = f"已选 {n} 篇" if n else "未选中"
        multi_select_count.update()
        if _compare_btn:
            _compare_btn.visible = (n >= 2)
            try:
                _compare_btn.update()
            except RuntimeError:
                pass
        # 同步到 Agent 自动上下文
        if n:
            ctx.agent_paper_selection = [p for p in _project_papers if p["project_paper_id"] in _selected_ids]
        else:
            ctx.agent_paper_selection.clear()

    def _clear_library():
        """Agent 发消息后清除文献库选中状态。"""
        _selected_ids.clear()
        update_count()
        refresh_paper_list()

    ctx.clear_library_ui = _clear_library

    def on_batch_delete(e):
        if not _selected_ids:
            return

        def do_delete(e):
            # 从 catalog 移除 + PDF 进回收站
            proj = library.get_project(_selected_project_id)
            if proj:
                for pp_id in _selected_ids:
                    match = next((p for p in _project_papers if p["project_paper_id"] == pp_id), None)
                    if match:
                        repo_manager.remove_paper_from_catalog(proj.name, match)
            n = library.remove_papers_from_project(list(_selected_ids))
            _selected_ids.clear()
            upload_progress.value = f"已删除 {n} 篇"
            upload_progress.color = ft.Colors.GREEN
            dlg.open = False
            dlg.update()
            refresh_paper_list()
            threading.Timer(3.0, lambda: setattr(upload_progress, "value", "") or upload_progress.update()).start()

        def close_dlg(e):
            dlg.open = False; dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Text(f"将删除选中的 {len(_selected_ids)} 篇论文，PDF 移到回收站（7 天后自动清理）。"),
            actions=[
                ft.TextButton("取消", on_click=close_dlg),
                ft.FilledButton("确认删除", on_click=do_delete),
            ],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    def _on_library_compare(e):
        """对比分析文献库选中的论文。"""
        if len(_selected_ids) < 2:
            return
        sel = [p for p in _project_papers if p["project_paper_id"] in _selected_ids]
        ctx.trigger_compare_papers(sel, source="library")

    compare_btn.on_click = _on_library_compare
    _compare_btn = compare_btn

    select_all_cb = ft.Checkbox(
        label="全选",
        on_change=on_select_all,
        visible=False,
    )
    _select_all_ref = select_all_cb

    multi_select_bar.controls = [
        _select_all_ref,
        multi_select_count,
        compare_btn,
        ft.FilledTonalButton(
            content=ft.Text("删除选中"), icon=ft.Icons.DELETE,
            on_click=on_batch_delete,
        ),
    ]

    # ── 上传 & 排序 ──
    upload_progress = ft.Text("", size=13)
    sort_btn = ft.IconButton(
        icon=ft.Icons.SORT,
        tooltip="CE 语义排序",
        disabled=True,
    )
    ai_sort_btn = ft.IconButton(
        icon=ft.Icons.ANALYTICS,
        tooltip="AI 精细打分",
        disabled=True,
    )
    sort_mode_text = ft.Text("", size=12, color=ft.Colors.OUTLINE, width=24, text_align=ft.TextAlign.CENTER)

    def _update_sort_mode_label():
        sort_mode_text.value = "CE" if _sort_mode == "ce" else "AI"
        try:
            sort_mode_text.update()
        except RuntimeError:
            pass

    def on_sort_click(e):
        """对当前课题所有论文跑 CE 精排并持久化分数。"""
        nonlocal _sort_mode
        if _selected_project_id is None:
            return
        proj = library.get_project(_selected_project_id)
        if not proj:
            return
        query = (proj.description or "").strip() or proj.name

        all_papers = library.get_project_papers(_selected_project_id)
        if not all_papers:
            upload_progress.value = "暂无论文可排序"
            upload_progress.color = ft.Colors.ERROR
            upload_progress.update()
            return

        # 有选中时仅处理选中的论文
        if _selected_ids:
            papers = [p for p in all_papers if p["project_paper_id"] in _selected_ids]
            if not papers:
                upload_progress.value = "未选中任何论文"
                upload_progress.color = ft.Colors.ERROR
                upload_progress.update()
                return
            label = f"已选 {len(papers)} 篇"
        else:
            papers = all_papers
            label = f"{len(papers)} 篇"

        upload_progress.value = f"正在语义排序 {label}..."
        upload_progress.color = ft.Colors.OUTLINE
        upload_progress.update()

        # 构建 paper dict 列表（含 pdf_path）
        paper_dicts = []
        for p in papers:
            d = {
                "title": p.get("title", ""),
                "authors": p.get("authors", ""),
                "abstract": p.get("abstract", ""),
                "year": p.get("year"),
                "source": p.get("source", "local_pdf"),
                "url": p.get("url"),
                "doi": p.get("doi"),
                "api_score": None,
                "type": None,
                "cited_by_count": None,
                "journal": None,
                "pdf_path": p.get("pdf_path"),
            }
            paper_dicts.append(d)

        import threading
        _sort_done = threading.Event()
        _sort_result: list = []

        def _run_sort():
            try:
                scored = rank_papers(
                    query=query,
                    papers=paper_dicts,
                    top_k=len(paper_dicts),
                    ce_candidates=len(paper_dicts),
                )
                _sort_result.extend(scored)
            except Exception as ex:
                _sort_result.append(ex)
            finally:
                _sort_done.set()

        threading.Thread(target=_run_sort, daemon=True).start()

        async def _poll_sort():
            import asyncio
            while not _sort_done.is_set():
                await asyncio.sleep(0.3)
            if _sort_result and isinstance(_sort_result[0], Exception):
                upload_progress.value = f"排序失败: {_sort_result[0]}"
                upload_progress.color = ft.Colors.ERROR
            else:
                n = library.update_paper_scores(_selected_project_id, _sort_result)
                upload_progress.value = f"排序完成，已更新 {n} 篇"
                upload_progress.color = ft.Colors.GREEN
                _sort_mode = "ce"
                _update_sort_mode_label()
            upload_progress.update()
            refresh_paper_list()
            unload_cross_encoder()

        ctx.page.run_task(_poll_sort)

    sort_btn.on_click = on_sort_click

    def on_ai_sort_click(e):
        """对当前课题论文跑 AI 精排。多选时仅排选中论文。"""
        nonlocal _sort_mode
        if _selected_project_id is None:
            return
        if not ctx.ai_service.is_available:
            upload_progress.value = "AI 排序失败：未配置 API Key"
            upload_progress.color = ft.Colors.ERROR
            upload_progress.update()
            return

        proj = library.get_project(_selected_project_id)
        if not proj:
            return
        topic_desc = (proj.description or "").strip() or proj.name

        all_papers = library.get_project_papers(_selected_project_id)
        if not all_papers:
            upload_progress.value = "暂无论文可排序"
            upload_progress.color = ft.Colors.ERROR
            upload_progress.update()
            return

        # 有选中时仅处理选中的论文
        if _selected_ids:
            papers = [p for p in all_papers if p["project_paper_id"] in _selected_ids]
            if not papers:
                upload_progress.value = "未选中任何论文"
                upload_progress.color = ft.Colors.ERROR
                upload_progress.update()
                return
            label = f"已选 {len(papers)} 篇"
        else:
            papers = all_papers
            label = f"全部 {len(papers)} 篇"

        upload_progress.value = f"AI 正在评估 {label}..."
        upload_progress.color = ft.Colors.OUTLINE
        upload_progress.update()

        paper_dicts = [{k: v for k, v in p.items()} for p in papers]

        import threading
        _ai_sort_done = threading.Event()
        _ai_sort_result: list = []

        def _run_ai_sort():
            try:
                result = ctx.ai_service.score_papers(topic_desc, paper_dicts)
                _ai_sort_result.extend(result)
            except Exception as ex:
                _ai_sort_result.append(ex)
            finally:
                _ai_sort_done.set()

        threading.Thread(target=_run_ai_sort, daemon=True).start()

        async def _poll_ai_sort():
            import asyncio
            while not _ai_sort_done.is_set():
                await asyncio.sleep(0.5)
            if _ai_sort_result and isinstance(_ai_sort_result[0], Exception):
                upload_progress.value = f"AI 排序失败: {_ai_sort_result[0]}"
                upload_progress.color = ft.Colors.ERROR
            elif not _ai_sort_result:
                upload_progress.value = "AI 评分失败：网络超时 / API 繁忙 / 返回格式异常，可重试"
                upload_progress.color = ft.Colors.ERROR
            else:
                n = library.update_paper_ai_scores(
                    _selected_project_id, _ai_sort_result, paper_dicts)
                upload_progress.value = f"AI 排序完成，已评分 {n} 篇"
                upload_progress.color = ft.Colors.GREEN
                _sort_mode = "ai"
                _update_sort_mode_label()
            upload_progress.update()
            refresh_paper_list()

        ctx.page.run_task(_poll_ai_sort)

    ai_sort_btn.on_click = on_ai_sort_click

    def _start_upload(file_paths: list[str]):
        """后台提取 PDF 并保存到课题。"""
        if not file_paths:
            return
        total = len(file_paths)
        print(f"[_start_upload] Starting: {total} file(s), project_id={_selected_project_id}", flush=True)
        for fp in file_paths[:5]:
            print(f"  - {fp}", flush=True)
        upload_progress.value = f"正在提取 0/{total}..."
        upload_progress.color = ft.Colors.OUTLINE
        upload_progress.update()

        import threading
        _upload_done = threading.Event()
        _upload_result: dict = {}
        _progress_info: dict = {"cur": 0, "fname": ""}

        def _run_extract():
            def progress_cb(cur, tot, fname):
                _progress_info["cur"] = cur
                _progress_info["fname"] = fname

            papers, skipped = extract_pdfs(file_paths, on_progress=progress_cb)

            # 使用 repo_manager：规范命名 + catalog 管理 + 跨课题同步
            proj = library.get_project(_selected_project_id)
            project_name = proj.name if proj else "未分类"
            for paper in papers:
                src = paper.get("pdf_path")
                if src and os.path.isfile(src):
                    new_path = repo_manager.import_pdf(paper, project_name)
                    if new_path:
                        paper["pdf_path"] = new_path

            _upload_result["papers"] = papers
            _upload_result["skipped"] = skipped
            print(f"[_start_upload] Extracted: {len(papers)} papers, {len(skipped)} skipped", flush=True)
            if skipped:
                for s in skipped[:5]:
                    print(f"  skipped: {s}", flush=True)
            _upload_done.set()

        threading.Thread(target=_run_extract, daemon=True).start()

        async def _poll_upload():
            import asyncio
            while not _upload_done.is_set():
                ci = _progress_info["cur"]
                fn = _progress_info["fname"]
                if ci:
                    upload_progress.value = f"正在提取 {ci}/{total}: {fn[:30]}"
                    upload_progress.update()
                await asyncio.sleep(0.3)

            papers = _upload_result.get("papers", [])
            skipped = _upload_result.get("skipped", [])

            n, pdf_upd = library.save_papers_to_project(_selected_project_id, papers)
            print(f"[_start_upload] Saved: {n} new, {pdf_upd} pdf updated to project {_selected_project_id}", flush=True)
            msg_parts = [f"已添加 {n} 篇"]
            if pdf_upd:
                msg_parts.append(f"已更新 {pdf_upd} 篇 PDF 路径")
            if skipped:
                msg_parts.append(f"跳过 {len(skipped)} 篇")
            upload_progress.value = "，".join(msg_parts)
            upload_progress.color = ft.Colors.GREEN
            upload_progress.update()
            refresh_paper_list()

        ctx.page.run_task(_poll_upload)

    # 文件选择 → PowerShell 调用 Windows 原生对话框
    def _run_ps_dialog(script: str) -> str:
        import subprocess, tempfile, os
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
        _FOCUS_HELPER = (
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
        )
        script = script.replace(
            '$owner=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}\n',
            _FOCUS_HELPER,
        )
        script = script.replace('$owner.Dispose()\n', '$owner.Close()\n$owner.Dispose()\n')
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
            if r.stderr:
                print(f"[_run_ps_dialog] stderr: {r.stderr[:200]}", flush=True)
            print(f"[_run_ps_dialog] rc={r.returncode} stdout='{r.stdout.strip()[:100]}'", flush=True)
            return r.stdout.strip()
        except Exception as ex:
            print(f"[_run_ps_dialog] error: {ex}", flush=True)
            return ""
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _pick_single_file():
        script = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            '$owner=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}\n'
            '$f=New-Object System.Windows.Forms.OpenFileDialog\n'
            "$f.Filter='PDF Files (*.pdf)|*.pdf'\n"
            "$f.Title='选择 PDF 文件'\n"
            "if($f.ShowDialog($owner) -eq 'OK'){Write-Output $f.FileName}\n"
            '$owner.Dispose()\n'
        )
        out = _run_ps_dialog(script)
        if out:
            _start_upload([out])

    def _pick_multiple_files():
        script = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            '$owner=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}\n'
            '$f=New-Object System.Windows.Forms.OpenFileDialog\n'
            "$f.Filter='PDF Files (*.pdf)|*.pdf'\n"
            "$f.Title='选择 PDF 文件'\n"
            '$f.Multiselect=$true\n'
            "if($f.ShowDialog($owner) -eq 'OK'){$f.FileNames|%{Write-Output $_}}\n"
            '$owner.Dispose()\n'
        )
        out = _run_ps_dialog(script)
        if out:
            _start_upload([p for p in out.split("\n") if p.strip()])

    def _pick_folder():
        script = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            '$owner=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}\n'
            '$f=New-Object System.Windows.Forms.FolderBrowserDialog\n'
            "$f.Description='选择包含 PDF 的文件夹'\n"
            "if($f.ShowDialog($owner) -eq 'OK'){Write-Output $f.SelectedPath}\n"
            '$owner.Dispose()\n'
        )
        out = _run_ps_dialog(script)
        if out:
            pdfs = scan_folder(out, recursive=True)
            if pdfs:
                _start_upload(pdfs)
            else:
                upload_progress.value = "所选文件夹中无 PDF 文件"
                upload_progress.color = ft.Colors.ERROR
                upload_progress.update()

    # upload 按钮 → PopupMenu 选择模式
    upload_menu_btn = ft.PopupMenuButton(
        icon=ft.Icons.UPLOAD_FILE,
        tooltip="上传本地论文",
        items=[
            ft.PopupMenuItem(
                content=ft.Text("选择单个文件"),
                on_click=lambda e: _pick_single_file(),
            ),
            ft.PopupMenuItem(
                content=ft.Text("选择多个文件"),
                on_click=lambda e: _pick_multiple_files(),
            ),
            ft.PopupMenuItem(
                content=ft.Text("选择文件夹"),
                on_click=lambda e: _pick_folder(),
            ),
        ],
    )

    def _on_single_delete(e, pp_id):
        """删除单篇论文的确认对话框。"""
        def do_delete(e):
            # 从 catalog 移除 + PDF 进回收站
            proj = library.get_project(_selected_project_id)
            if proj:
                match = next((p for p in _project_papers if p["project_paper_id"] == pp_id), None)
                if match:
                    repo_manager.remove_paper_from_catalog(proj.name, match)
            library.remove_paper_from_project(pp_id)
            dlg.open = False
            dlg.update()
            refresh_paper_list()

        def close_dlg(e):
            dlg.open = False; dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Text("将删除这篇论文，PDF 移到回收站（7 天后自动清理）。"),
            actions=[
                ft.TextButton("取消", on_click=close_dlg),
                ft.FilledButton("确认删除", on_click=do_delete),
            ],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    def refresh_paper_list(target_pid=None):
        """从数据库刷新当前课题的论文列表（分页 + 精简控件）。"""
        nonlocal _pagination_page, _sort_mode
        if target_pid is not None and target_pid != _selected_project_id:
            return  # 回调来自其他课题，忽略
        ctx.refresh_paper_list = refresh_paper_list
        status_val = _status_filter
        sf = None if status_val == "all" else status_val
        papers = library.get_project_papers(_selected_project_id, status_filter=sf) if _selected_project_id else []

        # 根据排序模式排序
        if _sort_mode == "ai":
            def _ai_sort_key(p):
                ai = p.get("ai_score")
                if ai is not None:
                    return (0, -ai, 0)
                else:
                    return (1, 0, -(p.get("total_score", 0) or 0))
            papers.sort(key=_ai_sort_key)
        else:
            papers.sort(key=lambda p: p.get("total_score", 0) or 0, reverse=True)

        _project_papers[:] = papers

        if not papers:
            _library_list.controls = [_build_library_header()]
            _pagination_row.visible = False
            empty_hint.value = "此课题暂无保存的论文，请在检索页保存结果到此课题"
            empty_hint.update()
            _pagination_row.update()
            _library_list.update()
            return

        empty_hint.value = ""

        total_pages = max(1, (len(papers) + PAGE_SIZE - 1) // PAGE_SIZE)
        if _pagination_page >= total_pages:
            _pagination_page = total_pages - 1
        start = _pagination_page * PAGE_SIZE
        end = min(start + PAGE_SIZE, len(papers))
        page_papers = papers[start:end]

        status_colors = {"unread": ft.Colors.OUTLINE, "skimmed": ft.Colors.AMBER, "deep_read": ft.Colors.GREEN}
        rows = [_build_library_header()]
        _ai_available = ctx.ai_service.is_available

        row_pad = ft.padding.Padding(left=4, top=4, right=4, bottom=4)
        _next_status_map = {"unread": "skimmed", "skimmed": "deep_read", "deep_read": "unread"}

        for i, p in enumerate(page_papers):
            global_i = start + i + 1  # 1-based across all pages
            title = (p.get("title") or "")[:60]
            authors = (p.get("authors") or "")[:30]
            year = str(p.get("year") or "—")
            ce_score = p.get("total_score", 0)
            ai_score_val = p.get("ai_score")
            status = p.get("status", "unread")
            pp_id = p["project_paper_id"]

            # AI 分颜色（按档位）
            if ai_score_val is not None:
                ai_s = int(ai_score_val)
                if ai_s >= 85:
                    ai_color = ft.Colors.GREEN
                elif ai_s >= 70:
                    ai_color = ft.Colors.BLUE
                elif ai_s >= 55:
                    ai_color = ft.Colors.AMBER
                elif ai_s >= 40:
                    ai_color = ft.Colors.ORANGE
                else:
                    ai_color = ft.Colors.RED
            else:
                ai_color = ft.Colors.OUTLINE
            # CE 分颜色
            ce_score_val = ce_score or 0
            if ce_score_val >= 0.4:
                ce_color = ft.Colors.GREEN
            elif ce_score_val >= 0.2:
                ce_color = ft.Colors.ORANGE
            else:
                ce_color = ft.Colors.OUTLINE

            status_color = status_colors.get(status, ft.Colors.OUTLINE)

            # 阅读按钮
            read_btn = ft.IconButton(
                icon=ft.Icons.OPEN_IN_BROWSER,
                tooltip="打开全文",
                on_click=lambda e, paper=p: _on_read_paper(paper),
                icon_size=16,
            )
            if not is_full_reader_available():
                read_btn.disabled = True

            # 状态指示灯
            dot = ft.Container(
                width=12, height=12, border_radius=6, bgcolor=status_color,
                tooltip=f"状态: {status}（点击切换）",
                on_click=lambda e, ppid=pp_id, cur=status: _on_status_change(ppid, _next_status_map[cur]),
            )

            # AI 精读按钮
            deep_read_btn = ft.IconButton(
                icon=ft.Icons.PSYCHOLOGY,
                tooltip="AI 精读分析",
                icon_size=16,
                on_click=lambda e, p=p: _on_deep_read(e, p),
            )
            if not _ai_available:
                deep_read_btn.disabled = True
                deep_read_btn.tooltip = "AI 精读（未配置 API Key）"

            # 删除按钮
            delete_btn = ft.IconButton(
                icon=ft.Icons.DELETE,
                tooltip="删除",
                icon_size=16,
                on_click=lambda e, pid=pp_id: _on_single_delete(e, pid),
            )

            # 状态指示灯 + 提升按钮
            status_cell_parts = [dot]
            if status == "skimmed":
                promote_btn = ft.IconButton(
                    icon=ft.Icons.STAR,
                    tooltip="标为精读",
                    icon_size=14,
                    on_click=lambda e, ppid=pp_id: _on_status_change(ppid, "deep_read"),
                )
                status_cell_parts.append(promote_btn)

            def _show_detail_dialog(paper: dict):
                """弹出论文详情对话框，显示完整元数据。"""
                ptitle = paper.get("title", "无标题") or "无标题"
                pauthors = paper.get("authors", "未知") or "未知"
                pyear = str(paper.get("year") or "—")
                psource = paper.get("source", "未知") or "未知"
                pdoi = paper.get("doi", "") or ""
                purl = paper.get("url", "") or ""
                pabstract = paper.get("abstract", "") or "（无摘要）"
                pscore = paper.get("total_score", 0)
                pstatus = paper.get("status", "unread")
                pstatus_label = {"unread": "未读", "skimmed": "略读", "deep_read": "精读"}.get(pstatus, pstatus)
                pai_notes = paper.get("ai_notes", "") or ""
                puser_notes = paper.get("user_notes", "") or ""

                cparts = [
                    ft.Text(f"作者: {pauthors}", size=13),
                    ft.Text(f"年份: {pyear}  |  来源: {psource}  |  状态: {pstatus_label}", size=13),
                ]
                if pdoi:
                    import webbrowser
                    cparts.append(ft.Row([
                        ft.Text("DOI: ", size=13, color=ft.Colors.OUTLINE),
                        ft.TextButton(
                            content=ft.Text(pdoi, size=13),
                            on_click=lambda e, d=pdoi: webbrowser.open(f"https://doi.org/{d}"),
                            style=ft.ButtonStyle(padding=ft.padding.Padding.all(0)),
                        ),
                    ], spacing=0, wrap=True))
                if purl:
                    cparts.append(ft.Text(f"URL: {purl[:120]}", size=13, color=ft.Colors.OUTLINE))
                cparts.append(ft.Text(f"CE 得分: {pscore:.3f}", size=13, weight=ft.FontWeight.W_600))
                pai_score = paper.get("ai_score")
                if pai_score is not None:
                    import json as _json2
                    pai_reason_str = paper.get("ai_reason") or ""
                    try:
                        pai_reason = _json2.loads(pai_reason_str)
                        tier = str(pai_reason.get("tier", ""))
                    except (_json2.JSONDecodeError, TypeError):
                        pai_reason = {}
                        tier = ""
                    tier_badge = f" [{tier}]" if tier else ""
                    cparts.append(ft.Text(f"AI 评分: {int(pai_score)}{tier_badge}", size=13, weight=ft.FontWeight.W_600, color=ft.Colors.GREEN))
                    # 展示各维度理由
                    dims = [
                        ("相关性", pai_reason.get("relevance"), pai_reason.get("reason_relevance", "")),
                        ("方法", pai_reason.get("method"), pai_reason.get("reason_method", "")),
                        ("创新", pai_reason.get("novelty"), pai_reason.get("reason_novelty", "")),
                        ("时效", pai_reason.get("recency"), pai_reason.get("reason_recency", "")),
                    ]
                    for label, score_val, reason_text in dims:
                        if reason_text:
                            score_str = f"{int(score_val)}/10" if score_val is not None else ""
                            cparts.append(ft.Text(
                                f"  {label} {score_str}: {reason_text}",
                                size=13, color=ft.Colors.OUTLINE,
                            ))
                    overall = pai_reason.get("overall", "")
                    if overall:
                        cparts.append(ft.Text(
                            f"  综合: {overall}", size=13,
                            color=ft.Colors.OUTLINE, weight=ft.FontWeight.W_500,
                        ))
                cparts.append(ft.Divider(height=8))
                cparts.append(ft.Text("摘要", size=14, weight=ft.FontWeight.W_600))
                cparts.append(ft.Text(pabstract, size=13))
                if pai_notes:
                    cparts.append(ft.Divider(height=8))
                    cparts.append(ft.Text("AI 精读笔记", size=14, weight=ft.FontWeight.W_600))
                    try:
                        import json as _json
                        parsed = _json.loads(pai_notes)
                        if isinstance(parsed, dict):
                            for k, v in parsed.items():
                                if k.startswith("_"):
                                    continue
                                if isinstance(v, dict):
                                    scores_str = "  ".join(f"{sk}: {sv}" for sk, sv in v.items())
                                    cparts.append(ft.Text(f"{k}: {scores_str}", size=13))
                                else:
                                    cparts.append(ft.Text(f"{k}: {v}", size=13))
                        else:
                            cparts.append(ft.Text(pai_notes[:500], size=13))
                    except Exception:
                        cparts.append(ft.Text(pai_notes[:500], size=13))
                if puser_notes:
                    cparts.append(ft.Divider(height=8))
                    cparts.append(ft.Text("用户批注", size=14, weight=ft.FontWeight.W_600))
                    cparts.append(ft.Text(puser_notes, size=13))

                def close_dlg(e):
                    dlg.open = False
                    dlg.update()

                def read_paper_and_close(e):
                    close_dlg(e)
                    _on_read_paper(paper)

                dlg = ft.AlertDialog(
                    title=ft.Text(ptitle, size=16, weight=ft.FontWeight.W_600, max_lines=4),
                    content=ft.Column(cparts, spacing=8, scroll=ft.ScrollMode.AUTO, height=480, width=560),
                    actions=[
                        ft.TextButton("阅读原文", on_click=read_paper_and_close),
                        ft.TextButton("关闭", on_click=close_dlg),
                    ],
                )
                ctx.page.overlay.append(dlg)
                dlg.open = True
                ctx.page.update()

            # 已下载论文序号+标题变绿
            pdf_path = p.get("pdf_path", "")
            has_pdf = bool(pdf_path and os.path.isfile(str(pdf_path)))
            title_color = ft.Colors.GREEN if has_pdf else None

            # ── AI 评分 reasons tooltip ──
            _ai_tooltip = None
            if ai_score_val is not None:
                _reason_str = p.get("ai_reason") or ""
                try:
                    import json as _json3
                    _reason = _json3.loads(_reason_str)
                    _tt_lines = []
                    if _reason.get("reason_relevance"):
                        _tt_lines.append(f"相关性：{_reason['reason_relevance']}")
                    if _reason.get("reason_method"):
                        _tt_lines.append(f"方法：{_reason['reason_method']}")
                    if _reason.get("reason_novelty"):
                        _tt_lines.append(f"创新：{_reason['reason_novelty']}")
                    if _reason.get("reason_recency"):
                        _tt_lines.append(f"时效：{_reason['reason_recency']}")
                    if _reason.get("overall"):
                        _tt_lines.append(f"总评：{_reason['overall']}")
                    if _tt_lines:
                        _ai_tooltip = "\n".join(_tt_lines)
                except Exception:
                    pass

            # ── 精简行布局（每行比原来少 ~5 个 Container）──
            cells = [
                ft.Text(str(global_i), size=13, width=30, color=title_color,
                        weight=ft.FontWeight.W_600 if has_pdf else ft.FontWeight.W_400),
                ft.Container(
                    content=ft.Text(title, size=13, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    color=title_color),
                    expand=3, padding=ft.padding.Padding(right=4),
                    on_click=lambda e, p=p: _show_detail_dialog(p),
                ),
                ft.Text(authors[:28], size=13, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS, expand=2),
                ft.Text(year, size=13, width=44),
                ft.Text(
                    str(int(ai_score_val)) if ai_score_val is not None else "—",
                    size=13, color=ai_color,
                    weight=ft.FontWeight.W_600, width=42,
                    tooltip=_ai_tooltip),
                ft.Text(
                    f"{ce_score_val:.3f}",
                    size=13, color=ce_color, width=48),
                ft.Row(status_cell_parts, spacing=4, width=60),
                ft.Row([read_btn, deep_read_btn, delete_btn], spacing=0, width=120),
            ]

            is_checked = pp_id in _selected_ids
            cb = ft.Checkbox(
                value=is_checked,
                on_change=lambda e, pid=pp_id: on_check_one(e, pid),
                scale=0.85,
            )
            cells.insert(0, cb)

            row = ft.Container(
                content=ft.Row(cells, spacing=0),
                border=ft.border.Border(
                    bottom=ft.BorderSide(1, border_color())),
                padding=ft.padding.Padding(left=SP_SM, top=SP_XS, right=SP_SM, bottom=SP_XS),
            )
            rows.append(row)

        # 更新全选复选框状态
        select_all_cb.value = (len(_selected_ids) == len(papers) and len(papers) > 0)
        select_all_cb.visible = True
        update_count()

        paper_count_text.value = f"共 {len(papers)} 篇论文" if papers else ""
        paper_count_text.update()
        _library_list.controls = rows
        _library_list.update()

        # ── 分页控件 ──
        _pagination_text.value = f"第 {_pagination_page + 1}/{total_pages} 页（本页 {end - start} 篇）"

        def _build_page_btn(label, target_page, enabled):
            return ft.TextButton(
                content=ft.Text(label, size=13),
                disabled=not enabled,
                on_click=lambda e, pg=target_page: _go_to_page(pg),
            )

        prev_btn = _build_page_btn("上一页", _pagination_page - 1, _pagination_page > 0)
        next_btn = _build_page_btn("下一页", _pagination_page + 1, _pagination_page < total_pages - 1)
        _pagination_row.controls = [prev_btn, _pagination_text, next_btn]
        _pagination_row.visible = total_pages > 1
        _pagination_row.update()

    def _go_to_page(page: int):
        nonlocal _pagination_page
        _pagination_page = page
        refresh_paper_list()

    def on_select_project(project_id: int | None):
        """选中课题时刷新论文列表。"""
        nonlocal _selected_project_id, _pagination_page
        _selected_project_id = project_id
        ctx.selected_project_id = project_id
        _pagination_page = 0
        upload_progress.value = ""
        if project_id is None:
            selected_project_title.value = "请选择一个课题"
            paper_count_text.value = ""
            sort_btn.disabled = True
            ai_sort_btn.disabled = True
            ctx.set_agent_project(None)
        else:
            proj = library.get_project(project_id)
            if proj:
                selected_project_title.value = proj.name
                sort_btn.disabled = False
                sort_btn.tooltip = "CE 语义排序"
                ai_sort_btn.disabled = not ctx.ai_service.is_available
                ctx.set_agent_project(project_id, proj.name, proj.description or "")
            else:
                sort_btn.disabled = True
                ai_sort_btn.disabled = True
        selected_project_title.update()
        refresh_project_list()
        refresh_paper_list()

    def _on_read_paper(paper: dict):
        """打开 PDF 阅读器；无 PDF 时弹 Flet 原生提示框。"""
        title = (paper.get("title") or "论文")[:80]
        doi = paper.get("doi", "") or ""
        print(f"[_on_read_paper] called: {title}, pdf_path={paper.get('pdf_path')}, doi={doi}", flush=True)

        _done = threading.Event()
        _success = False
        _error_msg: str | None = None

        def _run():
            nonlocal _success, _error_msg
            try:
                _success = open_full_reader(
                    paper,
                    theme_seed=THEMES[state.theme_name]["seed"],
                    dark_mode=state.dark_mode,
                )
                print(f"[_on_read_paper] open_full_reader returned: {_success}", flush=True)
            except Exception as ex:
                _error_msg = str(ex)
                print(f"[_on_read_paper] ERROR: {ex}", flush=True)
            finally:
                _done.set()

        threading.Thread(target=_run, daemon=True).start()

        async def _poll():
            import asyncio
            while not _done.is_set():
                await asyncio.sleep(0.3)

            if _success:
                new_path = paper.get("pdf_path")
                # 将 PDF 从缓存同步到课题仓库
                if _selected_project_id is not None and new_path and os.path.isfile(str(new_path)):
                    proj = library.get_project(_selected_project_id)
                    if proj:
                        repo_path = repo_manager.import_pdf(paper, proj.name)
                        if repo_path:
                            paper["pdf_path"] = repo_path
                            new_path = repo_path
                # 持久化 pdf_path
                if new_path and paper.get("doi"):
                    library.set_paper_pdf_path(doi=paper["doi"], pdf_path=new_path)
                return

            # 无 PDF/HTML → 弹 Flet 原生提示框
            content_parts = [
                ft.Text("抱歉，暂时无法获取本文 PDF。", size=14),
                ft.Text("该论文无法通过直链下载，也不在 arXiv 上。", size=13,
                       color=ft.Colors.OUTLINE),
                ft.Text("请尝试手动下载 PDF 后，通过「导入 PDF」添加到文献库。", size=13),
            ]
            if _error_msg:
                content_parts.append(ft.Text(f"调试信息: {_error_msg}", size=12,
                                   color=ft.Colors.ERROR))
            if doi:
                import webbrowser
                content_parts.append(ft.Divider(height=8))
                content_parts.append(ft.Row([
                    ft.Text("DOI: ", size=13, color=ft.Colors.OUTLINE),
                    ft.TextButton(
                        content=ft.Text(doi, size=13),
                        on_click=lambda e, d=doi: webbrowser.open(f"https://doi.org/{d}"),
                        style=ft.ButtonStyle(padding=ft.padding.Padding.all(0)),
                    ),
                ], spacing=0))

            def close_dlg(e):
                dlg.open = False
                dlg.update()

            dlg = ft.AlertDialog(
                title=ft.Text("无法获取全文", size=15, weight=ft.FontWeight.W_600),
                content=ft.Column(content_parts, spacing=8, tight=True),
                actions=[ft.TextButton("关闭", on_click=close_dlg)],
            )
            ctx.page.overlay.append(dlg)
            dlg.open = True
            ctx.page.update()

        ctx.page.run_task(_poll)

    def _on_deep_read(e, paper: dict):
        """后台精读论文：获取全文 → RLM 分析 → 展示结果。"""
        title = (paper.get("title") or "论文")[:40]
        pp_id = paper.get("project_paper_id")

        def _save_msg(role: str, text: str):
            """将消息写入当前课题的对话记录。"""
            if ctx.agent_project_id is not None:
                ctx.ai_service.log_message(
                    ctx.agent_project_id, ctx.agent_project_name,
                    role, text, ctx.agent_topic_desc)

        ctx.send_agent_message(f"正在精读：《{title}》...\n\n正在获取全文，请稍候 🔍", role="agent")
        _save_msg("user", f"[精读请求] 请精读论文：《{title}》")

        _done = threading.Event()
        _result: dict = {}
        _error: str | None = None
        _status: str = ""  # 中间状态消息，由主线程轮询时展示

        def _run():
            nonlocal _error, _status
            try:
                full_text, source = get_full_text_for_paper(paper)
                if not full_text:
                    _error = f"无法获取《{title}》的全文。\n\n请先导入 PDF 或确保论文有可访问的 arXiv 链接。"
                    _done.set()
                    return

                _status = f"已获取全文（{len(full_text)} 字符，来源: {source}）\n正在 RLM 分层分析... 📖"

                result = ctx.ai_service.deep_read(paper, full_text)
                if not result:
                    _error = f"精读《{title}》失败，请检查 API Key 和网络连接。"
                    _done.set()
                    return

                _result.update(result)

                # 保存到数据库
                if pp_id:
                    import json as _json
                    try:
                        save_deep_read_notes(pp_id, _json.dumps(result, ensure_ascii=False))
                        # 首次 AI 精读后自动从未读 → 略读
                        if paper.get("status") == "unread":
                            library.update_paper_status(pp_id, "skimmed")
                    except Exception:
                        pass

                # 保存到本地 JSON
                save_deep_read_json(paper, result)

            except Exception as ex:
                _error = f"精读异常: {ex}"
            finally:
                _done.set()

        threading.Thread(target=_run, daemon=True).start()

        async def _poll():
            import asyncio
            last_status = ""
            while not _done.is_set():
                await asyncio.sleep(0.3)
                # 主线程安全地展示中间状态消息
                if _status and _status != last_status:
                    last_status = _status
                    ctx.send_agent_message(_status, role="agent")

            if _error:
                ctx.send_agent_message(f"精读失败：{_error}", role="agent")
                _save_msg("assistant", f"精读失败：{_error}")
                return

            # 刷新文献列表，使状态变化（unread→skimmed）立即反映到 UI
            refresh_paper_list()

            r = _result
            if r.get("_truncated"):
                trunc_msg = (
                    f"无法精读：《{title}》\n\n"
                    f"该论文在 HTML 源中仅含摘要，正文无法获取。\n\n"
                    f"建议：下载 PDF 文件后导入到文献库，再重新精读。\n"
                    f"操作：点击论文旁的 📥 按钮 → 选择 PDF 文件 → 导入成功后再点 📖"
                )
                ctx.send_agent_message(trunc_msg, role="agent")
                _save_msg("assistant", trunc_msg)
                return

            r = _result
            scores = r.get("scores", {})
            score_line = (
                f"新颖性 {scores.get('novelty', '?')}/10  |  "
                f"严谨性 {scores.get('rigor', '?')}/10  |  "
                f"重要性 {scores.get('significance', '?')}/10"
            )

            msg = (
                f"📖 精读分析：《{title}》\n\n"
                f"🔑 核心贡献\n{r.get('core_contribution', '—')}\n\n"
                f"🔬 研究方法\n{r.get('method', '—')}\n\n"
                f"📊 关键证据\n{r.get('key_evidence', '—')}\n\n"
                f"💡 创新亮点\n{r.get('highlights', '—')}\n\n"
                f"⚠️ 局限不足\n{r.get('limitations', '—')}\n\n"
                f"📈 {score_line}\n\n"
                f"（完整结果已保存到本地 outputs/deep_read/）"
            )
            ctx.send_agent_message(msg, role="agent")
            _save_msg("assistant", msg)

        ctx.page.run_task(_poll)

    def _on_status_change(pp_id: int, new_status: str):
        """更新论文状态并刷新列表。"""
        library.update_paper_status(pp_id, new_status)
        refresh_paper_list()

    # 状态筛选回调
    def on_status_filter_click(value: str):
        nonlocal _status_filter
        _status_filter = value
        _refresh_filter_chips()
        refresh_paper_list()
        status_filter_row.update()

    # ── 导出 ──
    def _get_export_papers() -> list[dict]:
        """获取待导出的论文列表（有选中时取选中，否则取全部）。"""
        if _selected_ids:
            return [p for p in _project_papers if p["project_paper_id"] in _selected_ids]
        return _project_papers

    def _do_export(ext: str, label: str, convert):
        papers = _get_export_papers()
        if not papers:
            return
        content = convert(papers)

        # 默认文件名
        proj_name = ""
        if _selected_project_id is not None:
            proj = library.get_project(_selected_project_id)
            if proj:
                import re
                proj_name = "_" + re.sub(r"[^\w\s\-]", "", proj.name)[:30]
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"PaperPilot{proj_name}_{ts}.{ext}"

        result = {"path": None, "done": False}

        def _bg_dialog():
            try:
                import subprocess, tempfile, os as _os
                try:
                    import ctypes
                    ctypes.windll.user32.AllowSetForegroundWindow(-1)
                except Exception:
                    pass
                _label = label.replace("'", "''")
                _defname = default_name.replace("'", "''")
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
                    '$f=New-Object System.Windows.Forms.SaveFileDialog\n'
                    f"$f.Title='导出为 {_label}'\n"
                    f"$f.DefaultExt='.{ext}'\n"
                    f"$f.FileName='{_defname}'\n"
                    f"$f.Filter='{_label} 文件 (*.{ext})|*.{ext}'\n"
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
                    if r.stderr:
                        print(f"[_bg_dialog] ps stderr: {r.stderr[:200]}", flush=True)
                    selected = r.stdout.strip()
                    if selected:
                        result["path"] = selected
                finally:
                    try:
                        _os.unlink(tmp.name)
                    except OSError:
                        pass
            except Exception as ex:
                print(f"[_bg_dialog] error: {ex}", flush=True)
            result["done"] = True

        import threading as _th
        _th.Thread(target=_bg_dialog, daemon=True).start()

        async def _poll():
            import asyncio
            while not result["done"]:
                await asyncio.sleep(0.2)

            path = result["path"]
            if path:
                try:
                    from paperpilot.export import save_file
                    save_file(content, path)
                    _show_export_done(label, path)
                except OSError as ex:
                    _show_export_error(str(ex))

        ctx.page.run_task(_poll)

    def on_export_bibtex(e):
        try:
            from paperpilot.export import to_bibtex
        except ImportError:
            _show_export_unavailable()
            return
        _do_export("bib", "BibTeX", to_bibtex)

    def on_export_csv(e):
        try:
            from paperpilot.export import to_csv
        except ImportError:
            _show_export_unavailable()
            return
        _do_export("csv", "CSV", to_csv)

    def _show_export_done(fmt: str, path: str):
        dlg = ft.AlertDialog(
            title=ft.Text("导出完成"),
            content=ft.Column([
                ft.Text(f"已导出为 {fmt} 格式"),
                ft.Text(path, size=12, color=ft.Colors.OUTLINE,
                       font_family="Consolas"),
            ], spacing=8, tight=True),
            actions=[ft.TextButton("确定", on_click=lambda e: _close_dlg(dlg))],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    def _show_export_error(msg: str):
        dlg = ft.AlertDialog(
            title=ft.Text("导出失败"),
            content=ft.Text(msg, size=13),
            actions=[ft.TextButton("确定", on_click=lambda e: _close_dlg(dlg))],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    def _close_dlg(dlg):
        dlg.open = False
        dlg.update()

    def _show_export_unavailable():
        dlg = ft.AlertDialog(
            title=ft.Text("导出功能暂不可用"),
            content=ft.Text("导出模块尚未完成，请等待后续更新。"),
            actions=[ft.TextButton("确定", on_click=lambda e: _close_dlg(dlg))],
        )
        ctx.page.overlay.append(dlg)
        dlg.open = True
        ctx.page.update()

    # ── 注册课题能力回调（供左侧导航省略号菜单 / 子菜单调用）──
    ctx.library_select_project = on_select_project
    ctx.library_new_project = lambda: on_new_project(None)
    ctx.library_delete_project = lambda: on_delete_project(None)
    ctx.library_refresh_projects = lambda: (refresh_project_list(), refresh_paper_list())

    # 首次加载课题列表（填充侧栏子菜单）
    refresh_project_list()

    # 初始化筛选标签
    _filter_chips[:] = _build_filter_chips()
    status_filter_row.controls[:] = _filter_chips

    # ── 布局：单卡片文献内容区（课题列表已移至左侧导航子菜单）──
    right_panel = card(
        ft.Column([
            ft.Row([
                ft.Row([
                    selected_project_title,
                    ft.PopupMenuButton(
                        icon=ft.Icons.MORE_VERT,
                        tooltip="课题操作",
                        items=[
                            ft.PopupMenuItem(
                                content=ft.Text("编辑课题", size=13),
                                on_click=on_edit_project,
                            ),
                            ft.PopupMenuItem(
                                content=ft.Text("删除课题", size=13),
                                on_click=on_delete_project,
                            ),
                        ],
                    ),
                ], expand=True, spacing=0),
                ft.Row([
                    upload_menu_btn,
                    sort_btn,
                    ai_sort_btn,
                    sort_mode_text,
                    ft.PopupMenuButton(
                        icon=ft.Icons.DOWNLOAD,
                        tooltip="导出",
                        items=[
                            ft.PopupMenuItem(content=ft.Text("BibTeX"), on_click=on_export_bibtex),
                            ft.PopupMenuItem(content=ft.Text("CSV"), on_click=on_export_csv),
                        ],
                    ),
                ], spacing=2),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            paper_count_text,
            upload_progress,
            multi_select_bar,
            ft.Row([
                ft.Text("筛选:", size=FS_MD, color=text_secondary()),
                status_filter_row,
            ], spacing=SP_SM, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(height=1, color=border_color()),
            empty_hint,
            _pagination_row,
            _library_list,
        ], spacing=SP_SM, expand=True),
        expand=True,
    )

    return right_panel
