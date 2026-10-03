"""Native app/settings callbacks and real loopback SDK calls, no paid models.

Controls are driven via callbacks; physical mouse/keyboard input is not claimed.
Run through tools/run_validation.py to protect production configuration and data.
"""
import asyncio
import copy
import json
import os
import ctypes
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

if not os.environ.get("PAPERPILOT_VALIDATION_ROOT"):
    raise SystemExit("Run with tools/run_validation.py to protect user configuration and data")

import flet as ft
import app
from pages.context import ctx
from paperpilot.config import load_config
from paperpilot.llm_client import MODEL_CATALOG, PROVIDERS
from tools.validate_llm_providers import transport, completion, response, configure

failures = []
# The native process must also keep its profile/cache writes on E:.
work = Path(os.environ["PAPERPILOT_VALIDATION_ROOT"])
os.environ.update(USERPROFILE=str(work / "home"), LOCALAPPDATA=str(work / "localapp"), APPDATA=str(work / "appdata"))


async def screenshot(page, name):
    """Capture visible pixels of this owned foreground window (Flutter is GPU rendered)."""
    await page.window.to_front()
    await asyncio.sleep(1.5)
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    hwnd = None
    try:
        hwnd = user32.FindWindowW(None, page.title)
        assert hwnd, "Owned validation window was not found"
        # Flutter's to_front() alone does not acquire foreground focus on Windows.
        # Reuse the bounded Win32 focus sequence from agent_native_input.py;
        # no mouse/keyboard events or other app content are captured.
        user32.ShowWindow(hwnd, 9)
        user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 3 | 64)
        thread = ctypes.windll.kernel32.GetCurrentThreadId()
        others = {user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None),
                  user32.GetWindowThreadProcessId(hwnd, None)} - {thread, 0}
        attached = [other for other in others if user32.AttachThreadInput(thread, other, True)]
        try:
            for _ in range(5):
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                await asyncio.sleep(.15)
                if user32.GetForegroundWindow() == hwnd:
                    break
        finally:
            for other in attached:
                user32.AttachThreadInput(thread, other, False)
        assert user32.GetForegroundWindow() == hwnd, "Validation window must be foreground"
        await asyncio.sleep(.5)
        rect = wintypes.RECT()
        assert user32.GetClientRect(hwnd, ctypes.byref(rect))
        origin = wintypes.POINT(rect.left, rect.top)
        assert user32.ClientToScreen(hwnd, ctypes.byref(origin))
        from PIL import ImageGrab
        # Invisible DWM borders and rounded corners can expose background apps.
        # Stay inside this window's opaque client area.
        inset = 8
        picture = ImageGrab.grab(bbox=(origin.x + inset, origin.y + inset,
            origin.x + rect.right - inset, origin.y + rect.bottom - inset), all_screens=True)
        assert len(picture.getcolors(picture.width * picture.height)) > 100, "Blank window capture"
        picture.save(Path.cwd() / name)
    finally:
        if hwnd:
            user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 3)
        user32.SetThreadDpiAwarenessContext(previous)


