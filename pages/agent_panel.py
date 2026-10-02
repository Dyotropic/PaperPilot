"""StudyCopilot Agent 对话面板（全局常驻右侧）。

从 app.py 迁入（PHASE3_PLAN §6.4 拆分）：气泡渲染、思考动画、对话触发、
ACTION/PROJECT_UPDATE 解析与 dispatch、课题更新确认框、面板构建与拖拽调宽。

模块级状态（消息列表/输入框/课题上下文/思考动画标志）整组驻留本模块，
app.py 经再导出提供冻结接口 send_agent_message / set_agent_project。
本模块只依赖 pages.context（ctx）与 paperpilot 后端包，不反向 import app.py；
切页经 ctx.switch_page（app.py 注入）。
"""
import asyncio
import json
import logging
import os
import re
import threading
import time

import flet as ft

from pages.context import (
    ctx,
    FONT_FAMILY, FS_XS, FS_SM, FS_MD, FS_LG, FS_XL, FS_XXL, FS_HERO,
    FW_REGULAR, FW_MEDIUM, FW_SEMIBOLD, FW_BOLD,
    R_SM, R_MD, R_LG, R_XL, SP_XS, SP_SM, SP_MD, SP_LG, SP_XL, SP_XXL,
    text_primary, text_secondary, text_tertiary, border_color,
    seed_color, app_bg, surface, surface_hi,
)
from pages.components import clamp_width, make_resize_handle, open_dialog, close_dialog
from pages.agent_attachments_ui import AttachmentComposer, history_attachments
from pages.agent_team_ui import AgentTeamView
from paperpilot.agent_attachments import (persist_attachments,
    format_attachment_material, ensure_image_support)

logger = logging.getLogger(__name__)

state = ctx.state

from paperpilot.ai_service import AIService
from paperpilot import library
from paperpilot import repo_manager, downloader
from paperpilot.keywords import extract_all_keywords
from paperpilot.llm_usage import UsageStore
from paperpilot.agent_runtime import AgentRun, OperationCancelled, run_scope, checkpoint

# ── 模块级状态 ──
_agent_msg_list: ft.ListView | None = None
_agent_input: ft.TextField | None = None
_ai_service = AIService()  # LLM API 封装实例
ctx.ai_service = _ai_service
_agent_project_id: int | None = None
_agent_project_name: str = ""
_agent_topic_desc: str = ""
_agent_session_id: str | None = None
_session_selector: ft.Dropdown | None = None
_usage_text: ft.Text | None = None
_context_text: ft.Text | None = None
_context_bar: ft.ProgressBar | None = None
_command_menu: ft.Container | None = None
_command_rows: dict = {}
_slash_dismissed = False
_slash_index = 0
_thinking_active: bool = False
_active_run: AgentRun | None = None
_agent_send_button: ft.IconButton | None = None
_attachment_composer: AttachmentComposer | None = None
_team_view: AgentTeamView | None = None


def _refresh_run_button():
    if _agent_send_button is None:
        return
    busy = _active_run is not None
    stopping = busy and _active_run.token.cancelled
    _agent_send_button.icon = ft.Icons.STOP if busy else ft.Icons.ARROW_UPWARD
    _agent_send_button.tooltip = ("正在停止，等待当前步骤退出" if stopping else
                                   "停止当前轮（保留记录）" if busy else "发送消息（Enter）")
    _agent_send_button.disabled = bool(stopping or (not busy and _attachment_composer and _attachment_composer.loading))
    if _attachment_composer:
        _attachment_composer.refresh()
    try:
        _agent_send_button.update()
    except RuntimeError:
        pass


def begin_agent_run(cm, pid, goal, operation="chat", on_partial=None, *, attachments=None):
    """Shared owner for chat and library tasks; finish only after all child jobs."""
    global _active_run, _thinking_active
    if _active_run is not None:
        return None
    def done(run, note):
        global _active_run, _thinking_active
        if _active_run is not run:
            return
        _active_run = None
        _thinking_active = False
        if note and not run.maintenance and run.identity == (_agent_project_id or 0, _agent_session_id):
            send_agent_message(note, role="agent")
        _refresh_run_button()
        refresh_agent_usage()
        _refresh_command_menu()
    _active_run = AgentRun(cm, pid, goal, operation, on_done=done, on_partial=on_partial, attachments=attachments)
    _thinking_active = True
    _refresh_run_button()
    _refresh_command_menu()
    return _active_run


def stop_agent_run(e=None):
    run = _active_run
    if run:
        run.stop()
        _refresh_run_button()

# ── Agent 面板拖拽拉伸 ──
_AGENT_PANEL_MIN = 320
_AGENT_PANEL_MAX_RATIO = 0.5   # 运行时上限：窗宽的 50%
_AGENT_PANEL_DEFAULT = 380
_AGENT_PANEL_ABS_MAX = 900     # 配置值钳制上限（运行时上限仍随窗宽）
_agent_panel_width = clamp_width(
    380, _AGENT_PANEL_MIN, _AGENT_PANEL_ABS_MAX, _AGENT_PANEL_DEFAULT)
_agent_panel_ref: ft.Container | None = None


def _load_saved_agent_width() -> None:
    """从 config 恢复上次拖拽保存的面板宽度（缺失/非法回退默认）。"""
    global _agent_panel_width
    try:
        from paperpilot.config import load_config
        saved = (load_config().get("ui", {}) or {}).get(
            "agent_panel_width", _AGENT_PANEL_DEFAULT)
        _agent_panel_width = clamp_width(
            saved, _AGENT_PANEL_MIN, _AGENT_PANEL_ABS_MAX, _AGENT_PANEL_DEFAULT)
    except Exception:
        pass


def _set_agent_panel_width(w: int) -> None:
    """写入面板宽度（拖拽 update 高频调用，控件更新失败静默）。"""
    global _agent_panel_width
    _agent_panel_width = w
    if _agent_panel_ref:
        _agent_panel_ref.width = w
        try:
            _agent_panel_ref.update()
        except Exception:
            pass


def _save_agent_width() -> None:
    """拖拽结束持久化宽度到 config ui.agent_panel_width。"""
    try:
        from paperpilot.config import save_config
        save_config({"ui": {"agent_panel_width": _agent_panel_width}})
    except Exception:
        pass


_load_saved_agent_width()


def _agent_theme_colors():
    """根据当前主题返回 Agent 面板配色。"""
    return {
        "user_bubble": surface_hi(),
        "system_bubble": surface_hi(),
        "user_text": text_primary(),
        "agent_text": text_primary(),
        "muted_text": text_secondary(),
    }


def _agent_markdown_styles(size: int = FS_LG) -> ft.MarkdownStyleSheet:
    """让长回复在窄面板中保持与桌面主题一致的正文层级。"""
    primary = text_primary()
    muted = text_secondary()
    body = ft.TextStyle(size=size, height=1.55, color=primary,
                        font_family=FONT_FAMILY)
    heading = ft.TextStyle(size=FS_XL, height=1.35, color=primary,
                           weight=FW_SEMIBOLD, font_family=FONT_FAMILY)
    edge = ft.BorderSide(1, border_color())
    return ft.MarkdownStyleSheet(
        p_text_style=body,
        h1_text_style=heading,
        h2_text_style=heading,
        h3_text_style=ft.TextStyle(size=FS_LG, height=1.4, color=primary,
                                   weight=FW_SEMIBOLD, font_family=FONT_FAMILY),
        h4_text_style=body,
        strong_text_style=ft.TextStyle(weight=FW_SEMIBOLD, color=primary),
        a_text_style=ft.TextStyle(color=ft.Colors.PRIMARY, weight=FW_MEDIUM),
        code_text_style=ft.TextStyle(size=FS_MD, color=primary,
                                     bgcolor=surface_hi(), font_family="Consolas"),
        blockquote_text_style=ft.TextStyle(size=size, height=1.5, color=muted,
                                           font_family=FONT_FAMILY),
        blockquote_padding=SP_SM,
        blockquote_decoration=ft.BoxDecoration(
            bgcolor=surface_hi(), border=ft.Border(left=ft.BorderSide(2, border_color())),
            border_radius=R_SM,
        ),
        codeblock_padding=SP_SM,
        codeblock_decoration=ft.BoxDecoration(
            bgcolor=surface_hi(), border=ft.Border(edge, edge, edge, edge),
            border_radius=R_SM,
        ),
        horizontal_rule_decoration=ft.BoxDecoration(
            border=ft.Border(bottom=edge),
        ),
        table_head_text_style=ft.TextStyle(size=FS_MD, color=primary,
                                           weight=FW_SEMIBOLD, font_family=FONT_FAMILY),
        table_body_text_style=body,
        table_cells_padding=SP_XS,
        list_bullet_text_style=body,
        list_indent=SP_LG,
        block_spacing=SP_SM,
    )


def _agent_markdown(text: str, size: int = FS_LG) -> ft.Markdown:
    return ft.Markdown(
        text,
        selectable=True,
        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        md_style_sheet=_agent_markdown_styles(size),
        code_theme=(ft.MarkdownCodeTheme.GITHUB if not state.dark_mode
                    else ft.MarkdownCodeTheme.A11Y_DARK),
        auto_follow_links=True,
        auto_follow_links_target=ft.UrlTarget.BLANK,
        soft_line_break=True,
    )


