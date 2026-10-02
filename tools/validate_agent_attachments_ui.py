"""Native desktop attachment acceptance with isolated files and a synthetic LLM."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
from unittest.mock import patch

import flet as ft
from PIL import Image
from paperpilot import library
from paperpilot.agent_runtime import publish_reply
from paperpilot.ai_service import AIService
from paperpilot.llm_client import LLMClient, ChatResult
from paperpilot.llm_usage import TokenUsage
from pages.context import ctx, apply_theme
from tools.agent_ui_capture import screenshot
import app
import pages.agent_panel as panel

project = library.create_project("原生附件验收", "Synthetic attachment research")
files = Path.cwd() / "selected files"
files.mkdir()
text = files / "result.csv"
text.write_text("x,value\nparameter,42\n" + "Synthetic evidence.\n" * 60, encoding="utf-8")
photo = files / "photo.png"
Image.new("RGB", (200, 120), "red").save(photo)
folder = Path.cwd() / "selected folder"
folder.mkdir()
(folder / "research.md").write_text("Folder evidence: parameter 73", encoding="utf-8")
(folder / "ignored.exe").write_bytes(b"ignored")
requests, failures = [], []
blocked = threading.Event()


class Client(LLMClient):
    def __init__(self):
        super().__init__("deepseek-flash")
        self.provider = "deepseek"
        self.block = False
    def _do_chat(self, messages, *args):
        requests.append(copy.deepcopy(messages))
        compact = isinstance(messages[-1]["content"], str) and "现在生成科研工作流" in messages[-1]["content"]
        return ChatResult(content="## 用户目标与需求\n研究附件。\n## 已完成工作与结果\n参数 42，红色图片。" if compact else "已读取合成附件，参数 42，图片红色。",
                          finish_reason="stop", usage=TokenUsage(5000, 30, 1024, 3976))
    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        if self.block:
            requests.append(copy.deepcopy(messages))
            async def wait():
                blocked.set()
                publish_reply("附件分析进行中")
                await asyncio.sleep(30)
                raise AssertionError("Stopped request completed")
            return token.run_async(wait)
        return self._do_chat(messages)


async def main(page):
    client = Client()
    with patch("paperpilot.ai_service.get_client", return_value=client):
        app.main(page)
    page.title = f"PaperPilot Native Attachments {os.getpid()}"
    page.window.width, page.window.height = 1000, 750
    panel.set_agent_project(project.id, project.name, project.description)
    page.update()
    with patch("paperpilot.ai_service.get_client", return_value=client), \
         patch("paperpilot.ai_service.get_task_model", return_value="deepseek-flash"), \
         patch("paperpilot.ai_service.get_task_model_override", return_value=""), \
         patch("paperpilot.llm_client.get_task_model_override", return_value=""):
        async def inputs(actions):
            for attempt in range(3):
                proc = await asyncio.create_subprocess_exec(sys.executable, "-B",
                    str(Path(__file__).with_name("agent_native_input.py")), json.dumps(actions), page.title,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                out, err = await proc.communicate()
                if not proc.returncode:
                    await asyncio.sleep(.7)
                    return
                if b"FOREGROUND_NOT_ACQUIRED" not in err:
                    raise AssertionError(err.decode())
                await asyncio.sleep(.5)
            raise AssertionError("Native input focus was not acquired")
        async def idle():
            for _ in range(60):
                if not panel._thinking_active:
                    return
                await asyncio.sleep(.1)
            raise AssertionError("Turn did not finish")
        async def add(mode, paths):
            delta = int((380 - panel._agent_panel_width) * 1.5)
            # Popup opens above the composer; fixed native coordinates follow
            # the project's 150% desktop acceptance geometry.
            for attempt in range(2):
                await inputs([["escape"],["click",962 + delta,1054],
                              ["click",880 + delta,{"files":913,"photos":986,"folder":1050}[mode]]])
                for _ in range(10):
                    if panel._attachment_composer.loading:
                        break
                    await asyncio.sleep(.1)
                if panel._attachment_composer.loading:
                    break
            assert panel._attachment_composer.loading, "Native menu click did not reach attachment picker"
            await inputs([["dialog_folder" if mode == "folder" else "dialog_files", paths]])
            assert not panel._attachment_composer.loading
        try:
            await asyncio.sleep(2)
            await screenshot(page,"native-attachments-before.png")
            await inputs([["click",962,1054]])
            await screenshot(page,"native-attachments-menu.png")
            print("READY attachment menu", page.title, flush=True)
            if os.environ.get("PAPERPILOT_UI_REVIEW"):
                await asyncio.sleep(20)
            # Re-open after capture/focus restoration and select in the same
            # native input process; foreground acquisition can dismiss a popup.
            await inputs([["escape"],["click",962,1054],["click",880,913]])
            await inputs([["dialog_files", [str(text),str(photo)]]])
            assert len(panel._attachment_composer.pending) == 2
            await screenshot(page,"native-attachments-preview.png")
            # Preview and close use actual pointer/keyboard input.
            await inputs([["click",1150,888]])
            assert page._dialogs.controls[-1].title.value == "附件预览"
            await screenshot(page,"native-attachments-preview-dialog.png")
            await inputs([["click",1070,945]])
            assert not page._dialogs.controls
            await inputs([["click",1190,1054],["text","读取参数和图片"],["enter"]])
            await idle()
            assert not panel._attachment_composer.pending
            assert isinstance(requests[0][-1]["content"], list)
            assert "parameter,42" in requests[0][-1]["content"][0]["text"]
            assert requests[0][-1]["content"][1]["type"] == "image_url"
            cm = ctx.ai_service.get_conversation(project.id, project.name, session_id=panel._agent_session_id)
            assert len(cm._history[0]["attachments"]) == 2
            await screenshot(page,"native-attachments-sent.png")
            print("PASS native: plus menu, OS multi-selection, preview/close and Enter image send", flush=True)
            await add("folder", str(folder))
            assert len(panel._attachment_composer.pending) == 1
            assert "跳过 1" in panel._attachment_composer.note.value
            old_id = panel._agent_session_id
            # Switching the chat must not expose the old chat's unsent files.
            panel._new_agent_session()
            assert not panel._attachment_composer.pending
            ctx.ai_service.select_session(project.id, project.name, old_id, project.description)
            panel.load_agent_conversation()
            assert len(panel._attachment_composer.pending) == 1
            await screenshot(page,"native-attachments-folder.png")
            # Remove, then use the dedicated photo entry for an attachment-only send.
            await inputs([["click",1446,888]])
            assert not panel._attachment_composer.pending
            await add("photos", str(photo))
            client.block = True
            await inputs([["click",1459,1054]])
            assert blocked.is_set() and panel._agent_send_button.icon == ft.Icons.STOP
            await inputs([["click",1459,1054]])
            await idle()
            assert cm._meta["last_run"]["state"] == "cancelled"
            assert cm._history[-2]["attachments"][0]["name"] == "photo.png"
            client.block = False
            await inputs([["click",1190,1054],["text","继续"],["enter"]])
            await idle()
            assert any(isinstance(m["content"], list) for m in requests[-1])
            await inputs([["click",1190,1054],["text","/compact"],["enter"]])
            await idle()
            assert cm.compressed_summaries
            assert cm._history[0]["attachments"]
            assert len(cm._messages) < len(cm._history)
            assert cm._history[0]["attachments"][0] not in [ref for m in cm._messages for ref in m.get("attachments", [])]
            text.unlink(); photo.unlink()
            panel.load_agent_conversation()
            assert cm._history[0]["attachments"]
            ctx.state.dark_mode = False
            apply_theme(page,"slate",False)
            panel.refresh_agent_panel_theme()
            panel._set_agent_panel_width(320)
            await add("folder", str(folder))
            page.update()
            await screenshot(page,"native-attachments-narrow-light.png")
            print("PASS native: folder scope, remove, isolated drafts, stop/continue, compaction and 320px light layout", flush=True)
            (Path.cwd()/"native-attachments-result.json").write_text(json.dumps(dict(
                plus_menu=True, native_file_picker=True, photo_picker=True, folder_picker=True,
                preview_closed=True, enter_send=True, multimodal_payload=True, remove=True,
                drafts_isolated=True, stop_continue=True, compaction_preserves_assets=True,
                narrow_light=True, provider="synthetic", requests=len(requests))), encoding="utf-8")
        except Exception:
            failures.append(True)
            traceback.print_exc()
            await screenshot(page,"native-attachments-failure.png")
        finally:
            await page.window.close()


ft.run(main)
if failures:
    raise SystemExit(1)
