"""PaperPilot - 面向课题攻关的可解释智能文献工作流系统。
Phase 1: 课题输入 → 关键词提取 → 论文抓取 → 排序展示
"""

import asyncio
import json
import logging
import os
import re
import threading

import flet as ft

from paperpilot.keywords import extract_all_keywords, merge_keywords
from paperpilot.mt_translator import translate_terms
from paperpilot.fetcher import fetch_arxiv, fetch_openalex, fetch_with_cascade, fetch_multi_primary, deduplicate
from paperpilot.indexer import rank_papers, unload_cross_encoder
from paperpilot import library
from paperpilot.local_import import scan_folder, extract_pdfs
from paperpilot.pdf_viewer import open_full_reader, render_preview, is_full_reader_available
from paperpilot.ai_service import AIService, save_deep_read_json, get_full_text_for_paper
from paperpilot.library import save_deep_read_notes
from paperpilot import repo_manager, downloader

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s.%(levelname)s: %(message)s",
    stream=__import__("sys").stdout,
)
logger = logging.getLogger(__name__)


# ── 主题定义 ──
THEMES = {
    "mint":  {"label": "薄荷绿", "seed": "#00A86B", "light_bg": "#E5FFF7", "dark_bg": "#0D1F17"},
    "ocean": {"label": "海蓝",   "seed": "#1565C0", "light_bg": "#E8F0FE", "dark_bg": "#0D1B2A"},
    "sand":  {"label": "暖沙",   "seed": "#E65100", "light_bg": "#FFF5F0", "dark_bg": "#1E1610"},
    "dusk":  {"label": "暮紫",   "seed": "#7B1FA2", "light_bg": "#F5F0FF", "dark_bg": "#1A1020"},
    "rose":  {"label": "玫瑰",   "seed": "#D81B60", "light_bg": "#FFF0F4", "dark_bg": "#1F0E15"},
    "cyan":  {"label": "青碧",   "seed": "#0097A7", "light_bg": "#E5F7F9", "dark_bg": "#0D1A1C"},
}
DEFAULT_THEME = "mint"


def apply_theme(page: ft.Page, theme_name: str, dark_mode: bool):
    """应用配色主题和夜间模式。"""
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    seed = theme["seed"]
    bg = theme["dark_bg"] if dark_mode else theme["light_bg"]
    page.theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=bg, font_family="Microsoft YaHei")
    page.dark_theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=theme["dark_bg"], font_family="Microsoft YaHei")
    page.theme_mode = ft.ThemeMode.DARK if dark_mode else ft.ThemeMode.LIGHT
    page.update()


def _border(color):
    """Flet 0.85 兼容的边框辅助函数。"""
    side = ft.BorderSide(1, color)
    return ft.Border(side, side, side, side)


# ── 全局状态 ──
class AppState:
    def __init__(self):
        self.topic_name: str = ""
        self.topic_desc: str = ""
        # 三层关键词结构（全部手工拖拽归类）
        self.keywords: list[str] = []               # 扁平列表，向后兼容
        self.primary_keywords: list[str] = []         # 主关键词（拖入）
        self.secondary_keywords: list[str] = []       # 副关键词（拖入）
        self.regular_keywords: list[str] = []         # 普通关键词（默认归属）
        self.papers: list[dict] = []
        self.scores: list[tuple[dict, float]] = []
        self.is_searching: bool = False
        self.status_text: str = ""
        self.selected_paper: dict | None = None
        self.theme_name: str = DEFAULT_THEME
        self.dark_mode: bool = False


state = AppState()
_page: ft.Page | None = None
_refresh_library = None  # 文献页刷新函数引用，page_switcher 触发
_search_selected_ids = set()  # 检索结果多选已选索引
_agent_paper_selection: list = []  # 当前选中的论文列表，Agent 发送时自动作为上下文
_clear_library_ui = None  # Agent 发消息后清除文献库复选框的回调
_search_actions: dict = {}  # 检索页回调引用，Agent 通过 [ACTION:xxx] 触发

# ── Agent 对话面板（全局常驻右侧）──
_agent_msg_list: ft.ListView | None = None
_agent_input: ft.TextField | None = None
_ai_service = AIService()  # DeepSeek API 封装实例
_agent_project_id: int | None = None
_agent_project_name: str = ""
_agent_topic_desc: str = ""
_thinking_active: bool = False

# ── Agent 面板拖拽拉伸 ──
_AGENT_PANEL_MIN = 290
_AGENT_PANEL_MAX_RATIO = 0.5
_agent_panel_width = 340
_agent_panel_ref: ft.Container | None = None
_resize_start_x: float = 0
_resize_start_width: float = 0


def _on_agent_resize_update(e):
    global _agent_panel_width, _resize_start_x, _resize_start_width
    if _resize_start_x == 0:
        _resize_start_x = e.global_position.x
        _resize_start_width = _agent_panel_width
    page_w = _page.width if _page else 1200
    delta_x = _resize_start_x - e.global_position.x
    _agent_panel_width = _resize_start_width + delta_x
    _agent_panel_width = max(_AGENT_PANEL_MIN, min(_agent_panel_width, int(page_w * _AGENT_PANEL_MAX_RATIO)))
    if _agent_panel_ref:
        _agent_panel_ref.width = _agent_panel_width
        try:
            _agent_panel_ref.update()
        except Exception:
            pass


def _on_agent_resize_end(e):
    global _resize_start_x
    if _agent_panel_ref:
        _agent_panel_ref.width = _agent_panel_width
        try:
            _agent_panel_ref.update()
        except Exception:
            pass
    _resize_start_x = 0


def _agent_theme_colors():
    """根据当前主题返回 Agent 面板配色。"""
    t = THEMES.get(state.theme_name, THEMES[DEFAULT_THEME])
    if state.dark_mode:
        return {
            "user_bubble": ft.Colors.PRIMARY_CONTAINER,
            "agent_bubble": ft.Colors.SURFACE_CONTAINER,
            "user_text": ft.Colors.ON_PRIMARY_CONTAINER,
            "agent_text": ft.Colors.ON_SURFACE,
        }
    else:
        return {
            "user_bubble": ft.Colors.PRIMARY_CONTAINER,
            "agent_bubble": ft.Colors.SURFACE_CONTAINER,
            "user_text": ft.Colors.ON_PRIMARY_CONTAINER,
            "agent_text": ft.Colors.ON_SURFACE,
        }


def _format_agent_text(text: str):
    """处理 Agent 消息：宽表格转列表，返回 (显示文本, [原始表格])。

    Returns:
        (formatted_markdown, [table_data]) — 无表格时 table_data 为空列表
        每个 table_data = (header, rows)
    """
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
    global _agent_msg_list, _page
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
    if _page:
        try:
            _page.update()
        except RuntimeError:
            pass
    _scroll_agent_to_bottom()


def _show_thinking_bubble():
    """在消息列表末尾添加一个"AI 正在思考"动画气泡。

    Returns:
        (content_text, stop_event) — 调用方在拿到结果后 stop_event.set()
        停止动画，然后直接改 content_text.value 原地替换文字。
        若已有思考动画，返回 (None, None)。
    """
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
    global _page
    if _agent_msg_list is None or _page is None:
        return

    async def _do():
        await asyncio.sleep(0.3)
        if _agent_msg_list is not None:
            await _agent_msg_list.scroll_to(offset=-1, duration=0)

    _page.run_task(_do)


