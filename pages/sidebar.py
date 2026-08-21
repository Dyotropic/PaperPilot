"""左侧导航栏：品牌区 + 文献(课题子菜单/操作) + 检索 + 设置。

从 app.py 迁入（PHASE3_PLAN §6.4 拆分）。切页经 ctx.switch_page（app.py 注入）。
"""
import flet as ft

from pages.context import (
    ctx,
    FS_XS, FS_LG, FS_XXL,
    FW_BOLD, FW_REGULAR, FW_SEMIBOLD,
    R_MD, SP_SM, SP_MD, SP_LG,
    text_primary, text_secondary, text_tertiary, border_color,
    seed_color, surface, accent_container,
)

# 文献课题子菜单展开状态（跨页面切换保持）
_project_submenu_expanded = True

_wenxian_expand_btn = None


def _toggle_project_submenu(e=None):
    """展开/折叠文献页下的课题子菜单。"""
    global _project_submenu_expanded
    _project_submenu_expanded = not _project_submenu_expanded
    if ctx.library_project_submenu_wrap is not None:
        ctx.library_project_submenu_wrap.visible = _project_submenu_expanded
        try:
            ctx.library_project_submenu_wrap.update()
        except RuntimeError:
            pass
    if _wenxian_expand_btn is not None:
        _wenxian_expand_btn.icon = (ft.Icons.EXPAND_MORE if _project_submenu_expanded
                                    else ft.Icons.CHEVRON_RIGHT)
        try:
            _wenxian_expand_btn.update()
        except RuntimeError:
            pass


def ensure_submenu_expanded():
    """切到文献页时保持子菜单展开（供 app.py page_switcher 调用）。"""
    global _project_submenu_expanded
    if not _project_submenu_expanded:
        _project_submenu_expanded = True
        if ctx.library_project_submenu_wrap is not None:
            ctx.library_project_submenu_wrap.visible = True
            try:
                ctx.library_project_submenu_wrap.update()
            except RuntimeError:
                pass


def _project_actions_menu():
    """文献按钮旁的省略号菜单：新建/删除/刷新课题。"""
    def _safe(cb):
        try:
            if cb:
                cb()
        except Exception:
            pass

    def _new(e): _safe(ctx.library_new_project)
    def _delete(e): _safe(ctx.library_delete_project)
    def _refresh(e): _safe(ctx.library_refresh_projects)

    return ft.PopupMenuButton(
        icon=ft.Icons.MORE_HORIZ,
        icon_size=18,
        icon_color=text_tertiary(),
        tooltip="课题操作",
        items=[
            ft.PopupMenuItem(content=ft.Row([
                ft.Icon(ft.Icons.ADD, size=16, color=text_secondary()),
                ft.Text("新建课题", size=FS_LG),
            ], spacing=SP_SM), on_click=_new),
            ft.PopupMenuItem(content=ft.Row([
                ft.Icon(ft.Icons.DELETE, size=16, color=text_secondary()),
                ft.Text("删除课题", size=FS_LG),
            ], spacing=SP_SM), on_click=_delete),
            ft.PopupMenuItem(content=ft.Row([
                ft.Icon(ft.Icons.REFRESH, size=16, color=text_secondary()),
                ft.Text("刷新", size=FS_LG),
            ], spacing=SP_SM), on_click=_refresh),
        ],
    )


