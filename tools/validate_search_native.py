"""Native mouse -> actual search pipeline -> Flet results, with source/CE doubles.

Run in the isolated validation runner. Requires the existing Flet desktop client.
External services and semantic inference are deliberately mocked in this probe.
"""
import asyncio
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import flet as ft
import numpy as np
from PIL import ImageGrab
from pages.context import ctx, apply_theme
from pages import search_page as search
from paperpilot import config, indexer

for name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA"):
    os.environ[name] = str(Path.cwd()/name.lower())
    Path(os.environ[name]).mkdir(exist_ok=True)
config.save_config({"search": {"ce_idle_seconds": 300}})
calls, failures = [], []
barrier = threading.Barrier(3)
def fetch(**kw):
    calls.append(dict(source=kw["source"], started=time.perf_counter()))
    barrier.wait(5)
    time.sleep(.12)
    return [dict(title=f"Study {hashlib.sha256((kw['source']+str(i)).encode()).hexdigest()[:24]}",
                 source=kw["source"], authors="Synthetic Author", year=2024,
                 abstract="Study of perovskite solar cell stability under humidity and heat. "*5,
                 doi=f"10.0/{kw['source']}-{i}", api_score=1-i/400) for i in range(400)]

u = ctypes.windll.user32
u.FindWindowW.restype = w.HWND
u.GetForegroundWindow.restype = w.HWND
u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
u.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
u.GetDpiForWindow.argtypes = [w.HWND]
u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]

async def main(page):
    page.title = "PaperPilot Native Search 20261002"
    page.window.width, page.window.height = 1100, 760
    page.padding = 0
    ctx.page = page
    ctx.ai_service = SimpleNamespace(is_available=False)
    ctx.state.topic_name = "Native search verification"
    ctx.state.topic_desc = "perovskite solar cell stability"
    ctx.state.primary_keywords = ["perovskite solar cells"]
    ctx.state.keywords = list(ctx.state.primary_keywords)
    search.arxiv_switch.value = search.openalex_switch.value = search.europepmc_switch.value = True
    search.max_results_slider.value = 400
    search.top_k_slider.value, search.ce_candidates_slider.value = 50, 100
    model = SimpleNamespace(predict=lambda pairs, **kw: np.linspace(1, -1, len(pairs)))
    with patch.object(search, "fetch_with_cascade", side_effect=lambda **kw: (fetch(**kw), 0)), \
         patch.object(search, "fetch_multi_primary", side_effect=fetch), \
         patch.object(search, "fetch_arxiv", return_value=[]), \
         patch.object(search, "fetch_openalex", return_value=[]), \
         patch.object(search, "fetch_europepmc", return_value=[]), \
         patch.object(indexer, "CrossEncoder", return_value=model):
        root = search.build_search_page(ctx)
        scroller = root.controls[0].controls[0].content
        ctx.search_actions["topic_name"].value = ctx.state.topic_name
        ctx.search_actions["topic_desc"].value = ctx.state.topic_desc
        # Fixed probe button uses the production click handler. The rest of the
        # form, background pipeline, polling and rendered results are unchanged.
        page.add(ft.Column([ft.FilledButton("Start native search", width=220, height=48,
                                           on_click=ctx.search_actions["on_search"]), root],
                           spacing=0, expand=True))
        apply_theme(page, ctx.state.theme_name, False)
        try:
            await asyncio.sleep(2)
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            hwnd = u.FindWindowW(None, page.title)
            assert hwnd, "Native window missing"
            scale = u.GetDpiForWindow(hwnd)/96
            point, rect = w.POINT(0, 0), w.RECT()
            assert u.ClientToScreen(hwnd, ctypes.byref(point))
            assert u.GetWindowRect(hwnd, ctypes.byref(rect))
            x, y = point.x-rect.left+round(110*scale), point.y-rect.top+round(24*scale)
            print(f"Native target scale={scale} click={x},{y}", flush=True)
            for attempt in range(3):
                proc = await asyncio.create_subprocess_exec(sys.executable, "-B",
                    str(Path(__file__).with_name("agent_native_input.py")), json.dumps([["click", x, y]]), page.title,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                out, err = await proc.communicate()
                if not proc.returncode:
                    break
                # Retry only before any input was delivered.
                if b"FOREGROUND_NOT_ACQUIRED" not in err:
                    raise AssertionError(err.decode())
                await asyncio.sleep(.5)
            assert not proc.returncode, err.decode()
            assert u.GetWindowRect(hwnd, ctypes.byref(rect))
            ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(Path.cwd()/"native_before.png")
            for _ in range(200):
                if not ctx.state.is_searching and ctx.state.scores:
                    break
                await asyncio.sleep(.1)
            if len(ctx.state.scores) != 50:
                assert u.GetWindowRect(hwnd, ctypes.byref(rect))
                ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(Path.cwd()/"native_failure.png")
            assert len(ctx.state.scores) == 50, ctx.state.status_text
            assert len(ctx.search_checkboxes) == 50
            assert not ctx.state.is_searching
            assert len(calls) == 3
            starts = [call["started"] for call in calls]
            assert max(starts)-min(starts) < .1, "Source requests did not overlap"
            await scroller.scroll_to(offset=1350, duration=200)
            await page.window.to_front()
            await asyncio.sleep(.7)
            assert u.GetForegroundWindow() == hwnd
            assert u.GetWindowRect(hwnd, ctypes.byref(rect))
            ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(Path.cwd()/"native_results.png")
            (Path.cwd()/"native_report.json").write_text(json.dumps(dict(
                native_mouse=True, source_and_model="mock", source_overlap=True,
                source_count=len(calls), recalled=len(ctx.state.papers), displayed=len(ctx.state.scores),
                status=ctx.state.status_text, screenshot="native_results.png"), ensure_ascii=False), encoding="utf-8")
            print("NATIVE SEARCH PASS: mouse, overlapping sources, 50 rendered results", flush=True)
        except BaseException:
            failures.append(True)
            raise
        finally:
            indexer.unload_cross_encoder()
            await page.window.close()

ft.run(main)
if failures:
    raise SystemExit(1)