def _trigger_agent_chat(message: str, papers: list | None = None,
                        thinking_enabled: bool = False,
                        display_message: str = ""):
    """统一的 Agent 对话入口：思考动画 + 后台调用 chat() + 原地显示回复。

    供 _on_agent_send 和 _trigger_compare_papers 共用。
    """
    global _agent_project_id, _agent_project_name, _agent_topic_desc, _ai_service
    global _agent_paper_selection, _search_selected_ids

    # 如果用户已手动选了论文，自动作为上下文
    if not papers and _agent_paper_selection:
        papers = list(_agent_paper_selection)

    # 加载课题论文列表（供 chat() 自动检测 @引用 / 标题匹配）
    _proj_papers = None
    if _agent_project_id is not None:
        try:
            _proj_papers = library.get_project_papers(_agent_project_id)
        except Exception:
            pass

    # 发送后清空选中状态
    _agent_paper_selection.clear()
    _search_selected_ids.clear()
    if _clear_library_ui:
        _clear_library_ui()
    # 清除检索结果复选框 UI
    for cb in _search_checkboxes:
        cb.value = False
        try:
            cb.update()
        except RuntimeError:
            pass
    if _search_select_count_ref:
        _search_select_count_ref.value = "未选中"
        try:
            _search_select_count_ref.update()
        except RuntimeError:
            pass
    if _search_compare_btn:
        _search_compare_btn.visible = False
        try:
            _search_compare_btn.update()
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
            if actions and _page:
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
                _page.run_task(_run_actions)

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
    """从 AI 回复中提取 [ACTION:xxx] 标记，返回 (清理后文本, 动作列表)。

    动作格式：
        [ACTION:search]{"topic_name": "...", "topic_desc": "...", "keywords": [...]}[/ACTION]
        [ACTION:score]{"scope": "all", "limit": 20}[/ACTION]
        [ACTION:import]{"project": "...", "filter": "ai_score > 80"}[/ACTION]
    """
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
        # 从用户消息中提取数字作为篇数限制
        nums = re.findall(r'(\d+)\s*篇', user_msg)
        if not nums:
            nums = re.findall(r'(?:前|top)\s*(\d+)', user_msg, re.IGNORECASE)
        limit = min(int(nums[0]), 50) if nums else 20
        actions.append({"type": "score", "params": {"scope": "all", "limit": limit}})
        return actions

    if any(k in msg for k in _IMPORT_KW):
        params = {}
        # 提取分数筛选条件，如"60分以上" "大于80分"
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

        # 从 AI 回复中提取英文词
        reply_words = re.findall(r'\b[A-Za-z][\w-]{2,}\b', ai_reply)
        reply_kw = list(dict.fromkeys(w for w in reply_words if w.lower() not in _stop))

        # 从课题描述中提取英文词（更精准的关键词来源）
        desc = state.topic_desc or _agent_topic_desc or user_msg[:200]
        desc_words = re.findall(r'\b[A-Za-z][\w-]{2,}\b', desc)
        desc_kw = list(dict.fromkeys(w for w in desc_words if w.lower() not in _stop))

        # 合并：课题描述关键词优先（更精准），AI 回复补充
        combined = list(dict.fromkeys(desc_kw + reply_kw))[:8]

        # 如果都没有英文词，回退到搜索页已有关键词
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
    global _search_actions, _search_selected_ids, _search_checkboxes
    action_type = action["type"]
    params = action.get("params", {})
    sa = _search_actions
    logger.info("[Agent dispatch] type=%s, params=%s, sa_keys=%s, has_scores=%d",
                action_type, params, list(sa.keys()) if sa else "EMPTY", len(state.scores))

    # 自动切换到检索页面
    if action_type in ("search", "score", "import"):
        try:
            page_switcher(0)  # 0 = 检索页
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

        # 兼容旧格式：无 primary/secondary 时，从 keywords 自动分配
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

        # 没有关键词时，自动从描述提取再检索
        if not state.keywords and topic_desc:
            send_agent_message(f"正在提取关键词并检索：{topic_desc[:60]}...", role="system")
            def _auto_extract_and_search():
                try:
                    weighted = extract_all_keywords(topic_desc, top_n=8)
                    async def _set_and_go():
                        state.keywords = [kw for kw, _ in weighted]
                        core = [kw for kw, w in weighted if w >= 1.0]
                        rest = [kw for kw, w in weighted if w < 1.0]
                        # 高权重做主关键词；若全低权重则取前3个兜底
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
                    _page.run_task(_set_and_go)
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

        # 读取 Agent 指定的篇数限制
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
        global _agent_project_id, _agent_project_name
        if not state.scores:
            send_agent_message("当前没有检索结果可供导入。", role="system")
            return

        # 确定目标课题：优先用 Agent 当前课题，否则弹窗选择
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

        # 按条件筛选论文
        filter_applied = False
        filter_rule = params.get("filter", "")
        if filter_rule and isinstance(filter_rule, str):
            import re as _re
            m = _re.match(r'(\w+)\s*(>=?|<=?|==)\s*(\d+(?:\.\d+)?)', filter_rule.strip())
            if m:
                filter_applied = True
                field, op, val = m.group(1), m.group(2), float(m.group(3))
                _search_selected_ids.clear()
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
                        _search_selected_ids.add(i)
                try:
                    sa["refresh_results_table"]() if sa else None
                except Exception:
                    pass

        # 收集待保存论文（筛选匹配 0 篇时不导入全部）
        if _search_selected_ids:
            sel_papers = [state.scores[i][0] for i in sorted(_search_selected_ids) if i < len(state.scores)]
        elif filter_applied:
            sel_papers = []
        else:
            sel_papers = [s[0] for s in state.scores]

        if not sel_papers:
            send_agent_message("没有符合条件的论文。", role="system")
            return

        # 直接保存到 Agent 当前课题
        n, _ = library.save_papers_to_project(target_pid, sel_papers,
            [(p, 0.0) for p in sel_papers])
        _search_selected_ids.clear()

        # 刷新检索结果表格
        if sa and sa.get("refresh_results_table"):
            try:
                sa["refresh_results_table"]()
            except Exception:
                pass

        # PDF 导入 + 自动下载
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

        # 立即刷新文献库（论文已保存），后台下载完成后再次刷新（绿标）
        if _refresh_paper_list_cb is not None:
            try:
                _refresh_paper_list_cb(target_pid)
            except Exception:
                pass

        # 后台下载缺失 PDF
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
                    global _refresh_paper_list_cb
                    if _refresh_paper_list_cb is not None:
                        try:
                            _refresh_paper_list_cb(target_pid)
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
    if _page is None or pid is None:
        return

    def do_apply(e):
        library.update_project(pid, name=new_name, description=new_desc)
        set_agent_project(pid, new_name, new_desc)
        if _refresh_library:
            _refresh_library()
        send_agent_message(f"已更新课题：**{new_name}**", role="system")
        dlg.open = False
        _page.update()

    def do_cancel(e):
        send_agent_message("已取消课题修改。", role="system")
        dlg.open = False
        _page.update()

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
    _page.overlay.append(dlg)
    dlg.open = True
    _page.update()