def build_sidebar(active_idx: int) -> ft.Column:
    """生成左侧垂直导航栏：品牌区 + 文献(课题子菜单/操作) + 检索 + 设置。"""
    global _wenxian_expand_btn
    accent = seed_color()

    def on_nav_click(e):
        if ctx.switch_page:
            ctx.switch_page(e.control.data)

    def _indicator(is_active):
        return ft.Container(width=3, height=18, border_radius=2,
                            bgcolor=accent if is_active else None)

    def _label_area(label, icon, idx, is_active):
        """图标+文字区域，点击切换页面。"""
        return ft.Container(
            content=ft.Row([
                ft.Icon(icon, size=18,
                        color=accent if is_active else text_secondary()),
                ft.Text(label, size=FS_LG,
                       weight=FW_SEMIBOLD if is_active else FW_REGULAR,
                       color=accent if is_active else text_primary()),
            ], spacing=SP_MD),
            on_click=on_nav_click,
            data=idx,
            expand=True,
        )

    # ── 文献项：指示条 + 图标/文字(切页) + 展开箭头 + 省略号菜单 ──
    is_wx = active_idx == 1
    _wenxian_expand_btn = ft.IconButton(
        icon=ft.Icons.EXPAND_MORE if _project_submenu_expanded else ft.Icons.CHEVRON_RIGHT,
        icon_size=18, icon_color=text_tertiary(),
        tooltip="展开/折叠课题列表",
        on_click=_toggle_project_submenu,
    )
    wenxian_item = ft.Container(
        content=ft.Row([
            _indicator(is_wx),
            _label_area("文献", ft.Icons.FORMAT_LIST_NUMBERED, 1, is_wx),
            _wenxian_expand_btn,
            _project_actions_menu(),
        ], spacing=0),
        padding=ft.padding.Padding(left=SP_SM, top=6, right=SP_MD, bottom=6),
        border_radius=R_MD,
        bgcolor=accent_container() if is_wx else None,
    )

    # 课题子菜单（持久实例，由 library_page 的 refresh_project_list 填充）
    if ctx.library_project_submenu is None:
        ctx.library_project_submenu = ft.Column(controls=[], spacing=2)
        ctx.library_project_submenu_wrap = ft.Container(
            content=ctx.library_project_submenu,
            padding=ft.padding.Padding(left=SP_MD, top=2, bottom=2),
            visible=_project_submenu_expanded,
        )

    jiansuo_item = ft.Container(
        content=ft.Row([
            _indicator(active_idx == 0),
            _label_area("检索", ft.Icons.SEARCH, 0, active_idx == 0),
        ], spacing=0),
        padding=ft.padding.Padding(left=SP_SM, top=6, right=SP_MD, bottom=6),
        border_radius=R_MD,
        bgcolor=accent_container() if active_idx == 0 else None,
    )
    shezhi_item = ft.Container(
        content=ft.Row([
            _indicator(active_idx == 2),
            _label_area("设置", ft.Icons.SETTINGS, 2, active_idx == 2),
        ], spacing=0),
        padding=ft.padding.Padding(left=SP_SM, top=6, right=SP_MD, bottom=6),
        border_radius=R_MD,
        bgcolor=accent_container() if active_idx == 2 else None,
    )

    return ft.Column([
        # 品牌区
        ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Container(
                        width=34, height=34, border_radius=R_MD,
                        bgcolor=accent,
                        alignment=ft.Alignment(0, 0),
                        content=ft.Icon(ft.Icons.SCIENCE, size=20, color="#FFFFFF"),
                    ),
                    ft.Column([
                        ft.Text("PaperPilot", size=FS_XXL, weight=FW_BOLD,
                                color=text_primary()),
                        ft.Text("智能文献工作流", size=FS_XS, color=text_tertiary()),
                    ], spacing=1),
                ], spacing=SP_MD),
            ]),
            padding=ft.padding.Padding(left=SP_MD, top=SP_LG, bottom=SP_LG),
        ),
        ft.Divider(height=1, color=border_color()),
        ft.Container(
            content=ft.Column([
                wenxian_item,
                ctx.library_project_submenu_wrap,
                jiansuo_item,
                shezhi_item,
            ], spacing=4),
            padding=ft.padding.Padding(left=SP_MD, top=SP_SM, right=SP_MD, bottom=SP_SM),
        ),
        # 底部版本信息
        ft.Container(expand=True),
        ft.Container(
            content=ft.Text("v1.0  Phase 2", size=FS_XS, color=text_tertiary()),
            padding=ft.padding.Padding(left=SP_LG, top=SP_SM, right=SP_LG, bottom=SP_SM),
        ),
    ], spacing=0, expand=True)