def _format_agent_text(text: str):
    """处理 Agent 消息：宽表格转列表，返回 (显示文本, [原始表格])。"""
    if "|---" not in text and "| --" not in text:
        return text, []

    lines = text.split('\n')
    result_lines = []
    tables: list[tuple[list[str], list[list[str]]]] = []
    buf: list[str] = []
    header: list[str] = []
    rows: list[list[str]] = []
    in_table = False

    def _flush_text():
        nonlocal buf
        if buf:
            result_lines.extend(buf)
            buf = []

    def _flush_table():
        nonlocal header, rows, in_table
        if not rows:
            header, rows = [], []
            in_table = False
            return
        tables.append((list(header), [list(r) for r in rows]))
        result_lines.append(f'<!--TABLE_{len(tables) - 1}-->')
        header, rows = [], []
        in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('|') and '|' in stripped[1:]:
            cells = [c.strip() for c in stripped.split('|')[1:-1]]
            if not in_table:
                _flush_text()
                in_table = True
                header = cells
            elif not all(c.replace('-', '').replace(':', '').strip() == '' for c in cells):
                rows.append(cells)
        else:
            if in_table:
                _flush_table()
            buf.append(line)

    if in_table:
        _flush_table()
    else:
        _flush_text()

    result_text = '\n'.join(result_lines)
    for i, (hdr, data_rows) in enumerate(tables):
        list_repr = _table_to_list(hdr, data_rows)
        result_text = result_text.replace(f'<!--TABLE_{i}-->', list_repr, 1)

    return result_text, tables


def _table_to_list(header: list[str], rows: list[list[str]]) -> str:
    """将表格转为窄面板友好的列表。≤2 列保持原样，>2 列转列表。"""
    if len(header) <= 2:
        parts = ['| ' + ' | '.join(header) + ' |']
        parts.append('|' + '|'.join(['---' for _ in header]) + '|')
        for row in rows:
            padded = row + [''] * (len(header) - len(row))
            parts.append('| ' + ' | '.join(padded[:len(header)]) + ' |')
        return '\n'.join(parts)

    lines = ['']
    for row in rows:
        parts = []
        first = row[0] if row else ''
        for j in range(1, min(len(row), len(header))):
            val = row[j]
            if val:
                h = header[j] if j < len(header) else ''
                parts.append(f'{h}={val}' if h else val)
        if first and parts:
            lines.append(f'- **{first}**: {" · ".join(parts)}')
        elif first:
            lines.append(f'- **{first}**')
        elif parts:
            lines.append(f'- {" · ".join(parts)}')
    lines.append('')
    return '\n'.join(lines)


def _make_bubble(text: str, role: str = "user", *, attachments=None, attachment_directory=None) -> ft.Container:
    """构建一条消息；助手回复按正文排版，用户和状态消息使用轻底。"""
    colors = _agent_theme_colors()
    if role == "agent":
        try:
            formatted, _ = _format_agent_text(text)
            body = _agent_markdown(formatted)
        except Exception:
            logger.warning("Markdown render failed, fallback to plain text", exc_info=True)
            body = ft.Text(text, size=FS_LG, color=colors["agent_text"],
                           no_wrap=False, selectable=True)
        content = body
        bg = surface()
        radius = 0
        margin = None
        padding = ft.padding.Padding(left=SP_LG, top=SP_SM,
                                    right=SP_LG, bottom=SP_MD)
    elif role == "system":
        try:
            body = _agent_markdown(text, FS_MD)
        except Exception:
            logger.warning("System Markdown render failed", exc_info=True)
            body = ft.Text(text, size=FS_MD, color=colors["agent_text"],
                           no_wrap=False, selectable=True)
        content = ft.Column([
            ft.Text("系统", size=FS_XS, color=colors["muted_text"],
                    weight=FW_SEMIBOLD),
            body,
        ], spacing=SP_XS)
        bg = colors["system_bubble"]
        radius = R_MD
        margin = ft.margin.Margin(left=SP_MD, top=0, right=SP_MD, bottom=0)
        padding = ft.padding.Padding(left=SP_MD, top=SP_SM,
                                    right=SP_MD, bottom=SP_SM)
    else:
        content = ft.Column([
            ft.Text("你", size=FS_XS, color=colors["muted_text"],
                    weight=FW_SEMIBOLD),
            ft.Text(text, size=FS_MD, color=colors["user_text"],
                    no_wrap=False, selectable=True),
        ], spacing=SP_XS)
        bg = colors["user_bubble"]
        radius = R_MD
        margin = ft.margin.Margin(left=SP_MD, top=0, right=SP_MD, bottom=0)
        padding = ft.padding.Padding(left=SP_MD, top=SP_SM,
                                    right=SP_MD, bottom=SP_SM)

    if role == "user" and attachments:
        content.controls.append(history_attachments(attachment_directory, attachments))
    return ft.Container(
        content=content,
        bgcolor=bg,
        border_radius=radius,
        margin=margin,
        padding=padding,
        data=role,
    )


def send_agent_message(text: str, role: str = "user", *, attachments=None, attachment_directory=None):
    """向 Agent 对话面板发送一条消息（高频路径：只刷新消息列表子树）。"""
    global _agent_msg_list
    if _agent_msg_list is None:
        return
    bubble = _make_bubble(text, role, attachments=attachments, attachment_directory=attachment_directory)
    _agent_msg_list.controls.append(bubble)
    if len(_agent_msg_list.controls) > 200:
        _agent_msg_list.controls.pop(0)
    try:
        _agent_msg_list.update()
    except RuntimeError:
        pass
    _scroll_agent_to_bottom()


def _show_thinking_bubble():
    """在消息列表末尾添加一个轻量的思考状态行。"""
    global _agent_msg_list, _thinking_active
    if _thinking_active:
        return None, None
    _thinking_active = True
    colors = _agent_theme_colors()
    fg = colors["muted_text"]

    content_text = ft.Text(
        "AI 正在思考", size=FS_MD, color=fg,
        no_wrap=False, selectable=True, italic=True,
    )
    bubble = ft.Container(
        content=content_text,
        bgcolor=surface(),
        padding=ft.padding.Padding(left=SP_LG, top=SP_SM,
                                  right=SP_LG, bottom=SP_SM),
        data="thinking",
    )

    _agent_msg_list.controls.append(bubble)
    if len(_agent_msg_list.controls) > 200:
        _agent_msg_list.controls.pop(0)
    try:
        _agent_msg_list.update()
    except RuntimeError:
        pass

    stop_event = threading.Event()

    def _animate():
        dots = ["", ".", "..", "..."]
        i = 0
        while not stop_event.is_set():
            content_text.value = f"AI 正在思考{dots[i % 4]}"
            i += 1
            try:
                content_text.update()
            except RuntimeError:
                pass
            stop_event.wait(0.5)

    threading.Thread(target=_animate, daemon=True).start()
    return content_text, stop_event


def refresh_agent_panel_theme() -> None:
    """主题切换后就地更新已显示消息，保留临时状态与滚动位置。"""
    if _agent_panel_ref is None or _agent_msg_list is None:
        return

    colors = _agent_theme_colors()
    _agent_panel_ref.bgcolor = surface()
    _agent_panel_ref.border = ft.Border(left=ft.BorderSide(1, border_color()))
    panel_column = _agent_panel_ref.content
    header_row = panel_column.controls[0].content
    header_row.controls[0].color = seed_color()
    header_row.controls[1].color = text_primary()
    panel_column.controls[1].color = border_color()
    for control in panel_column.controls:
        if isinstance(control, ft.Divider):
            control.color = border_color()
    if _session_selector is not None:
        _session_selector.color = text_primary()
        _session_selector.border_color = border_color()
    if _usage_text is not None:
        _usage_text.color = text_secondary()
    if _context_text is not None:
        _context_text.color = text_secondary()
        _context_bar.bgcolor = surface_hi()
    if _command_menu is not None:
        _command_menu.bgcolor = surface()
        _command_menu.border = ft.Border.all(1, border_color())
        _command_menu.content.controls[0].color = text_secondary()
        _command_menu.content.controls[-1].color = text_secondary()
        for row in _command_rows.values():
            row.content.controls[0].color = text_primary()
            row.content.controls[1].color = text_secondary()
    if _attachment_composer:
        _attachment_composer.refresh()

    if _team_view:
        _team_view.refresh()

    for message in _agent_msg_list.controls:
        role = message.data
        if role == "agent":
            message.bgcolor = surface()
            body = message.content
            if isinstance(body, ft.Markdown):
                body.md_style_sheet = _agent_markdown_styles()
                body.code_theme = (ft.MarkdownCodeTheme.GITHUB if not state.dark_mode
                                   else ft.MarkdownCodeTheme.A11Y_DARK)
            else:
                body.color = colors["agent_text"]
        elif role in {"user", "system"}:
            message.bgcolor = colors["user_bubble"]
            label, body = message.content.controls[:2]
            label.color = colors["muted_text"]
            if isinstance(body, ft.Markdown):
                body.md_style_sheet = _agent_markdown_styles(FS_MD)
                body.code_theme = (ft.MarkdownCodeTheme.GITHUB if not state.dark_mode
                                   else ft.MarkdownCodeTheme.A11Y_DARK)
            else:
                body.color = colors["user_text"]
        elif role == "thinking":
            message.bgcolor = surface()
            message.content.color = colors["muted_text"]
        elif role == "compression":
            tile = message.content
            tile.bgcolor = tile.collapsed_bgcolor = surface_hi()
            tile.text_color = tile.collapsed_text_color = text_primary()
            tile.icon_color = tile.collapsed_icon_color = text_secondary()
            tile.leading.color = seed_color()
            for label in tile.title.controls:
                label.color = text_primary() if label.size == FS_MD else text_secondary()
            tile.controls[0].md_style_sheet = _agent_markdown_styles(FS_MD)
            tile.controls[0].code_theme = (ft.MarkdownCodeTheme.GITHUB if not state.dark_mode
                                         else ft.MarkdownCodeTheme.A11Y_DARK)

    _refresh_command_menu()
    _agent_panel_ref.update()


def _scroll_agent_to_bottom():
    """将 Agent 消息列表滚到底部。"""
    page = ctx.page
    if _agent_msg_list is None or page is None:
        return

    async def _do():
        await asyncio.sleep(0.3)
        if _agent_msg_list is not None:
            try:
                await _agent_msg_list.scroll_to(offset=-1, duration=0)
            except RuntimeError:
                pass  # A delayed scroll can outlive the desktop session.

    page.run_task(_do)


