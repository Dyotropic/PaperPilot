"""Capture an isolated Flet validation window using its own native surface."""
import asyncio
import ctypes
from ctypes import wintypes
from pathlib import Path


async def screenshot(page, name):
    await page.window.to_front()
    await asyncio.sleep(.7)
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    hwnd = user32.FindWindowW(None, page.title)
    rect = wintypes.RECT()
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect))
    print("UI capture bounds", rect.right-rect.left, rect.bottom-rect.top, flush=True)
    # Capture the application's own surface, even when another window covers it.
    gdi = ctypes.windll.gdi32
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    width, height = rect.right-rect.left, rect.bottom-rect.top
    source_dc = user32.GetWindowDC(hwnd)
    dc = gdi.CreateCompatibleDC(source_dc)
    bitmap = gdi.CreateCompatibleBitmap(source_dc, width, height)
    old = gdi.SelectObject(dc, bitmap)
    try:
        assert user32.PrintWindow(hwnd, dc, 2)
        class Header(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                        ("height", wintypes.LONG), ("planes", wintypes.WORD),
                        ("bit_count", wintypes.WORD), ("compression", wintypes.DWORD),
                        ("size_image", wintypes.DWORD), ("xppm", wintypes.LONG),
                        ("yppm", wintypes.LONG), ("used", wintypes.DWORD), ("important", wintypes.DWORD)]
        header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                 wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        assert gdi.GetDIBits(dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
        from PIL import Image
        image = Image.frombuffer("RGB", (width,height), buffer.raw, "raw", "BGRX", 0, 1)
        image.save(Path.cwd()/name)
    finally:
        gdi.SelectObject(dc, old)
        gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi.DeleteDC.argtypes = [wintypes.HDC]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)
        user32.ReleaseDC(hwnd, source_dc)
        user32.SetThreadDpiAwarenessContext(previous_dpi)
