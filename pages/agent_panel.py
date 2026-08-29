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

import flet as ft

from pages.context import (
    ctx, AppState,
    FONT_FAMILY, FS_XS, FS_SM, FS_MD, FS_LG, FS_XL, FS_XXL, FS_HERO,
    FW_REGULAR, FW_MEDIUM, FW_SEMIBOLD, FW_BOLD,
    R_SM, R_MD, R_LG, R_XL, SP_XS, SP_SM, SP_MD, SP_LG, SP_XL, SP_XXL,
    text_primary, text_secondary, text_tertiary, border_color,
    seed_color, app_bg, surface, surface_hi, accent_container,
)
from pages.components import clamp_width, make_resize_handle

logger = logging.getLogger(__name__)

state = ctx.state

from paperpilot.ai_service import AIService
from paperpilot import library
from paperpilot import repo_manager, downloader
from paperpilot.keywords import extract_all_keywords

# ── 模块级状态 ──
_agent_msg_list: ft.ListView | None = None
_agent_input: ft.TextField | None = None
_ai_service = AIService()  # LLM API 封装实例
ctx.ai_service = _ai_service
_agent_project_id: int | None = None
_agent_project_name: str = ""
_agent_topic_desc: str = ""
_thinking_active: bool = False

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
        "user_bubble": accent_container(),
        "agent_bubble": surface_hi(),
        "user_text": seed_color(),
        "agent_text": text_primary(),
    }


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


def _make_bubble(text: str, role: str = "user") -> ft.Container:
    """构建一条消息气泡（不追加到列表，不调用 update）。"""
    colors = _agent_theme_colors()
    if role == "user":
        bg = colors["user_bubble"]
        fg = colors["user_text"]
        label = "你"
    else:
        bg = colors["agent_bubble"]
        fg = colors["agent_text"]
        label = "Agent"

    if role == "agent":
        try:
            formatted, _ = _format_agent_text(text)
            try:
                body = ft.Markdown(
                    formatted,
                    selectable=True,
                    extension_set="gitHubWeb",
                )
            except Exception:
                logger.warning("Markdown gitHubWeb failed, fallback to plain", exc_info=True)
                try:
                    body = ft.Markdown(formatted, selectable=True)
                except Exception:
                    raise
        except Exception:
            logger.warning("Markdown render failed, fallback to plain text", exc_info=True)
            body = ft.Text(text, size=13, color=fg, no_wrap=False, selectable=True)
    else:
        body = ft.Text(text, size=13, color=fg, no_wrap=False, selectable=True)

    return ft.Container(
        content=ft.Column([
            ft.Text(label, size=11, color=fg, weight=ft.FontWeight.W_600, opacity=0.7),
            body,
        ], spacing=2),
        bgcolor=bg,
        border_radius=12,
        padding=ft.padding.Padding(left=12, top=8, right=12, bottom=8),
        expand=True,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
    )


def send_agent_message(text: str, role: str = "user"):
    """向 Agent 对话面板发送一条消息。"""
    global _agent_msg_list
    page = ctx.page
    if _agent_msg_list is None:
        return
    bubble = _make_bubble(text, role)
    _agent_msg_list.controls.append(bubble)
    if len(_agent_msg_list.controls) > 200:
        _agent_msg_list.controls.pop(0)
    try:
        _agent_msg_list.update()
    except RuntimeError:
        pass
    if page:
        try:
            page.update()
        except RuntimeError:
            pass
    _scroll_agent_to_bottom()