def _trigger_agent_chat(message: str, papers: list | None = None,
                        thinking_enabled: bool = False,
                        display_message: str = "", *, include_library_context: bool = False,
                        operation: str = "chat", attachments=None):
    """统一的 Agent 对话入口：思考动画 + 后台调用 chat() + 原地显示回复。"""
    global _agent_project_id, _agent_project_name, _agent_topic_desc, _ai_service
    if _thinking_active:
        return
    if _agent_session_id is None:
        load_agent_conversation()
    identity = (_agent_project_id or 0, _agent_session_id)
    project_name, topic_desc = _agent_project_name or "通用", _agent_topic_desc

    # 如果用户已手动选了论文，自动作为上下文
    if not papers and ctx.agent_paper_selection:
        papers = list(ctx.agent_paper_selection)

    # 加载课题论文列表（供 chat() 自动检测 @引用 / 标题匹配）
    _proj_papers = None
    if _agent_project_id is not None:
        try:
            _proj_papers = library.get_project_papers(_agent_project_id)
        except Exception:
            if include_library_context:
                send_agent_message("无法读取课题文献库，未发起分析；请重试。", role="system")
                return

    # 发送后清空选中状态
    ctx.agent_paper_selection.clear()
    ctx.search_selected_ids.clear()
    if ctx.clear_library_ui:
        ctx.clear_library_ui()
    # 清除检索结果复选框 UI
    for cb in ctx.search_checkboxes:
        cb.value = False
        try:
            cb.update()
        except RuntimeError:
            pass
    if ctx.search_select_count_ref:
        ctx.search_select_count_ref.value = "未选中"
        try:
            ctx.search_select_count_ref.update()
        except RuntimeError:
            pass
    if ctx.search_compare_btn:
        ctx.search_compare_btn.visible = False
        try:
            ctx.search_compare_btn.update()
        except RuntimeError:
            pass

    content_text, thinking_stop = _show_thinking_bubble()
    if thinking_stop is None:
        return  # 已有思考动画在进行中
    thinking_bubble = _agent_msg_list.controls[-1] if _agent_msg_list else None

    last_partial_update = 0.0
    def show_partial(text):
        nonlocal last_partial_update
        if identity != (_agent_project_id or 0, _agent_session_id):
            return
        now = time.monotonic()
        if now - last_partial_update < 0.1:
            return
        last_partial_update = now
        thinking_stop.set()
        # Do not expose a half-written action payload as executable UI.
        content_text.value = re.split(r"\[(?:ACTION:|PROJECT_UPDATE|TEAM)", text, maxsplit=1)[0]
        try:
            content_text.update()
        except RuntimeError:
            pass
    cm = _ai_service.get_conversation(identity[0], project_name, topic_desc, identity[1])
    run = begin_agent_run(cm, identity[0], display_message or message, operation, show_partial, attachments=attachments)
    if run is None:
        thinking_stop.set()
        return

    def remove_thinking():
        thinking_stop.set()
        if identity == (_agent_project_id or 0, _agent_session_id) and _agent_msg_list:
            if thinking_bubble in _agent_msg_list.controls:
                _agent_msg_list.controls.remove(thinking_bubble)
                try:
                    _agent_msg_list.update()
                except RuntimeError:
                    pass

    def _save_error(text):
        try:
            if not run.user_recorded:
                cm.add_user_message(format_attachment_material(run.attachments) + run.resume_context + message,
                                    display_content=display_message or message, attachments=run.attachments)
                run.user_recorded = True
            _ai_service.log_message(identity[0], project_name, "assistant",
                                    text, topic_desc, session_id=identity[1])
        except (ValueError, OSError):
            logger.warning("Agent error was not saved: the original chat is unavailable")

    def _bg_chat():
        try:
            with run_scope(run):
                run.phase("模型回复")
                attachment_kwargs = dict(attachments=attachments) if attachments else {}
                result = _ai_service.chat(
                    project_id=identity[0],
                    project_name=project_name,
                    message=message,
                    topic_desc=topic_desc,
                    papers=papers,
                    project_papers=_proj_papers,
                    thinking_enabled=thinking_enabled,
                    display_message=display_message,
                    session_id=identity[1],
                    include_library_context=include_library_context,
                    operation=operation,
                    on_team_change=_team_view.notify if _team_view else None,
                    **attachment_kwargs,
                )
                checkpoint()
            reply = result.get("reply", "抱歉，AI 服务暂时无法回复。")

            # Diagnostic logging contains lengths rather than conversation text.
            logger.info("[Agent] reply length: %d", len(reply))

            # 解析 [ACTION:xxx] 标记，提取动作并在主线程执行
            reply, actions = _parse_agent_actions(reply)
            logger.info("[Agent] parsed actions: %s", actions)

            # 检测课题修改提案 [PROJECT_UPDATE]...[/PROJECT_UPDATE]
            proposal = _parse_project_update(reply)
            if proposal and identity[0]:
                new_name, new_desc = proposal
                reply = re.sub(r'\s*\[PROJECT_UPDATE\].*?\[/PROJECT_UPDATE\]', '', reply, flags=re.DOTALL).strip()

            thinking_stop.set()
            run.token.check()
            if identity != (_agent_project_id or 0, _agent_session_id):
                return  # The backend already persisted the response in its original chat.
            if _agent_msg_list and thinking_bubble in _agent_msg_list.controls:
                try:
                    _agent_msg_list.controls.remove(thinking_bubble)
                    _agent_msg_list.update()
                except RuntimeError:
                    pass
            if result.get("compressed"):
                load_agent_conversation()
            else:
                send_agent_message(reply or "AI 未返回内容，请查看用量详情或稍后重试。", role="agent")

            # 弹出确认对话框
            if proposal and identity[0]:
                run.token.check()
                _show_project_update_dialog(identity[0], new_name, new_desc)

            # AI 没输出 ACTION 标签时，根据用户消息意图自动兜底
            if not actions:
                actions = _infer_actions_from_message(message, reply)
                if actions:
                    logger.info("[Agent] fallback inferred actions: %s", actions)

            # 在主线程执行 Agent 动作（search 是异步的，后续动作需等搜索完成）
            if actions and ctx.page:
                run.plan(actions)
                run.reserve()  # Own the scheduled callback before the chat job exits.
                async def _run_actions():
                    try:
                        with run_scope(run):
                            if identity != (_agent_project_id or 0, _agent_session_id):
                                return
                            has_search = any(a["type"] == "search" for a in actions)
                            if has_search and len(actions) > 1:
                                _dispatch_agent_action(actions[0], run)
                                remaining = [a["type"] for a in actions[1:]]
                                send_agent_message(
                                    f"检索完成后请再次告诉我执行后续操作（{', '.join(remaining)}）。", role="system")
                            else:
                                for action in actions:
                                    checkpoint()
                                    _dispatch_agent_action(action, run)
                    except OperationCancelled:
                        pass
                    except Exception:
                        run.fail()
                        logger.exception("Agent action failed")
                    finally:
                        run.finish()
                try:
                    ctx.page.run_task(_run_actions)
                except Exception:
                    run.finish()
                    raise

        except OperationCancelled:
            pass
        except Exception as ex:
            run.fail()
            thinking_stop.set()
            if identity != (_agent_project_id or 0, _agent_session_id):
                _save_error(f"出错了：{ex}")
                return
            if _agent_msg_list and thinking_bubble in _agent_msg_list.controls:
                try:
                    _agent_msg_list.controls.remove(thinking_bubble)
                    _agent_msg_list.update()
                except RuntimeError:
                    pass
            send_agent_message(f"出错了：{ex}", role="agent")
            _save_error(f"出错了：{ex}")
        finally:
            remove_thinking()
            run.finish()

    threading.Thread(target=_bg_chat, daemon=True).start()
    return run


def _trigger_compare_papers(papers: list, source: str = "search"):
    """在 Agent 面板发起论文对比分析。"""
    n = len(papers)
    if n < 2 or _thinking_active:
        return
    source_label = "检索结果" if source == "search" else "文献库"
    visible_msg = f"对比分析 {n} 篇论文（来源：{source_label}）"
    send_agent_message(visible_msg, role="user")

    prompt = (
        f"请对以下 {n} 篇论文进行全面的对比分析，"
        f"从研究目标、方法、主要发现、创新点和局限性五个维度进行比较。"
        f"请用表格或分点形式组织输出，方便快速理解各论文之间的异同。"
    )
    _trigger_agent_chat(prompt, papers=papers, thinking_enabled=True)


def clear_agent_messages():
    """清空 Agent 对话面板。"""
    global _agent_msg_list
    if _agent_msg_list is not None:
        _agent_msg_list.controls.clear()
        try:
            _agent_msg_list.update()
        except RuntimeError:
            pass


def load_agent_conversation():
    """从磁盘加载当前课题的对话历史到面板。"""
    global _agent_session_id
    pid, name = _agent_project_id or 0, _agent_project_name or "通用"
    cm = _ai_service.get_conversation(pid, name, _agent_topic_desc)
    if _active_run is None or _active_run.identity != (pid, cm.session_id):
        cm.recover_interrupted_run()
    _agent_session_id = cm.session_id
    ctx.agent_session_id = cm.session_id
    if _session_selector is not None:
        sessions = _ai_service.session_store(pid, name, _agent_topic_desc).list_sessions()
        _session_selector.options = [ft.dropdown.Option(s["id"], s["title"]) for s in sessions]
        _session_selector.value = cm.session_id
        try:
            _session_selector.update()
        except RuntimeError:
            pass
    refresh_agent_usage()
    if _attachment_composer:
        _attachment_composer.refresh()
    if _team_view:
        _team_view.refresh(recover=True)
    if _agent_msg_list is None:
        return
    clear_agent_messages()

    # 批量构建气泡，最后一次性 update + scroll
    bubbles = []

    for item in cm.display_timeline():
        if item["kind"] == "compression":
            bubbles.append(_make_compaction_card(item["record"]))
        else:
            msg = item["message"]
            role = "user" if msg["role"] == "user" else "agent"
            bubbles.append(_make_bubble(msg["content"], role=role, attachments=msg.get("attachments"),
                                        attachment_directory=cm.storage_directory))
    maintenance = cm._meta.get("maintenance", {})
    if maintenance.get("state") in {"interrupted", "failed", "cancelled"}:
        text = {"interrupted": "上次压缩未正常结束；已提交的摘要与原始对话保留。",
                "failed": "上次压缩未完成，原上下文保留。",
                "cancelled": "上次压缩已停止，原始对话保留。"}[maintenance["state"]]
        bubbles.append(_make_bubble(text, role="system"))

    _agent_msg_list.controls.extend(bubbles)
    if len(_agent_msg_list.controls) > 200:
        _agent_msg_list.controls = _agent_msg_list.controls[-200:]
    try:
        _agent_msg_list.update()
    except RuntimeError:
        pass

    _scroll_agent_to_bottom()


