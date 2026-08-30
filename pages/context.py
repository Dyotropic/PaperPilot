"""跨页面共享的 UI 上下文 + 设计系统。

按 PHASE3_PLAN §6.4：页面函数通过参数注入所需回调，禁止跨文件直接访问
散落的全局变量。这里集中持有：
- 设计系统令牌（配色、字体、尺寸、圆角、阴影、间距）
- 共享常量（主题、边框辅助、apply_theme）
- AppState（全局业务状态）
- AppContext（跨页可变状态 + 回调注册表 + 由 app.py 注入的 Agent 面板回调）

app.py 创建 ctx 单例后，把 send_agent_message / set_agent_project 等 Agent
面板能力注入 ctx；各 pages/* 模块只依赖本模块，不反向 import app.py，
从而避免循环导入。
"""

import flet as ft


# ═══════════════════════════════════════════════════════════════════
# 设计系统令牌（Design Tokens）
# 目标：成熟工程项目的专业 UI —— 中性灰阶分层 + 克制 accent + 清晰层级
# ═══════════════════════════════════════════════════════════════════

# ── 字体：微软雅黑 UI（Windows 自带，UI 优化的现代中文字体，小字号清晰）──
FONT_FAMILY = "Microsoft YaHei UI"

# ── 字号阶梯 ──
FS_XS = 11   # 辅助说明、图注、次要信息
FS_SM = 12   # 次要正文、表格次要列
FS_MD = 13   # 正文、列表、表单
FS_LG = 14   # 强调正文、导航项、卡片副标题
FS_XL = 16   # 区块标题、页面副标题
FS_XXL = 18  # 页面标题
FS_HERO = 22 # 主标题

# ── 字重 ──
FW_REGULAR = ft.FontWeight.W_400
FW_MEDIUM = ft.FontWeight.W_500
FW_SEMIBOLD = ft.FontWeight.W_600
FW_BOLD = ft.FontWeight.W_700

# ── 圆角 ──
R_SM = 6
R_MD = 8
R_LG = 12
R_XL = 16

# ── 间距（8px 栅格）──
SP_XS = 4
SP_SM = 8
SP_MD = 12
SP_LG = 16
SP_XL = 24
SP_XXL = 32


# ── 主题定义 ──
# 每个主题含：label（中文名）、seed（accent 色）、light/dark 两套中性分层背景。
# 分层：app_bg（最底） → surface（卡片/面板） → surface_hi（悬停/抬高）。
THEMES = {
    "slate": {
        "label": "中性蓝",
        "seed": "#3B5BDB",
        "app_bg": "#EEF1F7", "surface": "#FFFFFF", "surface_hi": "#E9ECF4",
        "app_bg_dark": "#0F1420", "surface_dark": "#1A2233", "surface_hi_dark": "#232D42",
    },
    "mint": {
        "label": "薄荷绿",
        "seed": "#0F9D74",
        "app_bg": "#ECF4F1", "surface": "#FFFFFF", "surface_hi": "#DFEEE8",
        "app_bg_dark": "#0C1714", "surface_dark": "#152420", "surface_hi_dark": "#1C302A",
    },
    "ocean": {
        "label": "海蓝",
        "seed": "#1E63C8",
        "app_bg": "#EAF0F8", "surface": "#FFFFFF", "surface_hi": "#DEE9F6",
        "app_bg_dark": "#0C1524", "surface_dark": "#15233A", "surface_hi_dark": "#1C2E4A",
    },
    "sand": {
        "label": "暖沙",
        "seed": "#C25A1A",
        "app_bg": "#F6F0E8", "surface": "#FFFFFF", "surface_hi": "#EDE2D3",
        "app_bg_dark": "#1B1510", "surface_dark": "#28211A", "surface_hi_dark": "#332B22",
    },
    "dusk": {
        "label": "暮紫",
        "seed": "#7A4BC2",
        "app_bg": "#F3EFF8", "surface": "#FFFFFF", "surface_hi": "#E9E1F5",
        "app_bg_dark": "#161022", "surface_dark": "#221A36", "surface_hi_dark": "#2C2344",
    },
    "rose": {
        "label": "玫瑰",
        "seed": "#C2255C",
        "app_bg": "#F8EFF2", "surface": "#FFFFFF", "surface_hi": "#F1DFE6",
        "app_bg_dark": "#1D1015", "surface_dark": "#2B1A20", "surface_hi_dark": "#382129",
    },
}
DEFAULT_THEME = "slate"

# ── 中性文本色（不随主题变化，仅随明暗变化）──
TEXT_PRIMARY_LIGHT = "#1A1F2C"
TEXT_SECONDARY_LIGHT = "#5A6272"
TEXT_TERTIARY_LIGHT = "#8A92A3"
TEXT_PRIMARY_DARK = "#E8EBF2"
TEXT_SECONDARY_DARK = "#9AA3B5"
TEXT_TERTIARY_DARK = "#6B7488"

# ── 中性边框色 ──
BORDER_LIGHT = "#E2E6EE"
BORDER_DARK = "#2A3346"


