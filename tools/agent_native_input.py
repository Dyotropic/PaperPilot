"""Native input driver restricted to the isolated validation window."""
import ctypes
from ctypes import wintypes as w
import json
import sys
import time
import re

u = ctypes.windll.user32
u.FindWindowW.restype = w.HWND
u.GetForegroundWindow.restype = w.HWND
u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
u.SetForegroundWindow.argtypes = [w.HWND]
u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
u.BringWindowToTop.argtypes = [w.HWND]
u.SetActiveWindow.argtypes = [w.HWND]
u.SetActiveWindow.restype = w.HWND
u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
title = sys.argv[2] if len(sys.argv) > 2 else "PaperPilot Native Input 20261001"
assert re.fullmatch(r"PaperPilot Native (?:Input 20261001|Stop \d+|Context \d+|Attachments \d+|Team \d+)", title), "Unexpected validation window"
hwnd = u.FindWindowW(None, title)
assert hwnd
actions = json.loads(sys.argv[1])
dialog_mode = bool(actions and actions[0][0] in {"dialog_files", "dialog_folder"})
if dialog_mode:
    assert title.startswith("PaperPilot Native Attachments ")
    parent_pid = w.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(parent_pid))
    choices = []
    enum_callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def find_dialog(window, unused):
        name = ctypes.create_unicode_buffer(300)
        u.GetWindowTextW(window, name, 300)
        pid = w.DWORD()
        u.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value == parent_pid.value and name.value in {"选择资料文件", "选择照片", "选择提供给 Agent 的资料文件夹"}:
            choices.append(window)
        return True
    u.EnumWindows(enum_callback(find_dialog), 0)
    assert len(choices) == 1, "Expected the validation app's own native file dialog"
    hwnd = choices[0]
u.ShowWindow(hwnd, 9)
u.SetWindowPos(hwnd, w.HWND(-1), 0, 0, 0, 0, 1 | 64)
current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
other_threads = {u.GetWindowThreadProcessId(u.GetForegroundWindow(), None),
                 u.GetWindowThreadProcessId(hwnd, None)} - {current_thread, 0}
attached = [thread for thread in other_threads if u.AttachThreadInput(current_thread, thread, True)]
try:
    for _ in range(5):
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.SetActiveWindow(hwnd)
        time.sleep(.12)
        if u.GetForegroundWindow() == hwnd:
            break
finally:
    for thread in attached:
        u.AttachThreadInput(current_thread, thread, False)
import atexit
atexit.register(lambda: u.SetWindowPos(hwnd, w.HWND(-2), 0, 0, 0, 0, 3))
u.SetForegroundWindow(hwnd)
time.sleep(.1)
assert u.GetForegroundWindow() == hwnd, "FOREGROUND_NOT_ACQUIRED"
rect = w.RECT()
assert u.GetWindowRect(hwnd, ctypes.byref(rect))
assert rect.right - rect.left > (300 if dialog_mode else 1000)

class Mouse(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("data", w.DWORD),
                ("flags", w.DWORD), ("time", w.DWORD), ("extra", ctypes.c_size_t)]
class Keyboard(ctypes.Structure):
    _fields_ = [("vk", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", ctypes.c_size_t)]
class Union(ctypes.Union):
    _fields_ = [("mouse", Mouse), ("keyboard", Keyboard)]
class Input(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("data", Union)]
u.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]

def key(vk, flags=0, scan=0):
    assert u.GetForegroundWindow() == hwnd
    item = Input(type=1, data=Union(keyboard=Keyboard(vk, scan, flags, 0, 0)))
    assert u.SendInput(1, ctypes.byref(item), ctypes.sizeof(item)) == 1

for action in actions:
    assert u.GetForegroundWindow() == hwnd
    if action[0] == "click":
        x, y = action[1:]
        assert 0 < x < rect.right - rect.left and 45 < y < rect.bottom - rect.top
        assert u.SetCursorPos(rect.left + x, rect.top + y)
        actual = w.POINT()
        assert u.GetCursorPos(ctypes.byref(actual))
        assert (actual.x, actual.y) == (rect.left + x, rect.top + y), "Target clipped by desktop bounds"
        # Allow the desktop/Flutter pointer target to settle after foreground
        # acquisition and cursor movement before injecting the button pair.
        time.sleep(.2)
        down = Input(type=0, data=Union(mouse=Mouse(0, 0, 0, 2, 0, 0)))
        up = Input(type=0, data=Union(mouse=Mouse(0, 0, 0, 4, 0, 0)))
        assert u.SendInput(1, ctypes.byref(down), ctypes.sizeof(down)) == 1
        time.sleep(.06)
        assert u.SendInput(1, ctypes.byref(up), ctypes.sizeof(up)) == 1
    elif action[0] == "text":
        for c in action[1]:
            key(0, 4, ord(c)); key(0, 6, ord(c))
    elif action[0] == "selectall":
        key(0x11); key(0x41); key(0x41, 2); key(0x11, 2)
    elif action[0] in ("enter", "shiftenter"):
        if action[0] == "shiftenter": key(0x10)
        key(0x0D); key(0x0D, 2)
        if action[0] == "shiftenter": key(0x10, 2)
    elif action[0] in ("escape", "down", "up", "tab"):
        vk = {"escape": 0x1B, "down": 0x28, "up": 0x26, "tab": 0x09}[action[0]]
        key(vk); key(vk, 2)
    elif action[0] in {"dialog_files", "dialog_folder"}:
        from pathlib import Path
        paths = action[1] if isinstance(action[1], list) else [action[1]]
        assert all(Path(p).resolve().is_relative_to(Path(__file__).resolve().parents[1] / ".validation-search-20260922") for p in paths)
        if action[0] == "dialog_folder":
            key(0x11); key(0x4C); key(0x4C, 2); key(0x11, 2)
        else:
            key(0x12); key(0x4E); key(0x4E, 2); key(0x12, 2)
        key(0x11); key(0x41); key(0x41, 2); key(0x11, 2)
        value = paths[0] if action[0] == "dialog_folder" else " ".join('"' + p + '"' for p in paths)
        for c in value:
            key(0, 4, ord(c)); key(0, 6, ord(c))
        key(0x0D); key(0x0D, 2)
        if action[0] == "dialog_folder":
            time.sleep(.6)
            # Folder navigation leaves the address bar focused. Activate the
            # native dialog's OK button, restricted to this verified process.
            u.GetDlgItem.argtypes = [w.HWND, ctypes.c_int]
            u.GetDlgItem.restype = w.HWND
            button = u.GetDlgItem(hwnd, 1)
            assert button, "Native folder dialog has no OK button"
            u.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            u.SendMessageW(button, 0x00F5, 0, 0)
    else:
        raise ValueError("Unknown native input")
    # Give native popup animations/event delivery time to settle before a
    # second click; a fast pair can close the menu without selecting its row.
    time.sleep(1 if action[0] == "click" else .4)
print("Native inputs delivered:", len(json.loads(sys.argv[1])))