def descendants(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from descendants(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from descendants(content)


async def validate(page):
    def route(path, body):
        return 200, response(body["model"], "OK") if path.endswith("/responses") else completion(body["model"], "OK")
    with transport(route) as (base, captured, *_):
        configure("deepseek", base)
        app.main(page)
        page.title = f"PaperPilot Native LLM Settings {os.getpid()}"
        page.window.width, page.window.height = 1100, 700
        ctx.switch_page(2)
        page.update()
        await asyncio.sleep(1)
        all_controls = [c for root in page.controls for c in descendants(root)]
        provider = next(c for c in all_controls if isinstance(c, ft.Dropdown)
            and {o.key for o in c.options} == set(PROVIDERS))
        models = next(c for c in all_controls if isinstance(c, ft.Dropdown)
            and "deepseek-flash" in {o.key for o in c.options})
        def field(label):
            return next(c for c in all_controls if isinstance(c, ft.TextField) and c.label == label)
        def button(label):
            return next(c for c in all_controls if isinstance(c, (ft.FilledButton, ft.FilledTonalButton, ft.TextButton))
                and isinstance(c.content, ft.Text) and c.content.value == label)
        def choose(value):
            provider.value = value
            provider.on_select(SimpleNamespace(control=provider))
            page.update()
        key, endpoint = field("API Key"), field("Base URL（可选）")
        save, test = button("保存"), button("测试")
        async def connection(model):
            test.on_click(SimpleNamespace(control=test))
            for _ in range(80):
                await asyncio.sleep(.05)
                if any(isinstance(c, ft.Text) and c.value == f"连接成功（模型 {model}）" for c in all_controls):
                    return
            raise AssertionError("Native connection-test result was not delivered to the page")
        try:
            for name in PROVIDERS:
                choose(name)
                assert {m for m, _ in MODEL_CATALOG[name]} <= {o.key for o in models.options}
            choose("gemini")
            assert key.password and "aistudio" in key.hint_text
            assert models.value == "gemini-3.8-flash"
            key.value, endpoint.value = "synthetic-gemini-key", base
            page.update()
            await connection(models.value)
            save.on_click(SimpleNamespace(control=save))
            assert load_config()["llm"]["provider"] == "gemini"
            assert load_config()["llm"]["api_keys"]["deepseek"] == "synthetic-key"
            await asyncio.sleep(2)
            await screenshot(page, "native-llm-gemini.png")
            choose("openai")
            assert models.value == "gpt-6.1-sol" and not key.value
            key.value, endpoint.value = "synthetic-openai-key", base
            page.update()
            await connection(models.value)
            save.on_click(SimpleNamespace(control=save))
            assert load_config()["llm"]["api_keys"]["gemini"] == "synthetic-gemini-key"
            assert captured[-1][0] == "/v1/responses"
            await screenshot(page, "native-llm-openai.png")
            choose("gemini")
            assert key.value == "synthetic-gemini-key" and endpoint.value == base
            models.value = "--custom--"; models.on_select(SimpleNamespace(control=models))
            custom = field("自定义模型 ID")
            assert custom.visible
            custom.value = ""
            before = copy.deepcopy(load_config())
            save.on_click(SimpleNamespace(control=save))
            assert load_config() == before
            custom.value = "tenant-gemini-model"
            save.on_click(SimpleNamespace(control=save))
            assert load_config()["llm"]["model"] == "tenant-gemini-model"
            choose("ollama")
            key.value, endpoint.value = "", "https://ollama.com/v1"
            before = copy.deepcopy(load_config())
            save.on_click(SimpleNamespace(control=save))
            assert load_config() == before
            key.value, endpoint.value = "", ""
            save.on_click(SimpleNamespace(control=save))
            assert load_config()["llm"]["provider"] == "ollama"
            (Path.cwd() / "native-llm-settings.json").write_text(json.dumps(dict(
                native_app=True, callback_driven=True, provider_options=len(PROVIDERS), key_isolation=True,
                gemini_connection=True, responses_connection=True, custom_model=True,
                empty_remote_key_rejected=True, local_ollama_compatible=True,
                paid_models=False, requests=len(captured)), ensure_ascii=False, indent=2), encoding="utf-8")
            print("PASS native settings: Gemini/OpenAI SDK tests, provider/key switching, custom IDs and Ollama", flush=True)
        except Exception:
            failures.append(True)
            await screenshot(page, "native-llm-failure.png")
            raise
        finally:
            await page.window.close()


async def main(page):
    try:
        await validate(page)
    except Exception:
        if not failures:
            failures.append(True)
            await page.window.close()
        raise


if __name__ == "__main__":
    ft.run(main)
    if failures:
        raise SystemExit(1)