_search_select_count_ref = None  # 多选计数 UI
_search_check_handler = None  # _on_search_check_one 引用
_search_checkboxes: list = []  # 复选框控件引用，用于全选免重建
_search_compare_btn = None  # 检索结果"对比分析"按钮引用
results_summary: ft.Text | None = None
detail_sidebar: ft.Container | None = None  # 右侧文献详情侧边栏
_sidebar_busy = False  # 防竞态：侧边栏正在更新时拦截重复操作


def _has_cjk(text: str) -> bool:
    """检测文本是否包含中日韩字符，用于区分中英文关键词。"""
    return any('一' <= c <= '鿿' for c in text)


def _run_pipeline(max_per: int, year_min: str, year_max: str,
                  use_arxiv: bool, use_openalex: bool,
                  top_k: int, ce_candidates: int):
    """在后台线程中运行完整的搜索流水线。

    所有 Flet 控件值由主线程读取后传入，避免跨线程访问控件。
    """
    import time as _time
    _t0 = _time.time()
    print(f"[PaperPilot] === 流水线启动 === max_per={max_per}, arxiv={use_arxiv}, "
          f"openalex={use_openalex}, top_k={top_k}, ce_candidates={ce_candidates}", flush=True)

    papers = []

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
    # secondary_en 保持独立，不混入主关键词

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
            except Exception as e:
                print(f"[PaperPilot] OpenAlex（描述）失败: {e}")

    # 4. 去重
    state.status_text = "去重中..."
    papers = deduplicate(papers)
    print(f"[PaperPilot] 去重后: {len(papers)} 篇")

    if not papers:
        print("[PaperPilot] 未找到论文")
        return [], []

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
    return papers, scores


# ── 左侧导航栏 ──
NAV_ITEMS = [
    ("文献", ft.Icons.FORMAT_LIST_NUMBERED, 1),
    ("检索", ft.Icons.SEARCH, 0),
    ("设置", ft.Icons.SETTINGS, 2),
]


def build_top_nav(active_idx: int) -> ft.Row:
    """生成顶部导航栏，页面切换时替换此栏即可。"""

    def on_nav_click(e):
        idx = e.control.data
        page_switcher(idx)

    nav_buttons = []
    for label, icon, idx in NAV_ITEMS:
        is_active = idx == active_idx
        nav_buttons.append(
            ft.TextButton(
                content=ft.Row([
                    ft.Icon(icon, size=18,
                            color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None),
                    ft.Text(label, size=13,
                           weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.NORMAL,
                           color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None),
                ], spacing=6),
                data=idx,
                on_click=on_nav_click,
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.PRIMARY_CONTAINER if is_active else None,
                    padding=ft.padding.Padding(left=14, top=8, right=14, bottom=8),
                ),
            )
        )

    return ft.Row([
        ft.Text("OpenResearch", size=16),
        ft.VerticalDivider(width=1),
        *nav_buttons,
    ], spacing=6, alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER)


# ── 页面切换 ──
_last_page_idx = 1

def page_switcher(idx: int):
    """切换页面容器（仅更新变化过的容器，避免多余渲染）。"""
    global _last_page_idx
    if idx == _last_page_idx:
        return
    prev, _last_page_idx = _last_page_idx, idx

    containers = [container_project, container_results, container_settings]
    containers[prev].visible = False
    containers[idx].visible = True

    if idx == 1 and _refresh_library is not None:
        _refresh_library()

    top_nav_ref.content = build_top_nav(idx)
    containers[prev].update()
    containers[idx].update()
    top_nav_ref.update()