def set_agent_project(project_id: int | None, project_name: str = "",
                      topic_desc: str = ""):
    """设置 Agent 当前关联的课题，自动加载历史对话。"""
    global _agent_project_id, _agent_project_name, _agent_topic_desc, _agent_session_id
    rename_warning = None
    name_changed = (project_id is not None and _agent_project_id == project_id
                    and _agent_project_name and _agent_project_name != project_name)
    if name_changed:
        # 课题改名：重命名仓库文件夹，并更新会话存储路径。
        # 失败必须让用户知道——否则 DB 已是新名而目录仍为旧名，
        # 用户会看到对话历史/论文目录"清空"（数据其实在旧目录）
        try:
            from paperpilot import repo_manager
            renamed = repo_manager.rename_project(_agent_project_name, project_name)
        except Exception as ex:
            renamed = False
            _rename_err = str(ex)
        else:
            _rename_err = "目标目录已存在或不可移动"
        if not renamed:
            rename_warning = (f"⚠ 课题目录重命名失败（{_rename_err}）。"
                              f"论文与对话目录仍使用旧名称“{_agent_project_name}”。")
        if renamed:
            _ai_service.rebind_project_storage(project_id, project_name)
    _agent_project_id = project_id
    _agent_project_name = project_name
    _agent_topic_desc = topic_desc
    # 镜像到 ctx，供页面读取当前课题上下文
    ctx.agent_project_id = project_id
    ctx.agent_project_name = project_name
    ctx.agent_topic_desc = topic_desc
    _agent_session_id = None
    ctx.agent_session_id = None
    try:
        load_agent_conversation()
    except (ValueError, OSError) as ex:
        clear_agent_messages()
        send_agent_message(f"无法加载会话：{ex}", role="system")
    if rename_warning:
        send_agent_message(rename_warning, role="system")


def refresh_agent_usage():
    """Show weighted token statistics for this chat, with missing usage explicit."""
    refresh_agent_context()
    if _usage_text is None:
        return
    try:
        if _session_selector is not None and _agent_session_id:
            sessions = _ai_service.session_store(_agent_project_id or 0, _agent_project_name or "通用", _agent_topic_desc).list_sessions()
            _session_selector.options = [ft.dropdown.Option(s["id"], s["title"]) for s in sessions]
            _session_selector.update()
        stats = UsageStore().summary(_agent_project_id or 0, _agent_session_id, "chat")
        ratio = stats["cache_ratio"]
        cached = f"{ratio:.1%}" if ratio is not None else "暂无数据"
        input_count = f"{stats['input_tokens']:,}" if stats["reported"] or not stats["requests"] else "未提供"
        output_count = f"{stats['output_tokens']:,}" if stats["output_reported"] or not stats["requests"] else "未提供"
        _usage_text.value = (f"输入 {input_count} · 输出 {output_count}\n"
                             f"主对话缓存 {cached} · {stats['requests']} 次请求")
        if stats["reported"] < stats["requests"]:
            _usage_text.value += "（部分无用量）"
        _usage_text.tooltip = "缓存命中的输入 token / 有缓存计数的输入 token。点击右侧查看各任务详情。"
        _usage_text.update()
    except RuntimeError:
        pass
    except Exception:
        _usage_text.value = "用量统计暂不可用"
        logger.warning("Agent usage display failed", exc_info=True)


def refresh_agent_context():
    if _context_text is None:
        return
    try:
        draft = (_agent_input.value or "") if _agent_input else ""
        if _slash_filter(draft) is not None:
            draft = ""
        status = _ai_service.get_context_status(_agent_project_id or 0, _agent_project_name or "通用",
                    _agent_topic_desc, session_id=_agent_session_id, draft=draft)
        window = f"{status['window']:,}" if status["window"] else "未配置"
        ratio = f" · {status['ratio']:.1%}" if status["ratio"] is not None else ""
        _context_text.value = f"上下文 ≈{status['used']:,} / {window} token{ratio}"
        _context_bar.value = min(1, status["ratio"]) if status["ratio"] is not None else 0
        _context_bar.color = ft.Colors.ERROR if (status["ratio"] or 0) >= .9 else seed_color()
        detail = "最近聊天输入用量 + 后续内容估算" if status["anchored"] else "当前发送上下文估算"
        attachment_tokens = 0
        if _attachment_composer:
            from paperpilot.conversation import _estimate_tokens
            for item in _attachment_composer.pending:
                attachment_tokens += _estimate_tokens(item.excerpt) + 4096 * len(item.images)
        _context_text.tooltip = (f"{status['model'] or '模型未配置'}；{detail}。不是累计账单用量。"
            f"草稿约 {status['draft_tokens']:,} token，待发附件另约 {attachment_tokens:,} token；"
            f"自动压缩阈值 {status['compact_threshold']:,}。点击查看详情与配置容量。")
        _context_text.update()
        _context_bar.update()
    except RuntimeError:
        pass
    except Exception:
        _context_text.value = "上下文统计暂不可用"
        logger.warning("Agent context display failed", exc_info=True)


def _make_compaction_card(record):
    before, after = record.get("before_tokens"), record.get("after_tokens")
    label = (f"估算 {before:,} → {after:,} token · 原始记录保留" if before is not None and after is not None
             else f"涵盖 {record.get('original_rounds', '?')} 轮 · 原始记录按现有历史保存")
    body = _agent_markdown(record.get("rounds_summary", ""), FS_MD)
    summary_key = ft.ScrollKey("context-summary-" + record.get("compressed_at", "legacy"))
    body.key = summary_key
    async def reveal(e):
        if e.control.expanded and _agent_msg_list is not None:
            await asyncio.sleep(.35)
            try:
                await _agent_msg_list.scroll_to(scroll_key=summary_key, duration=150)
            except RuntimeError:
                pass
    tile = ft.ExpansionTile(
        title=ft.Column([
            ft.Text("手动压缩已完成" if record.get("mode") == "manual" else "上下文已压缩",
                    size=FS_MD, weight=FW_MEDIUM, color=text_primary()),
            ft.Text(label, size=FS_XS, color=text_secondary()),
        ], spacing=SP_XS, tight=True),
        leading=ft.Icon(ft.Icons.COMPRESS, size=FS_XL, color=seed_color()),
        controls=[body], on_change=reveal,
        expanded=False, dense=True, maintain_state=True,
        tile_padding=ft.padding.Padding(left=SP_SM, right=SP_SM, top=SP_XS, bottom=SP_XS),
        controls_padding=ft.padding.Padding(left=SP_MD, right=SP_MD, top=SP_SM, bottom=SP_MD),
        bgcolor=surface_hi(), collapsed_bgcolor=surface_hi(),
        text_color=text_primary(), collapsed_text_color=text_primary(),
        icon_color=text_secondary(), collapsed_icon_color=text_secondary(),
    )
    return ft.Container(content=tile, border_radius=R_MD, data="compression")


def _show_context_details(e=None):
    from paperpilot.config import save_config
    status = _ai_service.get_context_status(_agent_project_id or 0, _agent_project_name or "通用",
                            _agent_topic_desc, session_id=_agent_session_id)
    cm = _ai_service.get_conversation(_agent_project_id or 0, _agent_project_name or "通用",
                                    _agent_topic_desc, _agent_session_id)
    source = "官方标注 1M，按 1,000,000 保守计" if status["source"].startswith("https:") else status["source"]
    capacity = f"{status['window']:,} token" if status["window"] else "未配置（不猜测模型容量）"
    recent = status["sample"].get("input_tokens")
    lines = (f"模型：{status['provider'] or '未配置'} / {status['model'] or '未配置'}\n"
             f"总窗口：{capacity} · {source}\n当前占用：约 {status['used']:,} token\n"
             f"自动压缩阈值：约 {status['compact_threshold']:,} token\n"
             f"回复预留：{status['output_reserve']:,} token\n"
             "当前占用包括规则、有效摘要、近期对话和已注入的研究资料；属于估算，"
             "不等于累计输入/输出用量。压缩后的估算立即重算，下一次聊天用量会更新基准。")
    if recent is not None:
        lines += f"\n最近聊天请求实际输入：{recent:,} token（不是压缩请求的用量）。"
    if not cm._meta.get("history_complete", True):
        lines += "\n部分旧会话在历史版本中已丢弃压缩前原文，现有记录保留；不能恢复缺失原文。"
    field = ft.TextField(label="覆盖此模型的总窗口（token）", value="",
                         autofocus=True,
                         hint_text="留空保留现有容量", keyboard_type=ft.KeyboardType.NUMBER,
                         text_size=FS_MD, disabled=not bool(status["model"]))
    feedback = ft.Text("只影响当前服务商／模型的容量设置。", size=FS_SM, color=text_secondary())
    def save(e):
        value = (field.value or "").strip()
        if not value:
            close_dialog(ctx.page, dlg)
            return
        if not value.isdecimal() or not 1024 <= int(value) <= 50_000_000:
            field.error_text = "请输入 1,024 到 50,000,000 之间的整数"
            field.update()
            return
        try:
            save_config({"agent": {"context_windows": {status["provider"]: {status["model"]: int(value)}}}})
        except Exception:
            feedback.value = "容量设置保存失败，原设置保留。"
            feedback.color = ft.Colors.ERROR
            feedback.update()
            return
        close_dialog(ctx.page, dlg)
        refresh_agent_context()
    dlg = ft.AlertDialog(title=ft.Text("上下文窗口", size=FS_LG),
        content=ft.Column([ft.Text(lines, size=FS_MD, selectable=True), field, feedback],
                          width=SP_XXL * 13, tight=True, scroll=ft.ScrollMode.AUTO, height=SP_XXL * 11),
        actions=[ft.TextButton(content=ft.Text("关闭"), on_click=lambda e: close_dialog(ctx.page, dlg)),
                 ft.TextButton(content=ft.Text("保存容量"), on_click=save)])
    open_dialog(ctx.page, dlg)


