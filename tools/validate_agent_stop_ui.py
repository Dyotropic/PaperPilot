"""Isolated desktop probe: Win32 input reaches Flutter, never invokes callbacks."""
import asyncio
import ctypes
from ctypes import wintypes
import json
import subprocess
import sys
from pathlib import Path
import traceback
from unittest.mock import patch

import flet as ft
from PIL import Image
from paperpilot import library
from paperpilot.llm_client import LLMClient, ChatResult
from paperpilot.llm_usage import TokenUsage, UsageStore
from paperpilot.agent_runtime import OperationCancelled, current_run, checkpoint, publish_reply
import threading
import time
import os
from tools.agent_ui_capture import screenshot
from pages.context import ctx
import app
import pages.agent_panel as panel

project = library.create_project("原生交互验收", "Synthetic research")
library.save_papers_to_project(project.id, [dict(title="Synthetic library evidence", source="local_pdf",
    authors="Synthetic Author", year=2026, abstract="Measured evidence from the library.")])
requests = []
failures = []

class Client(LLMClient):
    def __init__(self):
        super().__init__("native-ui-test")
        self.provider = "deepseek"
    def _do_chat(self, messages, *args):
        requests.append(messages)
        return ChatResult(content="Synthetic response.", usage=TokenUsage(100, 5, 64, 36))

    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        if "Stop this generation" in messages[-1]["content"]:
            requests.append(messages)
            self.last_result = ChatResult(content="已生成但尚未完成的分析")
            async def delayed():
                publish_reply(self.last_result.content)
                await asyncio.sleep(30)
                raise AssertionError("Stopped generation unexpectedly completed")
            return token.run_async(delayed)
        if "Score stop fixture" in messages[-1]["content"]:
            requests.append(messages)
            return ChatResult(content='开始评分。[ACTION:score]{"limit": 5}[/ACTION]')
        if "Search stop fixture" in messages[-1]["content"]:
            requests.append(messages)
            return ChatResult(content='开始检索。[ACTION:search]{"topic_desc": "Synthetic search", "primary_keywords": ["synthetic"], "secondary_keywords": []}[/ACTION]')
        return super()._do_cancellable(messages,temperature,max_tokens,timeout,model,thinking,token)

u = ctypes.windll.user32
u.FindWindowW.restype = wintypes.HWND
u.GetForegroundWindow.restype = wintypes.HWND
u.SetForegroundWindow.argtypes = [wintypes.HWND]
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