def text_primary() -> str:
    from pages.context import ctx as _c
    return TEXT_PRIMARY_DARK if _c.state.dark_mode else TEXT_PRIMARY_LIGHT


def text_secondary() -> str:
    from pages.context import ctx as _c
    return TEXT_SECONDARY_DARK if _c.state.dark_mode else TEXT_SECONDARY_LIGHT


def text_tertiary() -> str:
    from pages.context import ctx as _c
    return TEXT_TERTIARY_DARK if _c.state.dark_mode else TEXT_TERTIARY_LIGHT


def border_color() -> str:
    from pages.context import ctx as _c
    return BORDER_DARK if _c.state.dark_mode else BORDER_LIGHT


def _current_theme() -> dict:
    from pages.context import ctx as _c
    return THEMES.get(_c.state.theme_name, THEMES[DEFAULT_THEME])


def seed_color() -> str:
    return _current_theme()["seed"]


def app_bg() -> str:
    from pages.context import ctx as _c
    t = _current_theme()
    return t["app_bg_dark"] if _c.state.dark_mode else t["app_bg"]


def surface() -> str:
    from pages.context import ctx as _c
    t = _current_theme()
    return t["surface_dark"] if _c.state.dark_mode else t["surface"]


def surface_hi() -> str:
    from pages.context import ctx as _c
    t = _current_theme()
    return t["surface_hi_dark"] if _c.state.dark_mode else t["surface_hi"]


def accent_container() -> str:
    """accent 的浅底容器色（用于选中态、强调底）。基于 seed 的柔和变体。"""
    from pages.context import ctx as _c
    seed = _current_theme()["seed"]
    if _c.state.dark_mode:
        return _blend(seed, _current_theme()["surface_dark"], 0.22)
    return _blend(seed, "#FFFFFF", 0.12)


def _blend(fg_hex: str, bg_hex: str, fg_ratio: float) -> str:
    """将 fg 按 fg_ratio 比例混入 bg，返回混合色 hex。"""
    def _hx(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    f, b = _hx(fg_hex), _hx(bg_hex)
    m = tuple(int(b[i] + (f[i] - b[i]) * fg_ratio) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*m)


def _border(color=None):
    """Flet 0.85 兼容的边框辅助函数。"""
    side = ft.BorderSide(1, color or border_color())
    return ft.Border(side, side, side, side)


def subtle_shadow():
    """卡片轻阴影（成熟工程界面的关键质感）。"""
    return ft.BoxShadow(
        spread_radius=0, blur_radius=16,
        color="rgba(15, 23, 42, 0.09)",
        offset=ft.Offset(0, 3),
    )


def card(content, padding=None, expand=None, width=None, height=None, visible=True):
    """标准卡片容器：surface 底 + 细边框 + 圆角 + 轻阴影。"""
    return ft.Container(
        content=content,
        bgcolor=surface(),
        border=_border(),
        border_radius=R_LG,
        padding=padding if padding is not None else SP_LG,
        shadow=subtle_shadow(),
        expand=expand, width=width, height=height, visible=visible,
    )


def apply_theme(page: ft.Page, theme_name: str, dark_mode: bool):
    """应用配色主题和夜间模式。"""
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    seed = theme["seed"]
    scaffold = theme["app_bg_dark"] if dark_mode else theme["app_bg"]
    dark_scaffold = theme["app_bg_dark"]

    # 用 seed 生成统一色彩方案，同时保留中性 scaffold 背景
    page.theme = ft.Theme(
        color_scheme_seed=seed,
        scaffold_bgcolor=scaffold,
        font_family=FONT_FAMILY,
    )
    page.dark_theme = ft.Theme(
        color_scheme_seed=seed,
        scaffold_bgcolor=dark_scaffold,
        font_family=FONT_FAMILY,
    )
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
        self.theme_name: str = DEFAULT_THEME
        self.dark_mode: bool = False


# ── 跨页上下文 ──
class AppContext:
    """集中持有跨页可变状态与回调注册表。"""

    def __init__(self):
        # 运行时
        self.page: ft.Page | None = None
        self.state = AppState()

        # 由 app.py 注入的 Agent 面板 / 导航能力
        self.send_agent_message = None
        self.trigger_compare_papers = None
        self.set_agent_project = None
        self.ai_service = None
        self.build_nav = None
        self.top_nav_ref = None
        self.switch_page = None  # app.py 注入 page_switcher；调用方需判 None

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

        # 文献库页注册的课题能力（左侧导航通过 ctx 触发）+ 侧栏项目子菜单
        self.library_select_project = None     # (pid) -> None
        self.library_new_project = None        # () -> None
        self.library_delete_project = None     # () -> None
        self.library_refresh_projects = None   # () -> None
        self.library_project_submenu = None    # ft.Column：侧栏课题子菜单（library_page 填充）
        self.library_project_submenu_wrap = None  # ft.Container：子菜单可见性包装（app.py 创建）
        self.selected_project_id = None        # 当前选中课题 id


ctx = AppContext()