_SLASH_COMMANDS = (("compact", "压缩上下文", "保留原始记录，生成继续工作摘要"),
                   ("new", "新建会话", "在当前课题下开始独立聊天"),
                   ("usage", "用量详情", "查看请求消耗与缓存命中"))


def _slash_filter(text):
    """Only a single leading slash word is a command; prose/paths stay ordinary."""
    if not text.startswith("/") or "/" in text[1:] or "\n" in text or any(c.isspace() for c in text.strip()):
        return None
    return text.strip()[1:].casefold()


def _refresh_command_menu():
    if _command_menu is None or _agent_input is None:
        return
    query = _slash_filter(_agent_input.value or "")
    matches = [name for name, _, _ in _SLASH_COMMANDS if query is not None and name.startswith(query)]
    _command_menu.visible = query is not None and not _slash_dismissed
    menu_navigation = bool(_command_menu.visible)
    if _agent_input.ignore_up_down_keys != menu_navigation:
        _agent_input.ignore_up_down_keys = menu_navigation
        try:
            _agent_input.update()
        except RuntimeError:
            pass
    selected = matches[min(_slash_index, len(matches) - 1)] if matches else None
    for name, row in _command_rows.items():
        row.visible = name in matches
        row.disabled = _thinking_active and name in {"compact", "new"}
        row.style = ft.ButtonStyle(bgcolor=surface_hi() if name == selected else surface(),
            alignment=ft.Alignment(-1, 0), shape=ft.RoundedRectangleBorder(radius=R_SM))
    _command_menu.content.controls[-1].value = (
        "正在工作；请先停止当前轮，再压缩或新建会话。" if _thinking_active else
        "Enter 执行选中项 · Esc 收起" if matches else "没有匹配的命令；修改文字或按 Esc 收起。")
    try:
        _command_menu.update()
    except RuntimeError:
        pass


def _dispatch_command(name):
    if _thinking_active and name in {"compact", "new"}:
        return
    _command_menu.visible = False
    _agent_input.value = ""
    _agent_input.update()
    _command_menu.update()
    if name == "compact":
        _start_context_compaction()
    elif name == "new":
        _new_agent_session()
    elif name == "usage":
        _show_usage_details()


def _start_context_compaction(e=None):
    if _thinking_active:
        return
    pid, name, topic, sid = _agent_project_id or 0, _agent_project_name or "通用", _agent_topic_desc, _agent_session_id
    cm = _ai_service.get_conversation(pid, name, topic, sid)
    run = begin_agent_run(cm, pid, "/compact", "compression")
    if run is None:
        return
    pending = _make_bubble("正在压缩上下文…\n保留原始记录、研究目标与近期对话，可点击方块停止。", role="system")
    _agent_msg_list.controls.append(pending)
    _agent_msg_list.update()
    _scroll_agent_to_bottom()
    async def worker():
        def work():
            with run_scope(run):
                run.phase("生成上下文检查点")
                result = _ai_service.compact_context(pid, name, topic, session_id=cm.session_id)
                if result["status"] == "completed":
                    run.completed("上下文压缩")
                elif result["status"] == "failed":
                    run.fail()
                return result
        try:
            result = await asyncio.to_thread(work)
            if run.identity == (_agent_project_id or 0, _agent_session_id):
                if result["status"] == "completed":
                    load_agent_conversation()
                elif pending in _agent_msg_list.controls:
                    index = _agent_msg_list.controls.index(pending)
                    _agent_msg_list.controls[index] = _make_bubble(result["message"], role="system")
                    _agent_msg_list.update()
        except OperationCancelled:
            if run.identity == (_agent_project_id or 0, _agent_session_id) and pending in _agent_msg_list.controls:
                index = _agent_msg_list.controls.index(pending)
                _agent_msg_list.controls[index] = _make_bubble("已停止压缩，原上下文与研究目标保留。", role="system")
                _agent_msg_list.update()
        except asyncio.CancelledError:
            # Closing the desktop must also cancel its background summarizer;
            # a task cancellation is never a successful maintenance checkpoint.
            run.stop()
            raise
        except Exception:
            run.fail()
            logger.warning("Manual context compaction failed", exc_info=True)
            if run.identity == (_agent_project_id or 0, _agent_session_id) and pending in _agent_msg_list.controls:
                index = _agent_msg_list.controls.index(pending)
                _agent_msg_list.controls[index] = _make_bubble("压缩未完成；已保存的记录保留，请重试。", role="system")
                _agent_msg_list.update()
        finally:
            run.finish()
    ctx.page.run_task(worker)


def _show_usage_details(e=None):
    store = UsageStore()
    pid, sid = _agent_project_id or 0, _agent_session_id
    task_names = {"chat": "主对话", "reasoning": "推理", "deep_read": "精读",
                  "compression": "上下文压缩", "score": "打分", "translation": "翻译",
                  "keyword_extraction": "关键词", "connection_test": "连接测试", "other": "其他"}
    def details(all_tasks=False):
        lines = ["服务商实际返回值；按任务、服务商和模型分组"]
        for group in store.groups(None if all_tasks else pid, None if all_tasks else sid):
            ratio = group["cache_ratio"]
            cache = f"{ratio:.1%}" if ratio is not None else "未提供"
            inputs = f"{group['input_tokens']:,}" if group["reported"] else "未提供"
            outputs = f"{group['output_tokens']:,}" if group["output_reported"] else "未提供"
            lines.append(f"\n{task_names.get(group['task'], group['task'])} · {group['provider']} / {group['model']}\n"
                         f"输入 {inputs} / 输出 {outputs}\n"
                         f"缓存 {cache}（{group['cache_hit_tokens']:,} / {group['cache_input_tokens']:,}）"
                         f" · 请求 {group['requests']} · 失败 {group['failed']} · 已停止 {group['cancelled']}\n"
                         f"已返回输入用量 {group['reported']}/{group['requests']} 次 · 缓存计数 {group['cache_reported']} 次")
        overall = store.summary()
        lines.append(f"\n本机全部任务已统计：输入 {overall['input_tokens']:,} / 输出 {overall['output_tokens']:,}"
                     f" · {overall['requests']} 次请求")
        recent = store.records(None if all_tasks else pid, None if all_tasks else sid, limit=10)
        if recent:
            lines.append("\n最近请求（新资料首次输入、服务商缓存预热或回收均可能降低命中）：")
        operations = {"chat": "聊天", "research_overview": "梳理研究现状", "research_gaps": "发现研究空白",
                      "research_plan": "建议技术路线", "project_refine": "完善课题"}
        prefixes = {"cold_start": "首条本机记录", "stable_append": "历史前缀保持",
                    "system_changed": "系统提示变化", "history_changed": "历史变化",
                    "history_shortened": "历史缩短"}
        modes = {"enabled": "思考开启", "disabled": "思考关闭", "default": "服务商默认思考设置"}
        statuses = {"ok": "已完成", "cancelled": "已停止", "error": "请求失败"}
        for r in recent:
            hit, miss = r["cache_hit_tokens"], r["cache_miss_tokens"]
            cache = f"{hit:,}/{hit + miss:,} ({hit / (hit + miss):.1%})" if hit is not None and miss is not None and hit + miss else "未提供"
            source = operations.get(r.get("operation"), task_names.get(r["task"], r["task"]))
            mode = modes.get(r.get("thinking_mode"), "思考设置未记录")
            lines.append(f"\n#{r['id']} · {source} · {task_names.get(r['task'], r['task'])} · {r['model']}\n"
                         f"{statuses.get(r['status'], r['status'])} · 缓存 {cache} · {prefixes.get(r['prefix_state'], '前缀未记录')} · {mode}")
        lines.append("\n推理 token 已包含在输出中，不重复累加。未提供用量的请求不会按零命中计入缓存率。")
        return "\n".join(lines)
    body = ft.Text(details(), size=FS_MD, selectable=True)
    def select_scope(e):
        body.value = details(e.control.value == "all")
        body.update()
    scope = ft.Dropdown(value="chat", text_size=FS_MD,
                        options=[ft.dropdown.Option("chat", "当前会话"),
                                 ft.dropdown.Option("all", "本机全部任务")], on_select=select_scope)
    dlg = ft.AlertDialog(title=ft.Text("用量详情", size=FS_LG),
                         content=ft.Column([scope, ft.Column([body], expand=True, scroll=ft.ScrollMode.AUTO)],
                                           width=420, height=360, spacing=SP_SM),
                         actions=[ft.TextButton("关闭", on_click=lambda e: close_dialog(ctx.page, dlg))])
    open_dialog(ctx.page, dlg)


def _new_agent_session(e=None):
    try:
        _ai_service.create_session(_agent_project_id or 0, _agent_project_name or "通用", _agent_topic_desc)
        load_agent_conversation()
    except (ValueError, OSError) as ex:
        send_agent_message(f"无法新建会话：{ex}", role="system")


