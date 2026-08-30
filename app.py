"""PaperPilot - 面向课题攻关的可解释智能文献工作流系统。

入口文件：页面已按 PHASE3_PLAN §6.4 拆分到 pages/ 包。
此处保留：日志配置、page_switcher、main()（配置加载 + ctx 注入 + 布局组装）。
Agent 面板在 pages/agent_panel.py，左侧导航在 pages/sidebar.py；
本文件再导出冻结接口 send_agent_message / set_agent_project。
"""
import logging

import flet as ft

from pages.context import (
    ctx, THEMES, DEFAULT_THEME, apply_theme,
    SP_LG, SP_XL,
    text_secondary, border_color, app_bg, surface,
)
from pages.agent_panel import (
    send_agent_message, set_agent_project, build_agent_panel,
    _trigger_compare_papers,  # noqa: F401  再导出保持模块级可访问
)
from pages.sidebar import build_sidebar, ensure_submenu_expanded
from pages.components import clamp_width, make_resize_handle
from pages.search_page import build_search_page
from pages.library_page import build_library_page
from pages.settings_page import (
    build_settings_page, arxiv_switch, openalex_switch, europepmc_switch,
    max_results_slider, top_k_slider, ce_candidates_slider,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s.%(levelname)s: %(message)s",
    stream=__import__("sys").stdout,
)
logger = logging.getLogger(__name__)

state = ctx.state

nav_ref: ft.Container | None = None
container_project: ft.Container | None = None
container_results: ft.Container | None = None
container_settings: ft.Container | None = None

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

    if idx == 1:
        # 切到文献页：展开课题子菜单 + 刷新课题列表
        ensure_submenu_expanded()
        if ctx.refresh_library is not None:
            ctx.refresh_library()

    nav_ref.content = build_sidebar(idx)
    containers[prev].update()
    containers[idx].update()
    nav_ref.update()


# ── 应用入口 ──
def main(page: ft.Page):
    global nav_ref
    global container_project, container_results, container_settings

    ctx.page = page
    page.title = "PaperPilot"
    page.window.width = 1200
    page.window.height = 750
    page.window.min_width = 900
    page.window.min_height = 500
    # 页面底部留白，避免内容贴底
    page.padding = ft.padding.Padding(left=0, top=0, right=0, bottom=SP_LG)

    # 注入 Agent 面板 / 导航能力到 ctx
    ctx.send_agent_message = send_agent_message
    ctx.trigger_compare_papers = _trigger_compare_papers
    ctx.set_agent_project = set_agent_project
    ctx.build_nav = build_sidebar
    ctx.switch_page = page_switcher

    # 加载已保存的所有设置
    from paperpilot.config import load_config
    cfg = load_config()
    ui_config = cfg.get("ui", {})
    state.theme_name = ui_config.get("theme", DEFAULT_THEME)
    state.dark_mode = ui_config.get("dark_mode", False)
    apply_theme(page, state.theme_name, state.dark_mode)

    # 恢复搜索/数据源设置（钳制到滑条合法区间，防止手改 config 越界导致渲染崩溃）
    def _clamp_slider(v, lo, hi) -> int:
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return lo

    search_cfg = cfg.get("search", {})
    if search_cfg.get("max_results"):
        max_results_slider.value = _clamp_slider(search_cfg["max_results"], 100, 500)
    if search_cfg.get("top_k"):
        top_k_slider.value = _clamp_slider(search_cfg["top_k"], 10, 200)
    if search_cfg.get("ce_candidates"):
        ce_candidates_slider.value = _clamp_slider(search_cfg["ce_candidates"], 10, 200)

    ds_cfg = cfg.get("data_sources", {})
    if "arxiv" in ds_cfg:
        arxiv_switch.value = bool(ds_cfg["arxiv"])
    if "openalex" in ds_cfg:
        openalex_switch.value = bool(ds_cfg["openalex"])
    if "europepmc" in ds_cfg:
        europepmc_switch.value = bool(ds_cfg["europepmc"])

    # 左侧导航栏（内容后续由 page_switcher 动态替换；宽度可拖拽，配置持久化）
    _NAV_MIN, _NAV_MAX, _NAV_DEFAULT = 150, 320, 190
    sidebar_w = clamp_width(ui_config.get("sidebar_width", _NAV_DEFAULT),
                            _NAV_MIN, _NAV_MAX, _NAV_DEFAULT)
    nav_ref = ft.Container(
        content=build_sidebar(1),
        width=sidebar_w,
        bgcolor=surface(),
        border=ft.Border(right=ft.BorderSide(1, border_color())),
    )
    ctx.top_nav_ref = nav_ref

    def _set_nav_width(w: int):
        nav_ref.width = w
        try:
            nav_ref.update()
        except Exception:
            pass

    def _save_nav_width():
        try:
            from paperpilot.config import save_config
            save_config({"ui": {"sidebar_width": nav_ref.width}})
        except Exception:
            pass

    nav_resize_handle = make_resize_handle(
        lambda: nav_ref.width, _set_nav_width, _NAV_MIN, _NAV_MAX,
        on_end=_save_nav_width, side="left",
    )

    container_project = ft.Container(
        content=build_search_page(ctx), visible=False, expand=True,
        bgcolor=app_bg(),
        padding=ft.padding.Padding(left=SP_XL, top=SP_LG, right=SP_XL, bottom=SP_XL),
    )
    container_results = ft.Container(
        content=build_library_page(ctx), visible=True, expand=True,
        bgcolor=app_bg(),
        padding=ft.padding.Padding(left=SP_XL, top=SP_LG, right=SP_XL, bottom=SP_XL),
    )
    container_settings = ft.Container(
        content=build_settings_page(ctx), visible=False, expand=True,
        bgcolor=app_bg(),
        padding=ft.padding.Padding(left=SP_XL, top=SP_LG, right=SP_XL, bottom=SP_XL),
    )

    # ── Agent 对话面板 ──
    agent_panel, resize_handle = build_agent_panel()

    page.add(
        ft.Row([
            nav_ref,
            nav_resize_handle,
            ft.Stack([
                container_project,
                container_results,
                container_settings,
            ], expand=True),
            resize_handle,
            agent_panel,
        ], expand=True),
    )

    # 回收站 7 天自动清理（clean_recycle 此前从未接线，USER_GUIDE 承诺过）：
    # 启动时后台静默清一次，不阻塞窗口出现，失败不影响主流程
    import threading as _threading

    def _startup_clean_recycle():
        try:
            from paperpilot import repo_manager as _rm
            n = _rm.clean_recycle()
            if n:
                print(f"[repo] 回收站清理 {n} 个过期项目", flush=True)
        except Exception as _ex:
            print(f"[repo] 回收站清理失败（忽略）: {_ex}", flush=True)

    _threading.Thread(target=_startup_clean_recycle, daemon=True).start()

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