def _show_thinking_bubble():
    """在消息列表末尾添加一个"AI 正在思考"动画气泡。"""
    global _agent_msg_list, _thinking_active
    if _thinking_active:
        return None, None
    _thinking_active = True
    colors = _agent_theme_colors()
    fg = colors["agent_text"]

    content_text = ft.Text(
        "AI 正在思考", size=13, color=fg,
        no_wrap=False, selectable=True, italic=True,
    )
    bubble = ft.Container(
        content=ft.Column([
            ft.Text("Agent", size=11, color=fg, weight=ft.FontWeight.W_600, opacity=0.7),
            content_text,
        ], spacing=2),
        bgcolor=colors["agent_bubble"],
        border_radius=12,
        padding=ft.padding.Padding(left=12, top=8, right=12, bottom=8),
        expand=True,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
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


def _scroll_agent_to_bottom():
    """将 Agent 消息列表滚到底部。"""
    page = ctx.page
    if _agent_msg_list is None or page is None:
        return

    async def _do():
        await asyncio.sleep(0.3)
        if _agent_msg_list is not None:
            await _agent_msg_list.scroll_to(offset=-1, duration=0)

    page.run_task(_do)


def _trigger_agent_chat(message: str, papers: list | None = None,
                        thinking_enabled: bool = False,
                        display_message: str = ""):
    """统一的 Agent 对话入口：思考动画 + 后台调用 chat() + 原地显示回复。"""
    global _agent_project_id, _agent_project_name, _agent_topic_desc, _ai_service

    # 如果用户已手动选了论文，自动作为上下文
    if not papers and ctx.agent_paper_selection:
        papers = list(ctx.agent_paper_selection)

    # 加载课题论文列表（供 chat() 自动检测 @引用 / 标题匹配）
    _proj_papers = None
    if _agent_project_id is not None:
        try:
            _proj_papers = library.get_project_papers(_agent_project_id)
        except Exception:
            pass

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

    def _bg_chat():
        global _thinking_active
        try:
            result = _ai_service.chat(
                project_id=_agent_project_id or 0,
                project_name=_agent_project_name or "通用",
                message=message,
                topic_desc=_agent_topic_desc,
                papers=papers,
                project_papers=_proj_papers,
                thinking_enabled=thinking_enabled,
                display_message=display_message,
            )
            reply = result.get("reply", "抱歉，AI 服务暂时无法回复。")

            # DEBUG: 打印 AI 原始回复，检查是否包含 ACTION 标签
            logger.info("[Agent] raw reply (%d chars): ...%s", len(reply), reply[-300:] if len(reply) > 300 else reply)

            # 解析 [ACTION:xxx] 标记，提取动作并在主线程执行
            reply, actions = _parse_agent_actions(reply)
            logger.info("[Agent] parsed actions: %s", actions)

            # 检测课题修改提案 [PROJECT_UPDATE]...[/PROJECT_UPDATE]
            proposal = _parse_project_update(reply)
            if proposal and _agent_project_id:
                new_name, new_desc = proposal
                reply = re.sub(r'\s*\[PROJECT_UPDATE\].*?\[/PROJECT_UPDATE\]', '', reply, flags=re.DOTALL).strip()

            thinking_stop.set()
            _thinking_active = False
            if _agent_msg_list and _agent_msg_list.controls:
                try:
                    _agent_msg_list.controls.pop()
                    _agent_msg_list.update()
                except RuntimeError:
                    pass
            send_agent_message(reply, role="agent")

            # 弹出确认对话框
            if proposal and _agent_project_id:
                _show_project_update_dialog(_agent_project_id, new_name, new_desc)

            # AI 没输出 ACTION 标签时，根据用户消息意图自动兜底
            if not actions:
                actions = _infer_actions_from_message(message, reply)
                if actions:
                    logger.info("[Agent] fallback inferred actions: %s", actions)

            # 在主线程执行 Agent 动作（search 是异步的，后续动作需等搜索完成）
            if actions and ctx.page:
                async def _run_actions():
                    has_search = any(a["type"] == "search" for a in actions)
                    if has_search and len(actions) > 1:
                        _dispatch_agent_action(actions[0])
                        remaining = [a["type"] for a in actions[1:]]
                        send_agent_message(
                            f"检索完成后请再次告诉我执行后续操作（{', '.join(remaining)}）。",
                            role="system",
                        )
                    else:
                        for action in actions:
                            _dispatch_agent_action(action)
                ctx.page.run_task(_run_actions)

        except Exception as ex:
            thinking_stop.set()
            _thinking_active = False
            if _agent_msg_list and _agent_msg_list.controls:
                try:
                    _agent_msg_list.controls.pop()
                    _agent_msg_list.update()
                except RuntimeError:
                    pass
            send_agent_message(f"出错了：{ex}", role="agent")
            if _agent_project_id is not None:
                _ai_service.log_message(
                    _agent_project_id, _agent_project_name or "通用",
                    "assistant", f"出错了：{ex}", _agent_topic_desc)

    threading.Thread(target=_bg_chat, daemon=True).start()


def _trigger_compare_papers(papers: list, source: str = "search"):
    """在 Agent 面板发起论文对比分析。"""
    n = len(papers)
    if n < 2:
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
    global _agent_msg_list, _agent_project_id, _agent_project_name, _agent_topic_desc, _ai_service
    if _agent_msg_list is None or _agent_project_id is None:
        return

    clear_agent_messages()

    # 直接从磁盘读取，不依赖 AI service 缓存
    from paperpilot.conversation import ConversationManager
    cm = ConversationManager(_agent_project_name, _agent_topic_desc)

    # 注入到 AI service 缓存，保证 chat() 能找到已有上下文
    _ai_service._conversations[_agent_project_id] = cm

    # 批量构建气泡，最后一次性 update + scroll
    bubbles = []

    for cs in cm.compressed_summaries:
        text = f"📋 历史摘要：{cs.get('rounds_summary', '')}"
        bubbles.append(_make_bubble(text, role="agent"))

    for msg in cm.display_messages:
        role = "user" if msg["role"] == "user" else "agent"
        bubbles.append(_make_bubble(msg["content"], role=role))

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
    global _agent_project_id, _agent_project_name, _agent_topic_desc
    name_changed = (project_id is not None and _agent_project_id == project_id
                    and _agent_project_name and _agent_project_name != project_name)
    if name_changed:
        # 课题改名：重命名仓库文件夹，清除旧的 conversation 缓存
        try:
            from paperpilot import repo_manager
            repo_manager.rename_project(_agent_project_name, project_name)
        except Exception:
            pass
        if _ai_service:
            _ai_service._conversations.pop(project_id, None)
    _agent_project_id = project_id
    _agent_project_name = project_name
    _agent_topic_desc = topic_desc
    # 镜像到 ctx，供页面读取当前课题上下文
    ctx.agent_project_id = project_id
    ctx.agent_project_name = project_name
    ctx.agent_topic_desc = topic_desc
    if project_id is not None:
        load_agent_conversation()


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


def _dispatch_agent_action(action: dict):
    """执行单个 Agent 动作（必须在主线程调用）。"""
    global _agent_project_id, _agent_project_name
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
            def _auto_extract_and_search():
                try:
                    weighted = extract_all_keywords(topic_desc, top_n=8)
                    async def _set_and_go():
                        state.keywords = [kw for kw, _ in weighted]
                        core = [kw for kw, w in weighted if w >= 1.0]
                        rest = [kw for kw, w in weighted if w < 1.0]
                        state.primary_keywords = core if core else [kw for kw, _ in weighted[:3]]
                        state.secondary_keywords = rest if core else [kw for kw, _ in weighted[3:]]
                        state.regular_keywords = []
                        try:
                            sa["refresh_all_zones"]()
                        except Exception:
                            pass
                        try:
                            sa["on_start_search"](None)
                        except Exception as ex:
                            send_agent_message(f"检索启动失败：{ex}", role="system")
                    ctx.page.run_task(_set_and_go)
                except Exception as ex:
                    send_agent_message(f"关键词提取失败：{ex}", role="system")
            threading.Thread(target=_auto_extract_and_search, daemon=True).start()
            return

        all_kw = list(primary_kw) + list(secondary_kw)
        send_agent_message(
            f"正在检索：{topic_desc or topic_name or ' '.join(all_kw[:3])}...",
            role="system",
        )
        try:
            sa["on_start_search"](None)
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
            sa["on_ai_score"](None)
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

        n, _ = library.save_papers_to_project(target_pid, sel_papers,
            [(p, 0.0) for p in sel_papers])
        ctx.search_selected_ids.clear()

        if sa and sa.get("refresh_results_table"):
            try:
                sa["refresh_results_table"]()
            except Exception:
                pass

        imported = 0
        for paper in sel_papers:
            pdf = paper.get("pdf_path", "")
            if not pdf or not os.path.isfile(str(pdf)):
                pdf = repo_manager.get_cached_pdf(paper)
            if pdf and os.path.isfile(str(pdf)):
                paper["pdf_path"] = pdf
                repo_path = repo_manager.import_pdf(paper, target_name)
                if repo_path:
                    doi = paper.get("doi") or ""
                    if doi:
                        library.set_paper_pdf_path(doi, repo_path)
                    else:
                        title = paper.get("title") or ""
                        if title:
                            library.set_paper_pdf_path_by_title(title, repo_path, paper.get("year"))
                imported += 1

        if ctx.refresh_paper_list is not None:
            try:
                ctx.refresh_paper_list(target_pid)
            except Exception:
                pass

        _need_dl = [p for p in sel_papers if not (p.get("pdf_path") and os.path.isfile(str(p.get("pdf_path"))))]
        if _need_dl:
            def _auto_dl():
                ok = 0
                for paper in _need_dl:
                    try:
                        cache_path = downloader.cache_pdf(paper)
                        if cache_path and os.path.isfile(cache_path):
                            paper["pdf_path"] = cache_path
                            repo_path = repo_manager.import_pdf(paper, target_name)
                            if repo_path:
                                doi = paper.get("doi") or ""
                                if doi:
                                    library.set_paper_pdf_path(doi, repo_path)
                                else:
                                    title = paper.get("title") or ""
                                    if title:
                                        library.set_paper_pdf_path_by_title(title, repo_path, paper.get("year"))
                            ok += 1
                    except Exception:
                        pass
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
        dlg.open = False
        page.update()

    def do_cancel(e):
        send_agent_message("已取消课题修改。", role="system")
        dlg.open = False
        page.update()

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
    page.overlay.append(dlg)
    dlg.open = True
    page.update()


# ── 面板构建 ──

def build_agent_panel() -> tuple[ft.Container, ft.GestureDetector]:
    """构建 Agent 面板容器与拖拽手柄，供 app.py main() 组装。

    Returns:
        (panel_container, resize_handle)
    """
    global _agent_msg_list, _agent_input, _agent_panel_ref

    _agent_input = ft.TextField(
        hint_text="问问 PaperPilot Agent...",
        multiline=True,
        min_lines=1,
        max_lines=4,
        expand=True,
        text_size=13,
        border_radius=20,
        content_padding=ft.padding.Padding(left=16, top=10, right=16, bottom=10),
    )
    _agent_msg_list = ft.ListView(expand=True, spacing=6, padding=ft.padding.Padding(top=4, bottom=4))

    def _on_agent_send(e):
        if _thinking_active:
            send_agent_message("AI 正在思考中，请稍候...", role="system")
            return
        text = _agent_input.value.strip()
        if not text:
            return
        send_agent_message(text, role="user")
        _agent_input.value = ""
        _agent_input.update()
        _trigger_agent_chat(text)

    _agent_input.on_submit = _on_agent_send

    def _send_preset_prompt(prompt: str):
        """发送预设课题讨论 prompt，开启深度思考。"""
        send_agent_message(prompt, role="user")
        _agent_input.value = ""
        _agent_input.update()
        _trigger_agent_chat(prompt, thinking_enabled=True)

    def _send_project_refine(e=None):
        """发送 AI 辅助完善课题 prompt。"""
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
                           display_message="帮我完善课题设计")

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
                    "4. 不同研究组/流派之间是否存在观点分歧？"
                ),
            ),
            ft.PopupMenuItem(
                content=ft.Text("发现研究空白"),
                on_click=lambda e: _send_preset_prompt(
                    "请基于文献库分析当前课题的研究空白和机会：\n"
                    "1. 现有方法在哪些场景下表现不佳或未覆盖？\n"
                    "2. 哪些关键问题被普遍忽视？\n"
                    "3. 跨领域的方法或思路是否可以引入？\n"
                    "4. 有哪些低垂果实值得优先尝试？"
                ),
            ),
            ft.PopupMenuItem(
                content=ft.Text("建议技术路线"),
                on_click=lambda e: _send_preset_prompt(
                    "请基于文献库中的研究进展，为我建议可行的技术路线：\n"
                    "1. 如果我要在这个课题上发一篇顶会/顶刊，最值得做的方向是什么？\n"
                    "2. 需要哪些基础模块和数据资源？\n"
                    "3. 可能的技术难点和应对策略？\n"
                    "4. 建议的实验验证方案"
                ),
            ),
            ft.PopupMenuItem(
                content=ft.Text("AI 辅助完善课题"),
                on_click=_send_project_refine,
            ),
        ],
    )

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
            _agent_msg_list,
            ft.Divider(height=1, color=border_color()),
            ft.Row([
                _preset_menu,
                _agent_input,
                ft.IconButton(icon=ft.Icons.SEND, on_click=_on_agent_send, icon_size=20),
            ], spacing=6),
        ], spacing=SP_XS),
        width=_agent_panel_width,
        bgcolor=surface(),
        border=ft.Border(left=ft.BorderSide(1, border_color())),
    )
    _agent_panel_ref = agent_panel

    def _max_panel_w() -> int:
        page_w = ctx.page.width if ctx.page else 1200
        return int(page_w * _AGENT_PANEL_MAX_RATIO)

    resize_handle = make_resize_handle(
        lambda: _agent_panel_width, _set_agent_panel_width,
        _AGENT_PANEL_MIN, _max_panel_w, on_end=_save_agent_width,
    )

    return agent_panel, resize_handle