async def main(page):
    client = Client()
    with patch("paperpilot.ai_service.get_client", return_value=client):
        app.main(page)
    page.title = f"PaperPilot Native Stop {os.getpid()}"
    page.window.width = 1000
    page.window.height = 750
    page.update()
    with patch("paperpilot.ai_service.get_client", return_value=client), \
         patch("paperpilot.ai_service.get_task_model", return_value="native-ui-test"), \
         patch("paperpilot.ai_service.get_task_model_override", return_value=""):
        try:
            await asyncio.sleep(2)
            hwnd = u.FindWindowW(None, page.title)
            assert hwnd
            async def inputs(actions):
                # The foreground guard can reject an attempt while the OS changes focus.
                for attempt in range(3):
                    proc = await asyncio.create_subprocess_exec(sys.executable, "-B",
                        str(Path(__file__).resolve().parent / "agent_native_input.py"), json.dumps(actions), page.title,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    out, err = await proc.communicate()
                    if not proc.returncode:
                        await asyncio.sleep(.8)
                        return
                    # Do not retry a partially delivered sequence.
                    if b"FOREGROUND_NOT_ACQUIRED" not in err:
                        raise AssertionError(err.decode())
                    await asyncio.sleep(.5)
                raise AssertionError("Native input focus was not acquired")

            await screenshot(page, "native-before.png")
            await inputs([["click",1459,222]])
            assert page._dialogs.controls[-1].title.value == "用量详情"
            await screenshot(page, "native-usage.png")
            await inputs([["click",1023,897]])
            assert not page._dialogs.controls
            await inputs([["click",1458,155]])
            assert page._dialogs.controls[-1].title.value == "重命名会话"
            await inputs([["click",699,550],["text","Native renamed"],["click",934,677]])
            assert not page._dialogs.controls
            store = ctx.ai_service.session_store(project.id, project.name)
            assert any(s["title"] == "新对话Native renamed" for s in store.list_sessions())
            print("PASS native mouse: usage close and rename save persisted", flush=True)
            await inputs([["click",1124,1050],["text","First native message"],["enter"]])
            assert len(requests) == 1 and not panel._agent_input.value and not panel._thinking_active
            await inputs([["click",1124,1050],["text","First line"],["shiftenter"],["text","Second line"]])
            assert len(requests) == 1 and panel._agent_input.value == "First line\nSecond line"
            await inputs([["enter"]])
            assert len(requests) == 2 and requests[-1][-1]["content"] == "First line\nSecond line"
            print("PASS native keyboard: Enter sends once; Shift+Enter preserves newline", flush=True)
            await screenshot(page, "native-chat.png")
            await inputs([["click",950,1055]])
            await screenshot(page, "native-preset-menu.png")
            # The menu opens above the bottom toolbar; the first item is overview.
            await inputs([["click",835,837]])
            assert len(requests) == 3
            assert "Measured evidence from the library" in requests[-1][-1]["content"]
            assert UsageStore().records(project.id)[0]["operation"] == "research_overview"
            print("PASS native preset: overview received library evidence and usage attribution", flush=True)
            await inputs([["click",1459,222]])
            await screenshot(page, "native-usage-final.png")
            await inputs([["click",1023,897]])
            assert not page._dialogs.controls
            assert panel._agent_send_button.icon == ft.Icons.ARROW_UPWARD
            await inputs([["click",1124,1050],["text","Stop this generation"],["enter"]])
            assert panel._thinking_active and panel._agent_send_button.icon == ft.Icons.STOP
            await screenshot(page,"native-stop-running.png")
            await inputs([["click",1124,1050],["text","保留的草稿"],["enter"]])
            assert len(requests) == 4 and panel._agent_input.value == "保留的草稿"
            started=time.monotonic()
            await inputs([["click",1459,1054]])
            assert not panel._thinking_active and panel._agent_send_button.icon == ft.Icons.ARROW_UPWARD
            assert time.monotonic()-started<3
            cm=ctx.ai_service.get_conversation(project.id,project.name,session_id=panel._agent_session_id)
            assert cm._meta["last_run"]["state"] == "cancelled"
            assert "已生成但尚未完成的分析" in cm.display_messages[-1]["content"]
            assert "本轮已由用户停止" in cm.display_messages[-1]["content"]
            assert panel._agent_input.value == "保留的草稿"
            assert UsageStore().records(project.id)[0]["status"] == "cancelled"
            await screenshot(page,"native-stop-preserved.png")
            await inputs([["click",1124,1050],["enter"]])
            assert len(requests)==5 and not panel._thinking_active
            assert any("本轮已由用户停止" in m["content"] for m in requests[-1])
            print("PASS native stop: partial reply and draft retained; Enter continued with interruption history",flush=True)

            # A completed model reply must not release the stop button while a child
            # stage is still running. Uncancellable fixture returns deliberately late.
            from pages import search_page as sp
            entered,release=threading.Event(),threading.Event()
            original= [(dict(title="Saved result",abstract="Synthetic abstract",source="local_pdf"),1.0)]
            ctx.state.scores=original
            def score(*args,**kwargs):
                entered.set(); assert release.wait(8)
                return [dict(index=0,ai_score=9,ai_reason="Must not commit after stop")]
            with patch.object(ctx.ai_service,"score_papers",side_effect=score):
                await inputs([["click",1124,1050],["text","Score stop fixture"],["enter"]])
                assert entered.is_set() and panel._thinking_active
                assert panel._agent_send_button.icon==ft.Icons.STOP
                await inputs([["click",1459,1054]])
                assert panel._thinking_active and panel._agent_send_button.disabled
                release.set(); await asyncio.sleep(.7)
                assert not panel._thinking_active and ctx.state.scores==original
                assert "ai_score" not in original[0][0]
            print("PASS native stop: scoring waits for its worker and discards late results",flush=True)

            entered,release=threading.Event(),threading.Event()
            ctx.state.papers=[original[0][0]]
            def pipeline(*args,**kwargs):
                entered.set(); assert release.wait(8)
                return [dict(title="Late result")],[(dict(title="Late result"),1.0)],[]
            with patch.object(sp,"_run_pipeline",side_effect=pipeline):
                await inputs([["click",1124,1050],["text","Search stop fixture"],["enter"]])
                assert entered.is_set() and panel._thinking_active
                await inputs([["click",1459,1054]])
                assert panel._agent_send_button.disabled
                release.set(); await asyncio.sleep(.8)
                assert not panel._thinking_active and not ctx.state.is_searching
                assert ctx.state.scores==original and ctx.state.papers==[original[0][0]]
            print("PASS native stop: search retains previous results and blocks late publication",flush=True)
            # Exercise the library's actual callback and stop it through Win32 input.
            import pages.library_page as lp
            ctx.library_select_project(project.id)
            ctx.switch_page(1)
            page.update(); await asyncio.sleep(.3)
            def walk(control):
                yield control
                for child in getattr(control,"controls",[]) or []:
                    yield from walk(child)
                child=getattr(control,"content",None)
                if isinstance(child,ft.Control):yield from walk(child)
            read_button=next(c for c in walk(app.container_results)
                if isinstance(c,ft.IconButton) and c.tooltip=="AI 精读分析")
            entered,release=threading.Event(),threading.Event()
            original_papers=library.get_project_papers(project.id)
            def delayed_read(*args,**kwargs):
                entered.set(); assert release.wait(8)
                return dict(core_contribution="Late notes must not save",method="Synthetic")
            with patch.object(lp,"get_full_text_for_paper",return_value=("Synthetic full text "*30,"provided")), \
                 patch.object(ctx.ai_service,"deep_read",side_effect=delayed_read), \
                 patch.object(lp,"save_deep_read_notes") as save_notes, \
                 patch.object(lp,"save_deep_read_json") as save_json:
                read_button.on_click(None)
                await asyncio.sleep(.2)
                assert entered.is_set() and panel._agent_send_button.icon==ft.Icons.STOP
                await inputs([["click",1459,1054]])
                assert panel._agent_send_button.disabled
                release.set(); await asyncio.sleep(.7)
                assert not panel._thinking_active
                save_notes.assert_not_called(); save_json.assert_not_called()
                assert library.get_project_papers(project.id)==original_papers
            print("PASS library stop: native stop blocks late notes and preserves reading status",flush=True)
            (Path.cwd()/"native-stop-result.json").write_text(json.dumps(dict(
                native_mouse=True,native_keyboard=True,stop_partial=True,draft_retained=True,
                continuation_context=True,score_late_result_blocked=True,
                search_late_result_blocked=True,deep_read_late_result_blocked=True,requests=len(requests)),ensure_ascii=False),encoding="utf-8")
        except Exception:
            failures.append(True)
            traceback.print_exc()
            await screenshot(page, "native-failure.png")
            for d in page._dialogs.controls:
                print("Remaining dialog", d.title.value, "open", d.open,
                      "value", getattr(d.content, "value", None), flush=True)
            raise
        finally:
            await page.window.close()

ft.run(main)
if failures:
    raise SystemExit(1)