# ── 课题页 ──
def build_project_page():
    global results_summary, detail_sidebar

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
            label=ft.Text(kw),
            leading=ft.Icon(icon, size=14, color=color) if icon else None,
            bgcolor=ft.Colors.PRIMARY_CONTAINER if zone == "primary" else None,
            on_delete=lambda e, k=kw: _on_delete_keyword(k),
        )
        return ft.Draggable(
            content=chip,
            data={"kw": kw, "from": zone},
            group="kw",
            content_feedback=ft.Chip(
                label=ft.Text(kw),
                bgcolor=ft.Colors.SURFACE,
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
            zone_container.border = _border(ft.Colors.OUTLINE_VARIANT)
            zone_container.update()

        zone_container = ft.Container(
            content=chip_row,
            border=_border(ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            padding=8,
            bgcolor=ft.Colors.SURFACE if hasattr(ft.Colors, 'SURFACE') else None,
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
                ft.Text(label, size=13, weight=ft.FontWeight.W_500),
            ], spacing=4),
            drag_target,
            ft.Text(hint, size=11, color=ft.Colors.OUTLINE),
        ], spacing=4)

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
                    ft.Text(hint, size=12, color=ft.Colors.OUTLINE)
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
    sb_title = ft.Text("", size=18, selectable=True)
    sb_meta = ft.Text("", size=13, selectable=True)
    sb_abstract = ft.Text("", size=13, selectable=True)
    sb_links = ft.Row([], spacing=8)

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
                ft.Text("文献详情", size=16, weight=ft.FontWeight.W_600),
                ft.IconButton(icon=ft.Icons.CLOSE, on_click=on_close_sidebar),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Divider(height=8),
            sb_title,
            sb_meta,
            ft.Divider(height=8),
            ft.Text("摘要", size=14, weight=ft.FontWeight.W_500),
            ft.Container(content=sb_abstract, expand=True),
            ft.Divider(height=8),
            sb_links,
        ], spacing=6),
        width=400,
        right=0,
        top=0,
        bottom=0,
        padding=ft.padding.Padding(left=16, top=12, right=16, bottom=12),
        border=_border(ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        bgcolor=ft.Colors.SURFACE,
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
    results_summary = summary

    # ── 检索结果多选（始终可见）──
    global _search_selected_ids
    _search_selected_ids.clear()

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
        global _search_select_count_ref, _search_compare_btn, _agent_paper_selection
        n = len(_search_selected_ids)
        if _search_select_count_ref:
            _search_select_count_ref.value = f"已选 {n} 篇" if n else "未选中"
            _search_select_count_ref.update()
        if _search_compare_btn:
            _search_compare_btn.visible = (n >= 2)
            try:
                _search_compare_btn.update()
            except RuntimeError:
                pass
        save_to_library_btn.content = ft.Text("保存选中" if n else "保存到文献库")
        try:
            save_to_library_btn.update()
        except RuntimeError:
            pass
        # 同步到 Agent 自动上下文
        if n:
            _agent_paper_selection = [state.scores[i][0] for i in sorted(_search_selected_ids) if i < len(state.scores)]
        else:
            _agent_paper_selection.clear()

    def _on_search_select_all(e):
        checked = e.control.value
        if checked:
            _search_selected_ids.update(range(len(state.scores)))
        else:
            _search_selected_ids.clear()
        # 直接翻转已有复选框，不重建整表
        for cb in _search_checkboxes:
            cb.value = checked
            cb.update()
        _update_search_count()

    def _on_search_check_one(e, idx: int):
        if e.control.value:
            _search_selected_ids.add(idx)
        else:
            _search_selected_ids.discard(idx)
        _update_search_count()

    global _search_select_count_ref, _search_check_handler
    _search_select_count_ref = search_select_count
    _search_check_handler = _on_search_check_one

    search_select_all_cb.on_change = _on_search_select_all


    def _on_search_compare(e):
        """对比分析检索结果选中的论文。"""
        if len(_search_selected_ids) < 2:
            return
        sel = [state.scores[i][0] for i in sorted(_search_selected_ids) if i < len(state.scores)]
        _trigger_compare_papers(sel, source="search")

    search_compare_btn.on_click = _on_search_compare
    global _search_compare_btn
    _search_compare_btn = search_compare_btn

    ai_limit_dd = ft.Dropdown(
        options=[
            ft.dropdown.Option("50", "50 篇"),
            ft.dropdown.Option("20", "20 篇"),
            ft.dropdown.Option("10", "10 篇"),
        ],
        value="50",
        width=80,
        visible=False,
        text_size=12,
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

    _ai_score_status = ft.Text("", size=12, visible=False)

    def on_ai_score(e):
        """AI 精排：筛选有摘要的论文 → 批量打分 → 重排序。"""
        global _ai_scored
        if not _ai_service.is_available:
            ai_score_btn.tooltip = "需要配置 DeepSeek API Key"
            ai_score_btn.update()
            if e is None:
                send_agent_message("AI 服务不可用，请检查 config.yaml 中的 API Key 配置。", role="system")
            return

        # 收集论文
        all_papers = [p for p, _ in state.scores]
        if not all_papers:
            return

        limit = int(ai_limit_dd.value)

        # 收集候选论文，保留原始 state.scores 索引
        # 格式: [(original_index, paper_dict), ...]
        candidates_idx: list[tuple[int, dict]] = []

        if _search_selected_ids:
            # 手动选中：全量打分，上限50
            for i in sorted(_search_selected_ids):
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

        source_label = "已选" if _search_selected_ids else f"前 {limit}"
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
                _results = _ai_service.score_papers(
                    state.topic_desc or state.topic_name, candidates,
                    max_papers=int(ai_limit_dd.value))
            except Exception as ex:
                _results = []
                logger.warning(f"AI score_papers error: {ex}")
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
            # AI 返回的 index 是 candidates 列表位置 → 映射回 state.scores 原始索引
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

        _page.run_task(_poll)

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
            if _search_selected_ids:
                sel_papers = [state.scores[i][0] for i in _search_selected_ids if i < len(state.scores)]
                sel_scores = [(state.scores[i][0], state.scores[i][1]) for i in _search_selected_ids if i < len(state.scores)]
                n, _ = library.save_papers_to_project(pid, sel_papers, sel_scores)
                papers_to_import = sel_papers
                _search_selected_ids.clear()
            else:
                n, _ = library.save_papers_to_project(pid, state.papers, state.scores)
                papers_to_import = [s[0] for s in state.scores]

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
                        global _refresh_paper_list_cb
                        if _refresh_paper_list_cb is not None:
                            try:
                                _refresh_paper_list_cb(pid)
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
            global _refresh_paper_list_cb
            if _refresh_paper_list_cb is not None:
                try:
                    _refresh_paper_list_cb(pid)
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
                ft.Text(f"将 {len(_search_selected_ids) if _search_selected_ids else len(state.scores)} 篇检索结果保存到："),
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

        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    save_to_library_btn.on_click = on_save_to_library

    # ── 分割比例：表单区 vs 结果区 ──
    _form_weight = 2
    _results_weight = 3

    results_area = ft.Column([
        ft.Divider(height=16),
        ft.Text("检索结果", size=22, weight=ft.FontWeight.W_600),
        summary,
        ft.Row([
            search_select_all_cb,
            search_select_count,
            search_compare_btn,
            ai_limit_dd,
            ai_score_btn,
            _ai_score_status,
            save_to_library_btn,
        ], alignment=ft.MainAxisAlignment.END, spacing=8),
        ft.Divider(height=8),
        _search_list,
    ], spacing=6, expand=_results_weight, visible=False)

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

        _page.run_task(_poll)

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
        logger.info("[on_start_search] called, topic_desc=%r, keywords=%s",
                    topic_desc_field.value.strip()[:60], state.keywords[:5] if state.keywords else "EMPTY")
        if not topic_desc_field.value.strip():
            logger.warning("[on_start_search] BLOCKED: empty topic_desc")
            status_text.value = "请先输入检索描述"
            status_text.update()
            return
        if not state.keywords:
            logger.warning("[on_start_search] BLOCKED: empty keywords")
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
        _top_k = int(top_k_slider.value)
        _ce_candidates = int(ce_candidates_slider.value)

        # 线程间共享结果
        _result: dict = {}       # {"papers": ..., "scores": ...} or {"error": ...}
        _done = threading.Event()

        def _run_in_thread():
            """在独立线程中执行流水线，避免 run_in_executor 嵌套回调丢失。"""
            try:
                papers, scores = _run_pipeline(
                    max_per=_max_per, year_min=_year_min, year_max=_year_max,
                    use_arxiv=_use_arxiv, use_openalex=_use_openalex,
                    top_k=_top_k, ce_candidates=_ce_candidates,
                )
                _result["papers"] = papers
                _result["scores"] = scores
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
                if "error" in _result:
                    state.status_text = f"检索失败: {_result['error']}"
                    traceback.print_exception(
                        type(_result["error"]), _result["error"],
                        _result["error"].__traceback__)
                elif not _result.get("papers"):
                    state.status_text = "未找到相关论文"
                    state.papers = []
                    state.scores = []
                    results_area.visible = True
                    _search_list.controls.clear()
                    _search_list.update()
                    summary.value = _summary_text()
                    summary.update()
                else:
                    state.papers = _result["papers"]
                    state.scores = _result["scores"]
                    state.status_text = f"完成！共 {len(_result['scores'])} 篇"
                    results_area.visible = True
                    save_to_library_btn.visible = True
                    ai_score_btn.visible = _ai_service.is_available
                    ai_limit_dd.visible = _ai_service.is_available
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
                if _page:
                    _page.update()

        _page.run_task(_poll)

    search_btn = ft.FilledButton(
        content=ft.Text("开始检索"), icon=ft.Icons.SEARCH, on_click=on_start_search,
        style=ft.ButtonStyle(padding=ft.padding.Padding(left=32, top=16, right=32, bottom=16)),
    )

    # 初始化分区（恢复已有状态）
    refresh_all_zones()

    # ── 页面布局：上方可滚动检索区 + 下方结果列表区 + 右侧详情侧边栏 ──
    # 关键：_search_list 的 expand=True 必须落在非滚动父链中才能拿到有效高度
    scrollable_form = ft.Column([
        ft.Text("PaperPilot", size=28, weight=ft.FontWeight.W_600),
        ft.Text("智能文献检索与筛选", size=14),
        ft.Divider(height=20),
        ft.Text("检索信息", size=16, weight=ft.FontWeight.W_500),
        topic_name_field,
        topic_desc_field,
        ft.Row([
            ft.FilledTonalButton(
                content=ft.Text("提取关键词"), icon=ft.Icons.AUTO_AWESOME,
                on_click=on_extract,
            ),
            manual_kw_field,
        ], spacing=8),
        _make_zone("主关键词", "拖拽关键词至此设为「主关键词」",
                   primary_zone_row, "primary",
                   ft.Icons.STAR, ft.Colors.AMBER),
        _make_zone("副关键词", "拖拽关键词至此设为「副关键词」",
                   secondary_zone_row, "secondary",
                   ft.Icons.ARROW_FORWARD, ft.Colors.PRIMARY),
        _make_zone("普通关键词", "拖拽关键词至此设为「普通关键词」",
                   regular_zone_row, "regular",
                   None, None),
        ft.Divider(height=12),
        ft.Row([search_btn, progress_bar], spacing=16),
        status_text,
    ], spacing=8, scroll=ft.ScrollMode.AUTO, expand=_form_weight)

    left_side = ft.Column([
        scrollable_form,
        ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),
        results_area,
    ], spacing=0, expand=True)

    # ── 注册检索页回调，供 Agent [ACTION:xxx] 标记使用 ──
    global _search_actions
    _search_actions = {
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


# ── 文献详情 ──
def show_paper_detail(paper: dict):
    """在右侧侧边栏展示论文详情。"""
    global _sidebar_busy
    sb = detail_sidebar
    if sb is None or _sidebar_busy:
        return
    _sidebar_busy = True
    from paperpilot.fetcher import get_article_type_label
    sb._title.value = paper.get("title", "")
    source = {"arxiv": "arXiv", "openalex": "OpenAlex", "local_pdf": "本地"}.get(
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

        _page.run_task(_poll_read)

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
        doi = p.get("doi", "")
        doi_url = f"https://doi.org/{doi}" if doi else p.get("url", "")

        def _go_download(e):
            if doi_url:
                _wb.open(doi_url)
            dlg.open = False
            dlg.update()

        def _cancel(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("无法自动获取全文"),
            content=ft.Text(
                "该论文无法自动下载 PDF 或提取全文。\n\n"
                "请先点击「去下载」在浏览器中打开，\n"
                "下载 PDF 后回到此处点击「导入PDF」选择文件。\n\n"
                f"论文 DOI: {doi or '无'}"
            ),
            actions=[
                ft.TextButton("取消", on_click=_cancel),
                ft.FilledButton("去下载", on_click=_go_download),
            ],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

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


# ── 推荐页 ──
_sort_ascending = False
_sort_column = "score"
_search_list = ft.ListView(expand=True, spacing=0)
_ai_scored = False  # 当前检索结果是否已 AI 精排
_refresh_paper_list_cb = None  # 后台下载完成后的刷新回调


def _build_search_header():
    """构建检索结果表头（支持点击排序）。"""
    def _hdr(label, column, width=None, expand=None):
        arrow = ""
        if column and _sort_column == column:
            arrow = " ▲" if not _sort_ascending else " ▼"
        return ft.Container(
            content=ft.TextButton(
                content=ft.Text(f"{label}{arrow}", size=12,
                                weight=ft.FontWeight.W_600),
                on_click=lambda e, c=column: sort_table(c) if c else None,
                style=ft.ButtonStyle(padding=ft.padding.Padding(left=4, top=4, right=4, bottom=4)),
            ),
            width=width, expand=expand,
            padding=ft.padding.Padding(left=4, right=4),
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border=ft.border.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
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


_last_sort_time = 0

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
    from paperpilot.fetcher import get_article_type_label
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
        ft.Text(label, size=12, color=color, weight=ft.FontWeight.W_600),
        border=_border(color),
        border_radius=4,
        padding=ft.padding.Padding(left=4, top=1, right=4, bottom=1),
    )


def refresh_results_table():
    global _search_list, _search_checkboxes
    scored = state.scores
    _search_checkboxes.clear()
    rows = [_build_search_header()]
    for i, (paper, score) in enumerate(scored):
        year_str = str(paper.get("year") or "—")
        cit = paper.get("cited_by_count")
        cit_str = str(cit) if cit is not None else "—"
        source_label = {"arxiv": "arXiv", "openalex": "OpenAlex", "local_pdf": "本地"}
        src = source_label.get(paper.get("source", ""), paper.get("source", ""))

        score_color = (
            ft.Colors.GREEN if score >= 0.4
            else ft.Colors.ORANGE if score >= 0.2
            else ft.Colors.OUTLINE
        )

        def _cell(content, width=None, expand=None):
            return ft.Container(
                content=ft.Text(content, size=12, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
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
                content=ft.Text(f"{value:.3f}", size=12, color=score_color,
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

        is_checked = i in _search_selected_ids
        cb = ft.Checkbox(
            value=is_checked,
            on_change=lambda e, idx=i: _search_check_handler(e, idx),
            scale=0.85,
        )
        _search_checkboxes.append(cb)
        cells.insert(0, ft.Container(content=cb, width=34,
                     padding=ft.padding.Padding(left=4)))

        row = ft.Container(
            content=ft.Row(cells, spacing=0),
            on_click=lambda e, p=paper: show_paper_detail(p),
            border=ft.border.Border(
                bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
        )
        rows.append(row)

    _search_list.controls = rows
    _search_list.update()


def build_results_page():
    """文献管理页面：课题列表 + 论文列表 + 状态筛选 + 阅读 + 导出。"""
    # 页面局部状态
    _selected_project_id = None
    _project_papers: list[dict] = []
    _status_filter = "all"
    _sort_mode = "ce"  # "ce" 或 "ai"

    # ── 左侧：课题列表 ──
    project_list_col = ft.Column(spacing=4, expand=True, scroll=ft.ScrollMode.AUTO)
    selected_project_title = ft.Text(
        "请选择一个课题", size=14, weight=ft.FontWeight.W_500,
        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True,
    )
    paper_count_text = ft.Text("", size=13)

    def refresh_project_list():
        """从数据库刷新课题列表。"""
        projects = library.get_all_projects()
        project_list_col.controls.clear()
        if not projects:
            project_list_col.controls.append(
                ft.Text("暂无课题，检索后保存即可创建", size=13,
                       color=ft.Colors.OUTLINE)
            )
        else:
            for proj in projects:
                is_active = _selected_project_id == proj.id
                btn = ft.TextButton(
                    content=ft.Row([
                        ft.Icon(ft.Icons.FOLDER, size=16,
                                color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None),
                        ft.Text(proj.name, size=13,
                               weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.NORMAL,
                               color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None,
                               max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                    ], spacing=6),
                    data=proj.id,
                    on_click=lambda e, pid=proj.id: on_select_project(pid),
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.PRIMARY_CONTAINER if is_active else None,
                        padding=ft.padding.Padding(left=12, top=8, right=12, bottom=8),
                    ),
                )
                project_list_col.controls.append(btn)
        try:
            project_list_col.update()
        except RuntimeError:
            pass

    # 暴露给 page_switcher，切到文献页时自动刷新
    global _refresh_library
    _refresh_library = refresh_project_list

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
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

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
                set_agent_project(pid, new_name, new_desc)
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
        msg = ft.Text("", size=12)

        def close_dlg(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("编辑课题"),
            content=ft.Column([name_field, desc_field, msg], spacing=12, tight=True, height=240),
            actions=[ft.TextButton("取消", on_click=close_dlg), ft.FilledButton("保存", on_click=do_save)],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

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
            set_agent_project(None)
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
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

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
                content=ft.Text(label, size=12,
                               weight=ft.FontWeight.W_600 if is_selected else ft.FontWeight.NORMAL,
                               color=ft.Colors.ON_PRIMARY_CONTAINER if is_selected else ft.Colors.ON_SURFACE),
                padding=ft.padding.Padding(left=12, right=12, top=5, bottom=5),
                border_radius=16,
                bgcolor=ft.Colors.PRIMARY_CONTAINER if is_selected else ft.Colors.SURFACE_CONTAINER,
                on_click=lambda e, v=val: on_status_filter_click(v),
            )
            chips.append(chip)
        return chips

    def _refresh_filter_chips():
        """刷新筛选标签的高亮状态。"""
        nonlocal _filter_chips
        for i, (label, val) in enumerate(_filter_options):
            is_selected = _status_filter == val
            _filter_chips[i].bgcolor = ft.Colors.PRIMARY_CONTAINER if is_selected else ft.Colors.SURFACE_CONTAINER
            _filter_chips[i].content.color = ft.Colors.ON_PRIMARY_CONTAINER if is_selected else ft.Colors.ON_SURFACE
            _filter_chips[i].content.weight = ft.FontWeight.W_600 if is_selected else ft.FontWeight.NORMAL

    status_filter_row = ft.Row([], spacing=4)

    _library_list = ft.ListView(expand=True, spacing=0)

    # ── 分页 ──
    PAGE_SIZE = 100
    _pagination_page = 0
    _pagination_text = ft.Text("", size=12)
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
                content=ft.Text(label, size=12, weight=ft.FontWeight.W_600),
                width=width, expand=expand,
                padding=ft.padding.Padding(left=4, right=4),
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                border=ft.border.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
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
        global _agent_paper_selection
        if n:
            _agent_paper_selection = [p for p in _project_papers if p["project_paper_id"] in _selected_ids]
        else:
            _agent_paper_selection.clear()

    def _clear_library():
        """Agent 发消息后清除文献库选中状态。"""
        _selected_ids.clear()
        update_count()
        refresh_paper_list()

    global _clear_library_ui
    _clear_library_ui = _clear_library

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
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()


    def _on_library_compare(e):
        """对比分析文献库选中的论文。"""
        if len(_selected_ids) < 2:
            return
        sel = [p for p in _project_papers if p["project_paper_id"] in _selected_ids]
        _trigger_compare_papers(sel, source="library")

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
    upload_progress = ft.Text("", size=12)
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
    sort_mode_text = ft.Text("", size=11, color=ft.Colors.OUTLINE, width=24, text_align=ft.TextAlign.CENTER)

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

        _page.run_task(_poll_sort)

    sort_btn.on_click = on_sort_click

    def on_ai_sort_click(e):
        """对当前课题论文跑 AI 精排。多选时仅排选中论文。"""
        nonlocal _sort_mode
        if _selected_project_id is None:
            return
        if not _ai_service.is_available:
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
                result = _ai_service.score_papers(topic_desc, paper_dicts)
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

        _page.run_task(_poll_ai_sort)

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

        _page.run_task(_poll_upload)

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
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    def refresh_paper_list(target_pid=None):
        """从数据库刷新当前课题的论文列表（分页 + 精简控件）。"""
        nonlocal _pagination_page, _sort_mode
        if target_pid is not None and target_pid != _selected_project_id:
            return  # 回调来自其他课题，忽略
        global _refresh_paper_list_cb
        _refresh_paper_list_cb = refresh_paper_list
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
        _ai_available = _ai_service.is_available

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
                        ft.Text("DOI: ", size=12, color=ft.Colors.OUTLINE),
                        ft.TextButton(
                            content=ft.Text(pdoi, size=12),
                            on_click=lambda e, d=pdoi: webbrowser.open(f"https://doi.org/{d}"),
                            style=ft.ButtonStyle(padding=ft.padding.Padding.all(0)),
                        ),
                    ], spacing=0, wrap=True))
                if purl:
                    cparts.append(ft.Text(f"URL: {purl[:120]}", size=12, color=ft.Colors.OUTLINE))
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
                                size=12, color=ft.Colors.OUTLINE,
                            ))
                    overall = pai_reason.get("overall", "")
                    if overall:
                        cparts.append(ft.Text(
                            f"  综合: {overall}", size=12,
                            color=ft.Colors.OUTLINE, weight=ft.FontWeight.W_500,
                        ))
                cparts.append(ft.Divider(height=8))
                cparts.append(ft.Text("摘要", size=14, weight=ft.FontWeight.W_600))
                cparts.append(ft.Text(pabstract, size=12))
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
                                    cparts.append(ft.Text(f"{k}: {scores_str}", size=12))
                                else:
                                    cparts.append(ft.Text(f"{k}: {v}", size=12))
                        else:
                            cparts.append(ft.Text(pai_notes[:500], size=12))
                    except Exception:
                        cparts.append(ft.Text(pai_notes[:500], size=12))
                if puser_notes:
                    cparts.append(ft.Divider(height=8))
                    cparts.append(ft.Text("用户批注", size=14, weight=ft.FontWeight.W_600))
                    cparts.append(ft.Text(puser_notes, size=12))

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
                _page.overlay.append(dlg)
                dlg.open = True
                _page.update()

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
                ft.Text(str(global_i), size=12, width=30, color=title_color,
                        weight=ft.FontWeight.W_600 if has_pdf else ft.FontWeight.W_400),
                ft.Container(
                    content=ft.Text(title, size=12, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    color=title_color),
                    expand=3, padding=ft.padding.Padding(right=4),
                    on_click=lambda e, p=p: _show_detail_dialog(p),
                ),
                ft.Text(authors[:28], size=12, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS, expand=2),
                ft.Text(year, size=12, width=44),
                ft.Text(
                    str(int(ai_score_val)) if ai_score_val is not None else "—",
                    size=12, color=ai_color,
                    weight=ft.FontWeight.W_600, width=42,
                    tooltip=_ai_tooltip),
                ft.Text(
                    f"{ce_score_val:.3f}",
                    size=12, color=ce_color, width=48),
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
                    bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
                padding=ft.padding.Padding(left=4, top=4, right=4, bottom=4),
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
                content=ft.Text(label, size=12),
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
        _pagination_page = 0
        upload_progress.value = ""
        if project_id is None:
            selected_project_title.value = "请选择一个课题"
            paper_count_text.value = ""
            sort_btn.disabled = True
            ai_sort_btn.disabled = True
            set_agent_project(None)
        else:
            proj = library.get_project(project_id)
            if proj:
                selected_project_title.value = proj.name
                sort_btn.disabled = False
                sort_btn.tooltip = "CE 语义排序"
                ai_sort_btn.disabled = not _ai_service.is_available
                set_agent_project(project_id, proj.name, proj.description or "")
            else:
                sort_btn.disabled = True
                ai_sort_btn.disabled = True
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
                ft.Text("该论文无法通过直链下载，也不在 arXiv 上。", size=12,
                       color=ft.Colors.OUTLINE),
                ft.Text("请尝试手动下载 PDF 后，通过「导入 PDF」添加到文献库。", size=12),
            ]
            if _error_msg:
                content_parts.append(ft.Text(f"调试信息: {_error_msg}", size=11,
                                   color=ft.Colors.ERROR))
            if doi:
                import webbrowser
                content_parts.append(ft.Divider(height=8))
                content_parts.append(ft.Row([
                    ft.Text("DOI: ", size=12, color=ft.Colors.OUTLINE),
                    ft.TextButton(
                        content=ft.Text(doi, size=12),
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
            _page.overlay.append(dlg)
            dlg.open = True
            _page.update()

        _page.run_task(_poll)

    def _on_deep_read(e, paper: dict):
        """后台精读论文：获取全文 → RLM 分析 → 展示结果。"""
        title = (paper.get("title") or "论文")[:40]
        pp_id = paper.get("project_paper_id")

        def _save_msg(role: str, text: str):
            """将消息写入当前课题的对话记录。"""
            if _agent_project_id is not None:
                _ai_service.log_message(
                    _agent_project_id, _agent_project_name, role, text, _agent_topic_desc)

        send_agent_message(f"正在精读：《{title}》...\n\n正在获取全文，请稍候 🔍", role="agent")
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

                result = _ai_service.deep_read(paper, full_text)
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
                    send_agent_message(_status, role="agent")

            if _error:
                send_agent_message(f"精读失败：{_error}", role="agent")
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
                send_agent_message(trunc_msg, role="agent")
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
            send_agent_message(msg, role="agent")
            _save_msg("assistant", msg)

        _page.run_task(_poll)

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

        _page.run_task(_poll)

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
                ft.Text(path, size=11, color=ft.Colors.OUTLINE,
                       font_family="Consolas"),
            ], spacing=8, tight=True),
            actions=[ft.TextButton("确定", on_click=lambda e: _close_dlg(dlg))],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    def _show_export_error(msg: str):
        dlg = ft.AlertDialog(
            title=ft.Text("导出失败"),
            content=ft.Text(msg, size=13),
            actions=[ft.TextButton("确定", on_click=lambda e: _close_dlg(dlg))],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    def _close_dlg(dlg):
        dlg.open = False
        dlg.update()

    def _show_export_unavailable():
        dlg = ft.AlertDialog(
            title=ft.Text("导出功能暂不可用"),
            content=ft.Text("导出模块尚未完成，请等待后续更新。"),
            actions=[ft.TextButton("确定", on_click=lambda e: _close_dlg(dlg))],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    # 首次加载课题列表
    refresh_project_list()

    # ── 布局 ──
    left_panel = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.IconButton(icon=ft.Icons.ADD, tooltip="新建课题", on_click=on_new_project),
                ft.IconButton(icon=ft.Icons.DELETE, tooltip="删除课题", on_click=on_delete_project),
                ft.IconButton(icon=ft.Icons.REFRESH, tooltip="刷新列表", on_click=lambda e: refresh_project_list()),
            ], spacing=2),
            project_list_col,
        ], spacing=6, expand=True),
        width=190,
        padding=ft.padding.Padding(top=8, right=8, bottom=8, left=0),
    )

    right_panel = ft.Container(
        content=ft.Column([
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
                ft.Text("筛选:", size=13),
                status_filter_row,
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(height=8),
            empty_hint,
            _pagination_row,
            _library_list,
        ], spacing=6, expand=True),
        expand=True,
        padding=ft.padding.Padding(top=8, left=8, bottom=8, right=0),
    )

    # 初始化筛选标签
    _filter_chips[:] = _build_filter_chips()
    status_filter_row.controls[:] = _filter_chips

    return ft.Row([
        left_panel,
        ft.VerticalDivider(width=1, color=ft.Colors.OUTLINE_VARIANT),
        right_panel,
    ], expand=True, alignment=ft.CrossAxisAlignment.STRETCH)


# ── 设置持久化 ──
def _save_setting(key: str, value):
    """保存单个搜索/数据源设置到 config.yaml。"""
    from paperpilot.config import save_config
    section, field = key.split(".", 1)
    save_config({section: {field: value}})


def _on_slider_saved(e, key: str):
    _save_setting(key, int(e.control.value))


# ── 设置页 ──
arxiv_switch = ft.Switch(label="arXiv", value=True)
arxiv_switch.on_change = lambda e: _save_setting("data_sources.arxiv", e.control.value)
openalex_switch = ft.Switch(label="OpenAlex", value=True)
openalex_switch.on_change = lambda e: _save_setting("data_sources.openalex", e.control.value)
max_results_slider = ft.Slider(min=100, max=500, value=100, divisions=40,
                                label="{value} 篇")
max_results_slider.on_change = lambda e: _on_slider_saved(e, "search.max_results")
top_k_slider = ft.Slider(min=10, max=200, value=50, divisions=19,
                          label="显示 {value} 篇")
top_k_slider.on_change = lambda e: _on_slider_saved(e, "search.top_k")
ce_candidates_slider = ft.Slider(min=10, max=200, value=100, divisions=19,
                                  label="精排候选 {value} 篇")
ce_candidates_slider.on_change = lambda e: _on_slider_saved(e, "search.ce_candidates")


def _make_model_selector():
    """创建模型选择下拉框，读取/写入 config.yaml。"""
    from paperpilot.config import load_config as _lc, save_config as _sc

    current = _lc().get("deepseek", {}).get("model", "deepseek-v4-flash")

    model_options = [
        ft.dropdown.Option("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ft.dropdown.Option("deepseek-chat", "DeepSeek V3 (chat) - 7月下线"),
    ]

    model_dd = ft.Dropdown(
        options=model_options,
        value=current if current in ("deepseek-v4-flash", "deepseek-chat") else "deepseek-v4-flash",
        expand=True,
    )

    model_status = ft.Text("", size=12)

    def on_change_model(e):
        _sc(updates={"deepseek": {"model": e.control.value}})
        model_status.value = f"已切换至 {e.control.value}，下次搜索生效"
        model_status.color = ft.Colors.GREEN
        model_status.update()

    model_dd.on_change = on_change_model

    return ft.Column([
        ft.Row([model_dd], spacing=8),
        model_status,
    ], spacing=4)


def build_settings_page():
    global container_settings
    from paperpilot.config import load_config, save_config as do_save

    current_config = load_config()
    current_key = current_config.get("deepseek", {}).get("api_key", "")

    def mask_key(key: str) -> str:
        if not key:
            return ""
        if len(key) <= 8:
            return key[:3] + "****" + key[-1:]
        return key[:3] + "****" + key[-4:]

    key_status = ft.Text("", size=12)
    if current_key:
        key_status.value = f"已配置: {mask_key(current_key)}"
        key_status.color = ft.Colors.GREEN
    else:
        key_status.value = "未配置 API Key，关键词提取和翻译功能不可用"
        key_status.color = ft.Colors.ORANGE

    api_key_field = ft.TextField(
        label="DeepSeek API Key",
        hint_text="sk-...",
        value=current_key,
        password=True,
        can_reveal_password=True,
        expand=True,
    )

    save_status = ft.Text("", size=13)

    def on_save_key(e):
        new_key = api_key_field.value.strip()
        if not new_key:
            save_status.value = "API Key 不能为空"
            save_status.color = ft.Colors.ERROR
            save_status.update()
            return
        do_save(updates={"deepseek": {"api_key": new_key}})
        save_status.value = "API Key 已保存，下次搜索生效"
        save_status.color = ft.Colors.GREEN
        save_status.update()
        key_status.value = f"已配置: {mask_key(new_key)}"
        key_status.color = ft.Colors.GREEN
        key_status.update()

    # ── 主题切换 ──
    theme_buttons: dict[str, ft.Container] = {}

    def on_select_theme(name):
        state.theme_name = name
        apply_theme(_page, name, state.dark_mode)
        do_save(updates={"ui": {"theme": name, "dark_mode": state.dark_mode}})
        # 就地更新主题按钮边框（避免重建页面导致滚回顶部）
        for n, btn in theme_buttons.items():
            btn.border = _border(
                ft.Colors.ON_SURFACE if n == name else ft.Colors.OUTLINE_VARIANT
            )
            btn.update()
        # 导航栏重建
        top_nav_ref.content = build_top_nav(2)
        top_nav_ref.update()

    def on_toggle_dark(e):
        state.dark_mode = e.control.value
        apply_theme(_page, state.theme_name, state.dark_mode)
        do_save(updates={"ui": {"theme": state.theme_name, "dark_mode": state.dark_mode}})
        # 按钮边框不随夜间模式变化，只重建导航栏
        top_nav_ref.content = build_top_nav(2)
        top_nav_ref.update()

    theme_selector = ft.Row([
        ft.Column([
            ft.Container(
                width=44, height=44, border_radius=22,
                bgcolor=t["seed"],
                border=_border(
                    ft.Colors.ON_SURFACE if state.theme_name == name else ft.Colors.OUTLINE_VARIANT
                ),
                ink=True,
                on_click=lambda e, n=name: on_select_theme(n),
            ),
            ft.Text(t["label"], size=11, text_align=ft.TextAlign.CENTER),
        ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        for name, t in THEMES.items()
    ], spacing=20, alignment=ft.MainAxisAlignment.CENTER)

    # 收集按钮引用用于就地更新（避免重建页面导致滚回顶部）
    for i, name in enumerate(THEMES):
        col = theme_selector.controls[i]
        btn = col.controls[0]  # Container 是 Column 的第一个子控件
        theme_buttons[name] = btn

    dark_switch = ft.Switch(
        label="夜间模式",
        value=state.dark_mode,
        on_change=on_toggle_dark,
    )

    return ft.Column([
        ft.Text("设置", size=22),
        ft.Divider(height=16),
        ft.Text("DeepSeek API", size=16, weight=ft.FontWeight.W_500),
        ft.Text("用于关键词提取和中英翻译，密钥仅存储在本地 config.yaml", size=13),
        key_status,
        ft.Row([
            api_key_field,
            ft.FilledTonalButton(
                content=ft.Text("保存"), icon=ft.Icons.SAVE, on_click=on_save_key,
            ),
        ], spacing=8),
        save_status,
        ft.Divider(height=12),
        ft.Text("模型", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择 DeepSeek API 模型，7月后 V3 将下线", size=13),
        _make_model_selector(),
        ft.Divider(height=16),
        ft.Text("数据源", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择从哪些来源获取论文", size=13),
        arxiv_switch,
        openalex_switch,
        ft.Divider(height=16),
        ft.Text("检索数量", size=16, weight=ft.FontWeight.W_500),
        ft.Text("每个来源的最大检索结果数", size=13),
        max_results_slider,
        ft.Container(height=8),
        ft.Text("结果显示与精排", size=16, weight=ft.FontWeight.W_500),
        ft.Text("控制最终显示的论文数量和送入精排的候选数", size=13),
        top_k_slider,
        ce_candidates_slider,
        ft.Divider(height=16),
        ft.Text("外观", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择配色主题和夜间模式", size=13),
        theme_selector,
        ft.Container(height=8),
        dark_switch,
        ft.Divider(height=16),
        ft.Text("离线模式", size=16, weight=ft.FontWeight.W_500),
        ft.Text("Embedding 模型：paraphrase-multilingual-MiniLM-L12-v2", size=13),
        ft.Text("已下载至本地缓存，无需联网", size=13, color=ft.Colors.GREEN),
    ], spacing=8, scroll=ft.ScrollMode.AUTO)


# ── 应用入口 ──
def main(page: ft.Page):
    global _page, top_nav_ref
    global container_project, container_results, container_settings
    global _agent_panel_ref, _agent_msg_list, _agent_input

    _page = page
    page.title = "PaperPilot"
    page.window.width = 1200
    page.window.height = 750
    page.window.min_width = 900
    page.window.min_height = 500
    page.padding = 0

    # 加载已保存的所有设置
    from paperpilot.config import load_config
    cfg = load_config()
    ui_config = cfg.get("ui", {})
    state.theme_name = ui_config.get("theme", DEFAULT_THEME)
    state.dark_mode = ui_config.get("dark_mode", False)
    apply_theme(page, state.theme_name, state.dark_mode)

    # 恢复搜索/数据源设置
    search_cfg = cfg.get("search", {})
    if search_cfg.get("max_results"):
        max_results_slider.value = int(search_cfg["max_results"])
    if search_cfg.get("top_k"):
        top_k_slider.value = int(search_cfg["top_k"])
    if search_cfg.get("ce_candidates"):
        ce_candidates_slider.value = int(search_cfg["ce_candidates"])

    ds_cfg = cfg.get("data_sources", {})
    if "arxiv" in ds_cfg:
        arxiv_switch.value = bool(ds_cfg["arxiv"])
    if "openalex" in ds_cfg:
        openalex_switch.value = bool(ds_cfg["openalex"])

    # 顶部导航栏（内容后续由 page_switcher 动态替换）
    top_nav_ref = ft.Container(
        content=build_top_nav(1),
        padding=ft.padding.Padding(left=16, top=8, right=16, bottom=8),
        border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
    )

    container_project = ft.Container(
        content=build_project_page(), visible=False, expand=True,
        padding=ft.padding.Padding(left=16, top=12, right=8, bottom=16),
    )
    container_results = ft.Container(
        content=build_results_page(), visible=True, expand=True,
        padding=ft.padding.Padding(left=16, top=12, right=8, bottom=16),
    )
    container_settings = ft.Container(
        content=build_settings_page(), visible=False, expand=True,
        padding=ft.padding.Padding(left=16, top=12, right=8, bottom=16),
    )

    # ── Agent 对话面板 ──
    global _agent_msg_list, _agent_input
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
            ft.Row([
                ft.Text("StudyCopilot", size=15, weight=ft.FontWeight.W_600),
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Divider(height=1),
            _agent_msg_list,
            ft.Divider(height=1),
            ft.Row([
                _preset_menu,
                _agent_input,
                ft.IconButton(icon=ft.Icons.SEND, on_click=_on_agent_send, icon_size=20),
            ], spacing=6),
        ], spacing=4),
        width=_agent_panel_width,
        padding=ft.padding.Padding(left=8, top=12, right=8, bottom=12),
        border=ft.Border(left=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
    )
    _agent_panel_ref = agent_panel

    resize_handle = ft.GestureDetector(
        content=ft.Container(
            width=8,
            bgcolor=ft.Colors.OUTLINE_VARIANT,
            border_radius=4,
        ),
        mouse_cursor=ft.MouseCursor.RESIZE_LEFT_RIGHT,
        on_horizontal_drag_update=_on_agent_resize_update,
        on_horizontal_drag_end=_on_agent_resize_end,
    )

    page.add(
        ft.Column([
            top_nav_ref,
            ft.Row([
                ft.Stack([
                    container_project,
                    container_results,
                    container_settings,
                ], expand=True),
                resize_handle,
                agent_panel,
            ], expand=True),
        ], expand=True),
    )

    # 欢迎消息（必须在 page.add 之后，控件已挂载才能 update）
    send_agent_message(
        "你好！我是你的学术助理。\n\n"
        "• 在检索结果或文献库中，点击论文旁的 📖 按钮帮你精读论文\n"
        "• 多选几篇论文后，可以让我对比分析\n"
        "• 有任何研究相关问题，随时问我",
        role="agent",
    )

if __name__ == "__main__":
    ft.run(main)
