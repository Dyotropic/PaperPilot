"""Native full application: Agent search action -> save selection -> library CE.

Win32 input triggers a probe wired to production dispatch; all application pages
are constructed by app.main. Sources and CE use doubles. No external calls.
"""
import asyncio
import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import flet as ft
import numpy as np
from tools.agent_ui_capture import screenshot
from pages.context import ctx
from pages import search_page as search, agent_panel
from paperpilot import config, library, indexer
from paperpilot.search_metrics import SearchTrace
import app

parser=argparse.ArgumentParser()
parser.add_argument("--live-sources",action="store_true")
parser.add_argument("--cached-model",action="store_true")
args=parser.parse_args()
if args.cached_model: assert Path(indexer._CE_PATH).is_dir(),"Existing CE cache required; no model downloads"
for name in ("USERPROFILE","LOCALAPPDATA","APPDATA"):
    os.environ[name]=str(Path.cwd()/name.lower());Path(os.environ[name]).mkdir(exist_ok=True)
config.save_config({"data_sources":{"arxiv":True,"openalex":True,"europepmc":True},
                    "search":{"max_results":400,"top_k":50,"ce_candidates":100,"ce_idle_seconds":300}})
calls, predictions, failures, dialogs, timings = [], [], [], [], []
def fetch(**kw):
    source=kw["source"];calls.append((source,time.perf_counter()));time.sleep(.12)
    return [dict(title=f"Study {hashlib.sha256((source+str(i)).encode()).hexdigest()[:24]}",source=source,
                 authors="Fixture Author",abstract="Robot navigation and reproducible path planning. "*5,
                 doi=f"10.0/{source}-{i}",year=2024,api_score=1-i/400) for i in range(400)]
def predict(pairs,**kwargs):
    predictions.append(len(pairs)); return np.linspace(1,-1,len(pairs))
def walk(control):
    yield control
    for child in getattr(control,"controls",[]) or []: yield from walk(child)
    child=getattr(control,"content",None)
    if isinstance(child,ft.Control): yield from walk(child)

u=ctypes.windll.user32;u.FindWindowW.restype=w.HWND
u.GetWindowRect.argtypes=[w.HWND,ctypes.POINTER(w.RECT)]
u.ClientToScreen.argtypes=[w.HWND,ctypes.POINTER(w.POINT)];u.GetDpiForWindow.argtypes=[w.HWND]
u.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p]