def _select_agent_session(e):
    try:
        _ai_service.select_session(_agent_project_id or 0, _agent_project_name or "通用",
                                   e.control.value, _agent_topic_desc)
        load_agent_conversation()
    except (ValueError, OSError) as ex:
        _session_selector.value = _agent_session_id
        _session_selector.update()
        send_agent_message(f"无法切换会话：{ex}", role="system")


def _rename_agent_session(e=None):
    pid, name, sid = _agent_project_id or 0, _agent_project_name or "通用", _agent_session_id
    store = _ai_service.session_store(pid, name, _agent_topic_desc)
    title = next(s["title"] for s in store.list_sessions() if s["id"] == sid)
    field = ft.TextField(value=title, label="会话名称", max_length=80, autofocus=True)
    def save(e):
        try:
            store.rename_session(sid, field.value or "")
        except ValueError as ex:
            field.error_text = str(ex)
            field.update()
            return
        close_dialog(ctx.page, dlg)
        if (pid, sid) == (_agent_project_id or 0, _agent_session_id):
            load_agent_conversation()
    dlg = ft.AlertDialog(title=ft.Text("重命名会话", size=FS_LG), content=field,
                         actions=[ft.TextButton("取消", on_click=lambda e: close_dialog(ctx.page, dlg)),
                                  ft.TextButton("保存", on_click=save)])
    open_dialog(ctx.page, dlg)


def _parse_project_update(text: str) -> tuple | None:
    """从 AI 回复中检测 [PROJECT_UPDATE] 标记，返回 (name, description)。"""
    m = re.search(r'\[PROJECT_UPDATE\]\s*(.+?)\s*\[/PROJECT_UPDATE\]', text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        name = (data.get("name") or "").strip()
        desc = (data.get("description") or "").strip()
        if name:
            return name, desc
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _parse_agent_actions(reply: str) -> tuple[str, list[dict]]:
    """从 AI 回复中提取 [ACTION:xxx] 标记，返回 (清理后文本, 动作列表)。"""
    import re as _re
    actions: list[dict] = []

    def _replacer(m: _re.Match) -> str:
        action_type = m.group(1)
        try:
            params = json.loads(m.group(2))
            if isinstance(params, dict):
                actions.append({"type": action_type, "params": params})
        except (json.JSONDecodeError, AttributeError):
            pass
        return ""

    cleaned = _re.sub(
        r'\s*\[ACTION:(\w+)\]\s*(.+?)\s*\[/ACTION\]\s*',
        _replacer, reply, flags=_re.DOTALL,
    ).strip()
    return cleaned, actions


def _infer_actions_from_message(user_msg: str, ai_reply: str) -> list[dict]:
    """当 AI 未输出 ACTION 标签时，根据用户消息意图兜底推断动作。"""
    msg = user_msg.lower()
    actions: list[dict] = []

    _SEARCH_KW = ("检索", "搜索", "查找", "搜一下", "找论文", "找文章", "帮我找", "帮我搜", "查一下", "搜一搜")
    _SCORE_KW = ("打分", "评分", "精排", "排序", "排一下", "打个分", "评个分", "ai打分", "ai评分")
    _IMPORT_KW = ("保存到文献", "导入文献", "存到文献", "放到文献", "存入文献", "导入课题", "保存到课题",
                   "加入文献", "放入文献", "存进文献", "导入到", "保存到库", "加入到文献", "加入到课题")

    if any(k in msg for k in _SCORE_KW):
        nums = re.findall(r'(\d+)\s*篇', user_msg)
        if not nums:
            nums = re.findall(r'(?:前|top)\s*(\d+)', user_msg, re.IGNORECASE)
        limit = min(int(nums[0]), 50) if nums else 20
        actions.append({"type": "score", "params": {"scope": "all", "limit": limit}})
        return actions

    if any(k in msg for k in _IMPORT_KW):
        params = {}
        fm = re.search(r'(\d+)\s*分以上', user_msg) or re.search(r'(?:大于|超过|>=?)\s*(\d+)\s*分', user_msg)
        if fm:
            params["filter"] = f"ai_score >= {fm.group(1)}"
        actions.append({"type": "import", "params": params})
        return actions

    if any(k in msg for k in _SEARCH_KW):
        _stop = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "will",
                 "can", "have", "has", "been", "but", "not", "also", "its", "such", "into",
                 "which", "their", "these", "those", "more", "based", "using", "used",
                 "about", "between", "through", "during", "before", "after", "above",
                 "each", "every", "both", "few", "most", "other", "some", "any", "all",
                 "than", "very", "just", "only", "then", "when", "where", "how", "what",
                 "new", "recent", "study", "research", "analysis", "review", "paper"}

        reply_words = re.findall(r'\b[A-Za-z][\w-]{2,}\b', ai_reply)
        reply_kw = list(dict.fromkeys(w for w in reply_words if w.lower() not in _stop))

        desc = state.topic_desc or _agent_topic_desc or user_msg[:200]
        desc_words = re.findall(r'\b[A-Za-z][\w-]{2,}\b', desc)
        desc_kw = list(dict.fromkeys(w for w in desc_words if w.lower() not in _stop))

        combined = list(dict.fromkeys(desc_kw + reply_kw))[:8]

        if len(combined) < 2 and state.keywords:
            combined = list(state.keywords)[:8]

        primary = combined[:3] if combined else []
        secondary = combined[3:] if combined else []
        actions.append({"type": "search", "params": {
            "topic_name": state.topic_name or _agent_project_name or "",
            "topic_desc": desc,
            "primary_keywords": primary,
            "secondary_keywords": secondary,
        }})
        return actions

    return actions


