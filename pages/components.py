"""通用 UI 组件（跨页面复用）。

提供 VSCode 式拖拽调宽分隔手柄：按住手柄水平拖动即可调整相邻面板宽度，
拖拽结束触发 on_end 回调（用于持久化到 config）。
"""
import flet as ft

from pages.context import border_color


def make_resize_handle(get_width, set_width, min_w, max_w, on_end=None,
                       side: str = "right") -> ft.GestureDetector:
    """构建水平拖拽调宽手柄（竖条样式，RESIZE_LEFT_RIGHT 光标）。

    Args:
        get_width: () -> int，当前面板宽度（拖拽起点基准）
        set_width: (int) -> None，写入新宽度（内部自行 update 控件）
        min_w: 最小宽度
        max_w: 最大宽度，数字或 () -> int（如随窗口宽度变化的面板上限）
        on_end: 拖拽结束回调（无参），用于持久化宽度
        side: 面板位于分隔线的哪一侧——"right"（面板在线右侧，向左拖变宽，
              如 Agent 面板）或 "left"（面板在线左侧，向右拖变宽，如左侧导航）

    实现说明：delta 取拖拽起点与当前 x 的差值，符号由 side 决定；
    用 dragging 标志而非 start_x==0 判定起点，
    避免光标恰好在 x=0 附近开始拖动时的基准错误。
    """
    if side not in ("left", "right"):
        raise ValueError(f"side 必须是 'left' 或 'right'，收到 {side!r}")
    sign = 1 if side == "right" else -1
    _drag = {"active": False, "start_x": 0.0, "start_w": 0}

    def _on_update(e: ft.DragUpdateEvent):
        if not _drag["active"]:
            _drag["active"] = True
            _drag["start_x"] = e.global_position.x
            _drag["start_w"] = get_width()
        upper = max_w() if callable(max_w) else max_w
        delta = sign * (_drag["start_x"] - e.global_position.x)
        set_width(max(min_w, min(int(_drag["start_w"] + delta), upper)))

    def _on_end(e):
        was_active = _drag["active"]
        _drag["active"] = False
        if was_active and on_end:
            on_end()

    return ft.GestureDetector(
        content=ft.Container(
            width=8,
            bgcolor=border_color(),
            border_radius=4,
        ),
        mouse_cursor=ft.MouseCursor.RESIZE_LEFT_RIGHT,
        on_horizontal_drag_update=_on_update,
        on_horizontal_drag_end=_on_end,
    )


def clamp_width(value, min_w: int, max_w: int, default: int) -> int:
    """把配置读到的宽度钳制到合法区间（非法值回退默认）。"""
    try:
        return max(min_w, min(int(value), max_w))
    except (TypeError, ValueError):
        return default
