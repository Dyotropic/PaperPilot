"""跨页面共享的 UI 上下文。

按 PHASE3_PLAN §6.4：页面函数通过参数注入所需回调，禁止跨文件直接访问
散落的全局变量。这里集中持有：
- 共享常量（主题、边框辅助、apply_theme）
- AppState（全局业务状态）
- AppContext（跨页可变状态 + 回调注册表 + 由 app.py 注入的 Agent 面板回调）

app.py 创建 ctx 单例后，把 send_agent_message / set_agent_project 等 Agent
面板能力注入 ctx；各 pages/* 模块只依赖本模块，不反向 import app.py，
从而避免循环导入。
"""

import flet as ft


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


def _border(color):
    """Flet 0.85 兼容的边框辅助函数。"""
    side = ft.BorderSide(1, color)
    return ft.Border(side, side, side, side)


def apply_theme(page: ft.Page, theme_name: str, dark_mode: bool):
    """应用配色主题和夜间模式。"""
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    seed = theme["seed"]
    bg = theme["dark_bg"] if dark_mode else theme["light_bg"]
    page.theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=bg, font_family="Microsoft YaHei UI")
    page.dark_theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=theme["dark_bg"], font_family="Microsoft YaHei UI")
    page.theme_mode = ft.ThemeMode.DARK if dark_mode else ft.ThemeMode.LIGHT
    page.update()


# ── 全局业务状态 ──
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


# ── 跨页上下文 ──
class AppContext:
    """集中持有跨页可变状态与回调注册表。"""

    def __init__(self):
        # 运行时
        self.page: ft.Page | None = None
        self.state = AppState()
        self.containers: dict[str, ft.Container] = {}

        # 由 app.py 注入的 Agent 面板 / 导航能力
        self.send_agent_message = None
        self.trigger_compare_papers = None
        self.set_agent_project = None
        self.ai_service = None
        self.build_nav = None
        self.top_nav_ref = None

        # 检索页注册给 Agent 的回调 + 多选状态
        self.search_actions: dict = {}
        self.search_selected_ids: set = set()
        self.search_checkboxes: list = []
        self.search_select_count_ref = None
        self.search_compare_btn = None

        # 文献库页注册的回调
        self.refresh_library = None
        self.refresh_paper_list = None
        self.clear_library_ui = None

        # Agent 自动上下文（检索/文献库页选中论文后同步）
        self.agent_paper_selection: list = []

        # 当前 Agent 关联的课题（由 app.py 的 set_agent_project 镜像写入，供页面读取）
        self.agent_project_id = None
        self.agent_project_name = ""
        self.agent_topic_desc = ""


ctx = AppContext()