def _dispatch_agent_action(action: dict, run: AgentRun | None = None):
    """执行单个 Agent 动作（必须在主线程调用）。"""
    global _agent_project_id, _agent_project_name
    checkpoint()
    if run:
        run.start_step(action["type"])
    action_type = action["type"]
    params = action.get("params", {})
    sa = ctx.search_actions
    logger.info("[Agent dispatch] type=%s, params=%s, sa_keys=%s, has_scores=%d",
                action_type, params, list(sa.keys()) if sa else "EMPTY", len(state.scores))

    # 自动切换到检索页面
    if action_type in ("search", "score", "import") and ctx.switch_page:
        try:
            ctx.switch_page(0)  # 0 = 检索页
        except Exception:
            pass

    if action_type == "search":
        if not sa:
            send_agent_message("请先切换到检索页面。", role="system")
            return

        topic_name = params.get("topic_name", "").strip()
        topic_desc = params.get("topic_desc", "").strip()
        primary_kw = params.get("primary_keywords", [])
        secondary_kw = params.get("secondary_keywords", [])
        keywords = params.get("keywords", [])

        if not primary_kw and not secondary_kw and keywords:
            primary_kw = keywords[:2]
            secondary_kw = keywords[2:]

        if topic_name:
            try:
                sa["topic_name"].value = topic_name
                sa["topic_name"].update()
            except Exception:
                pass
        if topic_desc:
            try:
                sa["topic_desc"].value = topic_desc
                sa["topic_desc"].update()
            except Exception:
                pass

        if primary_kw or secondary_kw:
            state.primary_keywords = list(primary_kw)
            state.secondary_keywords = list(secondary_kw)
            state.regular_keywords = []
            state.keywords = list(primary_kw) + list(secondary_kw)
            try:
                sa["refresh_all_zones"]()
            except Exception:
                pass

        if not state.keywords and topic_desc:
            send_agent_message(f"正在提取关键词并检索：{topic_desc[:60]}...", role="system")
            if run:
                run.reserve()
            def _auto_extract_and_search():
                scheduled = False
                try:
                    with run_scope(run):
                        weighted = extract_all_keywords(topic_desc, top_n=8)
                        checkpoint()
                    async def _set_and_go():
                        try:
                            if run:
                                run.token.check()
                                if run.identity != (_agent_project_id or 0, _agent_session_id):
                                    return
                            state.keywords = [kw for kw, _ in weighted]
                            core = [kw for kw, w in weighted if w >= 1.0]
                            rest = [kw for kw, w in weighted if w < 1.0]
                            state.primary_keywords = core if core else [kw for kw, _ in weighted[:3]]
                            state.secondary_keywords = rest if core else [kw for kw, _ in weighted[3:]]
                            state.regular_keywords = []
                            sa["refresh_all_zones"]()
                            sa["on_start_search"](None, **({"agent_run": run} if run else {}))
                        except OperationCancelled:
                            pass
                        except Exception as ex:
                            send_agent_message(f"检索启动失败：{ex}", role="system")
                        finally:
                            if run:
                                run.finish()
                    ctx.page.run_task(_set_and_go)
                    scheduled = True
                except OperationCancelled:
                    pass
                except Exception as ex:
                    send_agent_message(f"关键词提取失败：{ex}", role="system")
                finally:
                    if run and not scheduled:
                        run.finish()
            threading.Thread(target=_auto_extract_and_search, daemon=True).start()
            return

        all_kw = list(primary_kw) + list(secondary_kw)
        send_agent_message(
            f"正在检索：{topic_desc or topic_name or ' '.join(all_kw[:3])}...",
            role="system",
        )
        try:
            sa["on_start_search"](None, **({"agent_run": run} if run else {}))
        except Exception as ex:
            send_agent_message(f"检索启动失败：{ex}", role="system")

    elif action_type == "score":
        if not sa:
            send_agent_message("请先切换到检索页面。", role="system")
            return
        if not state.scores:
            send_agent_message("当前没有检索结果可供评分。", role="system")
            return

        agent_limit = params.get("limit")
        if isinstance(agent_limit, (int, float)) and agent_limit > 0:
            agent_limit = min(int(agent_limit), 50)
            try:
                sa["ai_limit_dd"].value = str(agent_limit)
                sa["ai_limit_dd"].update()
            except Exception:
                pass
            send_agent_message(f"正在 AI 精排打分（上限 {agent_limit} 篇）...", role="system")
        else:
            send_agent_message("正在 AI 精排打分...", role="system")
        try:
            sa["on_ai_score"](None, **({"agent_run": run} if run else {}))
        except Exception as ex:
            send_agent_message(f"AI 评分启动失败：{ex}", role="system")

    elif action_type == "import":
        if not state.scores:
            send_agent_message("当前没有检索结果可供导入。", role="system")
            return

        if _agent_project_id and _agent_project_name:
            target_pid = _agent_project_id
            target_name = _agent_project_name
            auto_mode = True
        else:
            send_agent_message("请先在文献库中选择一个课题。", role="system")
            if sa:
                try:
                    sa["on_save_to_library"](None)
                except Exception as ex:
                    send_agent_message(f"保存对话框打开失败：{ex}", role="system")
            return

        filter_applied = False
        filter_rule = params.get("filter", "")
        if filter_rule and isinstance(filter_rule, str):
            import re as _re
            m = _re.match(r'(\w+)\s*(>=?|<=?|==)\s*(\d+(?:\.\d+)?)', filter_rule.strip())
            if m:
                filter_applied = True
                field, op, val = m.group(1), m.group(2), float(m.group(3))
                ctx.search_selected_ids.clear()
                for i, (p, _) in enumerate(state.scores):
                    pv = p.get(field)
                    if pv is None:
                        continue
                    try:
                        pv = float(pv)
                    except (TypeError, ValueError):
                        continue
                    if op == '>':
                        match = pv > val
                    elif op == '>=':
                        match = pv >= val
                    elif op == '<':
                        match = pv < val
                    elif op == '<=':
                        match = pv <= val
                    elif op == '==':
                        match = abs(pv - val) < 0.01
                    else:
                        match = False
                    if match:
                        ctx.search_selected_ids.add(i)
                try:
                    sa["refresh_results_table"]() if sa else None
                except Exception:
                    pass

        if ctx.search_selected_ids:
            sel_papers = [state.scores[i][0] for i in sorted(ctx.search_selected_ids) if i < len(state.scores)]
        elif filter_applied:
            sel_papers = []
        else:
            sel_papers = [s[0] for s in state.scores]

        if not sel_papers:
            send_agent_message("没有符合条件的论文。", role="system")
            return

        checkpoint()
        n, _ = library.save_papers_to_project(target_pid, sel_papers,
            [(p, 0.0) for p in sel_papers])
        if run:
            run.completed(f"已保存 {n} 篇文献到课题「{target_name}」")
        ctx.search_selected_ids.clear()

        if sa and sa.get("refresh_results_table"):
            try:
                sa["refresh_results_table"]()
            except Exception:
                pass
            if sa.get("update_search_count"):
                try:
                    sa["update_search_count"]()
                except Exception:
                    pass

        imported = 0
        for paper in sel_papers:
            checkpoint()
            pdf = paper.get("pdf_path", "")
            if not pdf or not os.path.isfile(str(pdf)):
                pdf = repo_manager.get_cached_pdf(paper)
            if pdf and os.path.isfile(str(pdf)):
                paper["pdf_path"] = pdf
                repo_path = repo_manager.import_pdf(paper, target_name)
                if repo_path:
                    library.set_paper_pdf_path_smart(paper, repo_path)
                imported += 1

        if ctx.refresh_paper_list is not None:
            try:
                ctx.refresh_paper_list(target_pid)
            except Exception:
                pass

        _need_dl = [p for p in sel_papers if not (p.get("pdf_path") and os.path.isfile(str(p.get("pdf_path"))))]
        if _need_dl:
            if run:
                run.reserve()
            def _auto_dl():
                ok = 0
                try:
                    with run_scope(run):
                        for paper in _need_dl:
                            checkpoint()
                            try:
                                cache_path = downloader.cache_pdf(paper)
                                checkpoint()
                                if cache_path and os.path.isfile(cache_path):
                                    paper["pdf_path"] = cache_path
                                    repo_path = repo_manager.import_pdf(paper, target_name)
                                    if repo_path:
                                        library.set_paper_pdf_path_smart(paper, repo_path)
                                    ok += 1
                                    if run:
                                        run.completed(f"已导入 PDF：{paper.get('title', '论文')[:80]}")
                            except Exception:
                                pass
                except OperationCancelled:
                    pass
                finally:
                    if run:
                        run.finish()
                if ok:
                    print(f"[auto-dl] Downloaded {ok}/{len(_need_dl)} papers for '{target_name}'", flush=True)
                    if ctx.refresh_paper_list is not None:
                        try:
                            ctx.refresh_paper_list(target_pid)
                        except Exception:
                            pass
            threading.Thread(target=_auto_dl, daemon=True).start()

        dl_note = f"（{len(_need_dl)} 篇后台下载中...）" if _need_dl else ""
        send_agent_message(
            f"已保存 {n} 篇论文到「{target_name}」{dl_note}",
            role="system",
        )


def _show_project_update_dialog(pid: int, new_name: str, new_desc: str):
    """AI 辅助完善课题的确认对话框。"""
    page = ctx.page
    if page is None or pid is None:
        return

    def do_apply(e):
        library.update_project(pid, name=new_name, description=new_desc)
        set_agent_project(pid, new_name, new_desc)
        if ctx.refresh_library:
            ctx.refresh_library()
        send_agent_message(f"已更新课题：**{new_name}**", role="system")
        close_dialog(page, dlg)

    def do_cancel(e):
        send_agent_message("已取消课题修改。", role="system")
        close_dialog(page, dlg)

    dlg = ft.AlertDialog(
        title=ft.Text("AI 建议修改课题"),
        content=ft.Column([
            ft.Text("是否应用以下修改？", size=14),
            ft.Divider(height=4),
            ft.Text(f"名称：{new_name}", size=13, weight=ft.FontWeight.W_500),
            ft.Text(f"描述：{new_desc}", size=13),
        ], spacing=8, tight=True),
        actions=[
            ft.TextButton("取消", on_click=do_cancel),
            ft.FilledButton("确认修改", on_click=do_apply),
        ],
    )
    open_dialog(ctx.page, dlg)


# ── 面板构建 ──

