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
assert re.fullmatch(r"PaperPilot Native (?:Input 20261001|Stop \d+)", title), "Unexpected validation window"
hwnd = u.FindWindowW(None, title)
assert hwnd
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
assert rect.right - rect.left > 1000

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

for action in json.loads(sys.argv[1]):
    assert u.GetForegroundWindow() == hwnd
    if action[0] == "click":
        x, y = action[1:]
        assert 0 < x < rect.right - rect.left and 45 < y < rect.bottom - rect.top
        assert u.SetCursorPos(rect.left + x, rect.top + y)
        actual = w.POINT()
        assert u.GetCursorPos(ctypes.byref(actual))
        assert (actual.x, actual.y) == (rect.left + x, rect.top + y), "Target clipped by desktop bounds"
        u.mouse_event(2, 0, 0, 0, 0)
        u.mouse_event(4, 0, 0, 0, 0)
    elif action[0] == "text":
        for c in action[1]:
            key(0, 4, ord(c)); key(0, 6, ord(c))
    elif action[0] == "selectall":
        key(0x11); key(0x41); key(0x41, 2); key(0x11, 2)
    elif action[0] in ("enter", "shiftenter"):
        if action[0] == "shiftenter": key(0x10)
        key(0x0D); key(0x0D, 2)
        if action[0] == "shiftenter": key(0x10, 2)
    else:
        raise ValueError("Unknown native input")
    time.sleep(.4)
print("Native inputs delivered:", len(json.loads(sys.argv[1])))