async def main(page):
    original_open=search.open_dialog
    def record_dialog(p,dialog): dialogs.append(dialog); return original_open(p,dialog)
    project=library.create_project("Native workflow project","robot navigation")
    original_factory=indexer.CrossEncoder
    def load(*a,**kw):
        if not args.cached_model: return SimpleNamespace(predict=predict)
        model=original_factory(*a,**kw);original_predict=model.predict
        def measured(pairs,**options):
            predictions.append(len(pairs));return original_predict(pairs,**options)
        model.predict=measured;return model
    original_report=SearchTrace.report
    def record_trace(trace,boundary="pipeline"):
        timings.append(dict(boundary=boundary,**trace.snapshot()));return original_report(trace,boundary)
    with ExitStack() as stack:
        if not args.live_sources:
            stack.enter_context(patch.object(search,"fetch_with_cascade",side_effect=lambda **kw:(fetch(**kw),0)))
            stack.enter_context(patch.object(search,"fetch_multi_primary",side_effect=fetch))
            for name in ("arxiv","openalex","europepmc"): stack.enter_context(patch.object(search,"fetch_"+name,return_value=[]))
        factory=stack.enter_context(patch.object(indexer,"CrossEncoder",side_effect=load))
        stack.enter_context(patch.object(search.downloader,"cache_pdf",return_value=None))
        stack.enter_context(patch.object(search,"open_dialog",side_effect=record_dialog))
        stack.enter_context(patch.object(SearchTrace,"report",record_trace))
        try:
            def launch(e):
                agent_panel._dispatch_agent_action(dict(type="search",params=dict(topic_name="Native search",
                    topic_desc="robot navigation",primary_keywords=["robot navigation"],secondary_keywords=[])))
            page.add(ft.FilledButton("Run native workflow probe",width=250,height=48,on_click=launch))
            app.main(page);page.title="PaperPilot Native Search 20261003";page.update()
            await asyncio.sleep(2)
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4));hwnd=u.FindWindowW(None,page.title);assert hwnd
            point,rect=w.POINT(0,0),w.RECT();u.ClientToScreen(hwnd,ctypes.byref(point));u.GetWindowRect(hwnd,ctypes.byref(rect))
            scale=u.GetDpiForWindow(hwnd)/96
            coords=["click",point.x-rect.left+round(125*scale),point.y-rect.top+round(24*scale)]
            for attempt in range(3):
                proc=await asyncio.create_subprocess_exec(sys.executable,"-B",str(Path(__file__).with_name("agent_native_input.py")),
                    json.dumps([coords]),page.title,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
                out,err=await proc.communicate()
                if not proc.returncode: break
                if b"FOREGROUND_NOT_ACQUIRED" not in err: raise AssertionError(err.decode())
                await asyncio.sleep(.5)
            assert not proc.returncode,err.decode()
            for _ in range(1800):
                if ctx.state.scores and not ctx.state.is_searching: break
                await asyncio.sleep(.1)
            assert len(ctx.state.scores)==50 and len(ctx.search_checkboxes)==50,ctx.state.status_text
            assert app.container_project.visible and not app.container_results.visible
            if not args.live_sources: assert len(calls)==3 and max(t for _,t in calls)-min(t for _,t in calls)<.1
            scroller=app.container_project.content.controls[0].controls[0].content
            await scroller.scroll_to(offset=1400,duration=200)
            await asyncio.sleep(.3)
            await screenshot(page,"app_search.png")
            for box in ctx.search_checkboxes[:3]:
                box.value=True;box.on_change(SimpleNamespace(control=box))
            assert len(ctx.agent_paper_selection)==3
            ctx.search_actions["on_save_to_library"](None);assert dialogs
            dialog=dialogs[-1];dialog.content.controls[2].value=str(project.id)
            dialog.content.controls[2].update();dialog.actions[-1].on_click(None)
            assert len(library.get_project_papers(project.id))==3
            ctx.library_select_project(project.id);app.page_switcher(1)
            assert app.container_results.visible and ctx.agent_project_id==project.id
            sort=next(c for c in walk(app.container_results) if isinstance(c,ft.IconButton) and c.tooltip=="CE 语义排序")
            assert not sort.disabled;sort.on_click(None)
            for _ in range(150):
                texts=[c.value for c in walk(app.container_results) if isinstance(c,ft.Text)]
                if "排序完成，已更新 3 篇" in texts: break
                await asyncio.sleep(.1)
            assert "排序完成，已更新 3 篇" in texts,texts[:20]
            assert predictions==[100,3] and factory.call_count==1,predictions
            await screenshot(page,"app_library.png")
            app.page_switcher(2);await asyncio.sleep(.2)
            assert app.container_settings.visible
            app.page_switcher(0);assert len(ctx.state.scores)==50
            await screenshot(page,"app_final.png")
            report=dict(native_mouse=True,entry="app.main",dispatch="production Agent search",
                        source="real APIs" if args.live_sources else "doubles",ce="cached real CPU" if args.cached_model else "double",
                        recalled=len(ctx.state.papers),displayed=50,saved=3,ce_calls=predictions,model_loads=factory.call_count,
                        source_counts={s:sum(p["source"]==s for p in ctx.state.papers) for s in ("arxiv","openalex","europepmc")},
                        timings=timings,status=ctx.state.status_text,
                        pages=["search","library","settings","search"],agent_project_id=ctx.agent_project_id)
            (Path.cwd()/"app_report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
            print("NATIVE APP PASS: Agent dispatch, overlapping search, selection/save, library CE reuse, navigation",flush=True)
        except BaseException:
            failures.append(True);raise
        finally:
            indexer.unload_cross_encoder();await page.window.close()
ft.run(main)
if failures: raise SystemExit(1)