def build_agent_panel() -> tuple[ft.Container, ft.GestureDetector]:
    """构建 Agent 面板容器与拖拽手柄，供 app.py main() 组装。

    Returns:
        (panel_container, resize_handle)
    """
    global _agent_msg_list, _agent_input, _agent_panel_ref, _session_selector, _usage_text, _agent_send_button
    global _context_text, _context_bar, _command_menu, _command_rows, _slash_dismissed, _slash_index
    global _attachment_composer, _team_view
    _slash_dismissed, _slash_index = False, 0
    ctx.refresh_agent_usage = refresh_agent_usage
    ctx.begin_agent_run = begin_agent_run
    def attachments_changed():
        _refresh_run_button()
        refresh_agent_context()
    _attachment_composer = AttachmentComposer(lambda: (_agent_project_id or 0, _agent_session_id),
        attachments_changed, lambda: _thinking_active)
    _team_view = AgentTeamView(
        lambda: _ai_service.get_conversation(_agent_project_id or 0, _agent_project_name or "通用",
                                             _agent_topic_desc, _agent_session_id),
        lambda: (_agent_project_id or 0, _agent_session_id), refresh_agent_usage)

    _agent_input = ft.TextField(
        key=ft.ValueKey("agent-composer-input"),
        hint_text="输入消息，或 / 查看命令",
        multiline=True,
        shift_enter=True,
        min_lines=1,
        max_lines=4,
        expand=True,
        text_size=13,
        border_radius=20,
        content_padding=ft.padding.Padding(left=16, top=10, right=16, bottom=10),
    )
    _agent_msg_list = ft.ListView(
        expand=True, spacing=SP_SM,
        padding=ft.padding.Padding(left=0, top=SP_SM, right=0, bottom=SP_SM),
    )

    def _on_agent_send(e):
        if _thinking_active or _attachment_composer.loading:
            return
        text = (_agent_input.value or "").strip()
        items = list(_attachment_composer.pending)
        if not text and not items:
            return
        query = _slash_filter(_agent_input.value or "")
        if query is not None:
            matches = [name for name, _, _ in _SLASH_COMMANDS if name.startswith(query)]
            if matches:
                _dispatch_command(matches[min(_slash_index, len(matches) - 1)])
            else:
                send_agent_message("未识别的命令。输入 / 可查看可用选项。", role="system")
            return
        text = text or "请分析所附资料。"
        identity = (_agent_project_id or 0, _agent_session_id)
        cm = _ai_service.get_conversation(identity[0], _agent_project_name or "通用", _agent_topic_desc, identity[1])
        try:
            if any(item.images for item in items):
                from paperpilot.context_budget import context_policy
                from paperpilot.llm_client import get_task_model_override
                policy = context_policy(_ai_service._resolve_task_model("chat"))
                ensure_image_support(policy.provider, policy.model)
                reasoning = get_task_model_override("reasoning")
                if reasoning:
                    ensure_image_support(policy.provider, reasoning)
            attachments = persist_attachments(cm.storage_directory, items)
        except (ValueError, OSError) as exc:
            send_agent_message(f"附件未发送：{exc}", role="system")
            return
        send_agent_message(text, role="user", attachments=attachments, attachment_directory=cm.storage_directory)
        bubble = _agent_msg_list.controls[-1]
        run = _trigger_agent_chat(text, attachments=attachments)
        if run is None:
            if bubble in _agent_msg_list.controls:
                _agent_msg_list.controls.remove(bubble)
                _agent_msg_list.update()
            return
        _agent_input.value = ""
        _agent_input.update()
        _attachment_composer.clear(identity)
        _refresh_command_menu()
        refresh_agent_context()

    _agent_input.on_submit = _on_agent_send
    async def composer_change(e):
        global _slash_dismissed, _slash_index
        was_visible = bool(_command_menu and _command_menu.visible)
        _slash_dismissed, _slash_index = False, 0
        _refresh_command_menu()
        refresh_agent_context()
        if was_visible != bool(_command_menu and _command_menu.visible):
            # Inserting the command menu changes the composer layout. Preserve
            # editing focus so Enter cannot activate an unrelated page menu.
            await asyncio.sleep(.1)
            await _agent_input.focus()
    _agent_input.on_change = composer_change
    previous_keyboard = ctx.page.on_keyboard_event
    async def composer_keyboard(e):
        global _slash_dismissed, _slash_index
        if _command_menu and _command_menu.visible:
            key = e.key.casefold().replace(" ", "")
            if key in {"escape", "esc"}:
                _slash_dismissed = True
                _refresh_command_menu()
                await _agent_input.focus()
                return
            if key in {"arrowdown", "arrowup"}:
                query = _slash_filter(_agent_input.value or "")
                matches = [name for name, _, _ in _SLASH_COMMANDS if query is not None and name.startswith(query)]
                if matches:
                    _slash_index = (_slash_index + (1 if key == "arrowdown" else -1)) % len(matches)
                    _refresh_command_menu()
                return
        if previous_keyboard:
            result = previous_keyboard(e)
            import inspect
            if inspect.isawaitable(result):
                await result
    ctx.page.on_keyboard_event = composer_keyboard
    def _send_or_stop(e):
        if _active_run is not None:
            stop_agent_run(e)
        else:
            _on_agent_send(e)
    _agent_send_button = ft.IconButton(icon=ft.Icons.ARROW_UPWARD,
        tooltip="发送消息（Enter）", on_click=_send_or_stop, icon_size=20)

    def _send_preset_prompt(prompt: str, operation: str):
        """发送预设课题讨论 prompt，开启深度思考。"""
        if _thinking_active:
            return
        if not _agent_project_id:
            send_agent_message("请先在文献库中选择一个课题。", role="system")
            return
        send_agent_message(prompt, role="user")
        _agent_input.value = ""
        _agent_input.update()
        _trigger_agent_chat(prompt, thinking_enabled=True, include_library_context=True, operation=operation)

    def _send_project_refine(e=None):
        """发送 AI 辅助完善课题 prompt。"""
        if _thinking_active:
            return
        if not _agent_project_id:
            send_agent_message("请先在文献库中选择一个课题。", role="system")
            return
        prompt = (
            f"你是一个课题设计顾问。请帮助我完善当前研究课题的设计。\n\n"
            f"**当前课题信息：**\n"
            f"- 名称：{_agent_project_name or '未设置'}\n"
            f"- 描述：{_agent_topic_desc or '未设置'}\n\n"
            f"请先分析当前课题名称和描述存在的问题，然后与我讨论如何改进。"
            f"重点讨论方向：\n"
            f"1. 课题名称是否准确、简洁、有学术辨识度？\n"
            f"2. 课题描述是否清晰界定了研究范围和核心问题？\n"
            f"3. 文献库中的论文覆盖了哪些子方向？描述是否与之匹配？\n\n"
            f"**流程要求：**\n"
            f"- 先和我讨论，逐步收敛，不要一上来就给最终答案\n"
            f"- 在我明确表示满意并请你输出最终方案时，用以下格式在回复末尾给出修改结果：\n"
            f"[PROJECT_UPDATE]\n"
            f'{{"name": "新课题名称", "description": "新课题描述"}}\n'
            f"[/PROJECT_UPDATE]\n"
            f"- 如果无需修改，直接告知我即可，不要输出上述标记"
        )
        send_agent_message("帮我完善课题设计", role="user")
        _agent_input.value = ""
        _agent_input.update()
        _trigger_agent_chat(prompt, thinking_enabled=True,
                           display_message="帮我完善课题设计", include_library_context=True, operation="project_refine")

    _preset_menu = ft.PopupMenuButton(
        icon=ft.Icons.AUTO_AWESOME,
        tooltip="课题讨论",
        items=[
            ft.PopupMenuItem(
                content=ft.Text("梳理研究现状"),
                on_click=lambda e: _send_preset_prompt(
                    "请基于文献库中的所有论文，梳理当前课题的研究现状：\n"
                    "1. 该领域要解决的核心问题是什么？\n"
                    "2. 主流方法可以分为哪几类？各自的演进脉络如何？\n"
                    "3. 有哪些关键的突破性成果？\n"
                    "4. 不同研究组/流派之间是否存在观点分歧？", "research_overview"
                ),
            ),
            ft.PopupMenuItem(
                content=ft.Text("发现研究空白"),
                on_click=lambda e: _send_preset_prompt(
                    "请基于文献库分析当前课题的研究空白和机会：\n"
                    "1. 现有方法在哪些场景下表现不佳或未覆盖？\n"
                    "2. 哪些关键问题被普遍忽视？\n"
                    "3. 跨领域的方法或思路是否可以引入？\n"
                    "4. 有哪些低垂果实值得优先尝试？", "research_gaps"
                ),
            ),
            ft.PopupMenuItem(
                content=ft.Text("建议技术路线"),
                on_click=lambda e: _send_preset_prompt(
                    "请基于文献库中的研究进展，为我建议可行的技术路线：\n"
                    "1. 如果我要在这个课题上发一篇顶会/顶刊，最值得做的方向是什么？\n"
                    "2. 需要哪些基础模块和数据资源？\n"
                    "3. 可能的技术难点和应对策略？\n"
                    "4. 建议的实验验证方案", "research_plan"
                ),
            ),
            ft.PopupMenuItem(
                content=ft.Text("AI 辅助完善课题"),
                on_click=_send_project_refine,
            ),
        ],
    )

    _session_selector = ft.Dropdown(
        options=[], expand=True, text_size=FS_SM, height=40,
        content_padding=ft.padding.Padding(left=SP_SM, right=SP_SM, top=SP_XS, bottom=SP_XS),
        color=text_primary(), border_color=border_color(), border_radius=R_SM,
        tooltip="当前课题的独立会话", on_select=_select_agent_session,
    )
    _usage_text = ft.Text("暂无用量", size=FS_XS, color=text_secondary(), expand=True)
    _context_text = ft.Text("正在读取上下文…", size=FS_XS, color=text_secondary(), no_wrap=False)
    _context_bar = ft.ProgressBar(value=0, height=SP_XS, color=seed_color(), bgcolor=surface_hi())
    context_meter = ft.Container(content=ft.Column([_context_text, _context_bar], spacing=SP_XS),
                                 on_click=_show_context_details,
                                 padding=ft.padding.Padding(left=SP_SM, right=SP_SM, top=SP_XS, bottom=SP_XS))
    _command_rows = {
        name: ft.TextButton(content=ft.Column([
            ft.Text(f"/{name}  {title}", size=FS_MD, color=text_primary()),
            ft.Text(description, size=FS_SM, color=text_secondary()),
        ], spacing=SP_XS, tight=True), on_click=lambda e, command=name: _dispatch_command(command))
        for name, title, description in _SLASH_COMMANDS
    }
    _command_menu = ft.Container(visible=False, bgcolor=surface(), border_radius=R_MD,
        border=ft.Border.all(1, border_color()),
        padding=ft.padding.Padding(left=SP_SM, right=SP_SM, top=SP_XS, bottom=SP_XS),
        content=ft.Column([
            ft.Text("命令", size=FS_XS, color=text_secondary()),
            *_command_rows.values(),
            ft.Text("Enter 执行选中项 · Esc 收起", size=FS_XS, color=text_secondary()),
        ], spacing=SP_XS, tight=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH))
    agent_panel = ft.Container(
        content=ft.Column([
            ft.Container(
                content=ft.Row([
                    ft.Icon(ft.Icons.AUTO_AWESOME, size=16, color=seed_color()),
                    ft.Text("StudyCopilot", size=FS_LG, weight=FW_SEMIBOLD,
                            color=text_primary()),
                ], spacing=SP_SM, alignment=ft.MainAxisAlignment.CENTER),
                padding=ft.padding.Padding(top=SP_MD, bottom=SP_MD),
            ),
            ft.Divider(height=1, color=border_color()),
            ft.Row([
                _session_selector,
                ft.IconButton(icon=ft.Icons.ADD_COMMENT_OUTLINED, icon_size=FS_XL,
                              tooltip="新建独立会话", on_click=_new_agent_session),
                ft.IconButton(icon=ft.Icons.DRIVE_FILE_RENAME_OUTLINE, icon_size=FS_XL,
                              tooltip="重命名会话", on_click=_rename_agent_session),
            ], spacing=SP_XS),
            ft.Row([_usage_text,
                    ft.IconButton(icon=ft.Icons.QUERY_STATS, icon_size=FS_XL,
                                  tooltip="用量详情", on_click=_show_usage_details)], spacing=SP_XS),
            _team_view.host,
            _agent_msg_list,
            ft.Divider(height=1, color=border_color()),
            context_meter,
            _attachment_composer.host,
            # Keep a composer sibling mounted even when the menu is hidden.
            # Flutter must not reuse its TextField state for a different child
            # when visible controls are filtered out of a Column.
            ft.Container(content=_command_menu, key=ft.ValueKey("agent-command-host")),
            ft.Row([
                _attachment_composer.button,
                _preset_menu,
                _agent_input,
                _agent_send_button,
            ], spacing=6, key=ft.ValueKey("agent-composer-row")),
        ], spacing=SP_XS),
        width=_agent_panel_width,
        bgcolor=surface(),
        border=ft.Border(left=ft.BorderSide(1, border_color())),
    )
    _agent_panel_ref = agent_panel
    try:
        load_agent_conversation()
    except (ValueError, OSError) as ex:
        send_agent_message(f"无法加载会话：{ex}", role="system")

    def _max_panel_w() -> int:
        page_w = ctx.page.width if ctx.page else 1200
        return int(page_w * _AGENT_PANEL_MAX_RATIO)

    resize_handle = make_resize_handle(
        lambda: _agent_panel_width, _set_agent_panel_width,
        _AGENT_PANEL_MIN, _max_panel_w, on_end=_save_agent_width,
    )

    return agent_panel, resize_handle
