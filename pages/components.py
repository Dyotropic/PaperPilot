"""通用 UI 组件（跨页面复用）。

提供 VSCode 式拖拽调宽分隔手柄：按住手柄水平拖动即可调整相邻面板宽度，
拖拽结束触发 on_end 回调（用于持久化到 config）。
另含 Windows PowerShell 原生文件对话框统一入口（D6 收敛）。
"""
import os
import sys

import flet as ft

from pages.context import border_color

# ── Windows 原生对话框（PowerShell 焦点修复样板，全仓库唯一份）──
_PS_FOCUS_HELPER = (
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


def run_ps_script(script: str, inject_focus: bool = True,
                  timeout: int = 120) -> str:
    """以 PowerShell 运行一段 UI 脚本，返回其 stdout（Windows 原生对话框）。

    统一处理：前台窗口授权 + 焦点修复样板注入 + 临时脚本文件 + 清理。
    script 用 `$owner=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}`
    占位 owner 创建，注入时替换为完整焦点修复；调用方仅需提供对话框本身逻辑。
    """
    import tempfile
    import subprocess
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass

    if inject_focus:
        script = script.replace(
            '$owner=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}\n',
            _PS_FOCUS_HELPER,
        )
        script = script.replace('$owner.Dispose()\n', '$owner.Close()\n$owner.Dispose()\n')

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8-sig")
    tmp.write(script)
    tmp.close()
    try:
        r = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", tmp.name],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.stderr:
            print(f"[run_ps_script] ps stderr: {r.stderr[:200]}", flush=True)
        return r.stdout.strip()
    except Exception as ex:
        print(f"[run_ps_script] error: {ex}", flush=True)
        return ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def is_shift_pressed() -> bool:
    """读取 Shift 键的实时物理按下状态（仅 Windows；其它平台或失败返回 False）。

    Flet 控件事件不携带键盘修饰符，而勾选事件到达 Python 时用户通常仍按着
    Shift（点击与事件处理间隔为毫秒级），故用 GetAsyncKeyState 判定 Shift+点击。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except Exception:
        return False


def safe_update(ctrl) -> None:
    """静默更新控件；控件已 unmount（RuntimeError）时忽略。

    供跨线程回调、Timer、轮询收尾等"控件可能已被销毁"的路径使用。
    """
    if ctrl is None:
        return
    try:
        ctrl.update()
    except RuntimeError:
        pass
    except Exception:
        pass


def open_dialog(page, dlg) -> None:
    """追加到 overlay 并打开对话框的统一入口。"""
    page.overlay.append(dlg)
    dlg.open = True
    page.update()


def close_dialog(page, dlg) -> None:
    """关闭并从 overlay 移除对话框（修复对象泄漏）。"""
    dlg.open = False
    try:
        page.overlay.remove(dlg)
    except ValueError:
        pass
    page.update()


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
