"""Native desktop acceptance of context meter, slash commands and compaction.

Run through tools/run_validation.py on a Windows desktop at 150% scaling with
a 1000x750 logical window (as in the other native acceptance probe).
Synthetic data/model; no paid provider calls or production configuration writes.
"""
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
from paperpilot import library
from paperpilot.agent_runtime import OperationCancelled
from paperpilot.ai_service import AIService
from paperpilot.llm_client import LLMClient, ChatResult
from paperpilot.llm_usage import TokenUsage
from pages.context import ctx, apply_theme
import pages.agent_panel as panel
from tools.agent_ui_capture import screenshot
import app

project = library.create_project("原生上下文验收", "Synthetic research topic")
seed = AIService().get_conversation(project.id, project.name, project.description)
for i in range(6):
    seed.add_user_message(f"研究问题 {i}：" + "合成研究依据，请保留数值单位与论文标识。" * 180)
    seed.add_assistant_message(f"合成研究结论 {i}：参数 0.123456789 m；DOI:10.fixture/example；尚未验证。")
requests, failures = [], []
blocked = threading.Event()


class Client(LLMClient):
    def __init__(self):
        super().__init__("deepseek-flash")
        self.provider = "deepseek"
        self.block = False
    def _do_chat(self, messages, *args):
        requests.append(copy.deepcopy(messages))
        text = ("## 用户目标与需求\n继续合成科研任务。\n## 研究依据与引用\nDOI:10.fixture/example。"
                "\n## 关键决策与约束\n参数 0.123456789 m；尚未验证。\n## 未完成工作与下一步\n继续核对证据。"
                if "现在生成科研工作流" in messages[-1]["content"] else "合成回复，已携带摘要继续研究。")
        return ChatResult(content=text, finish_reason="stop", usage=TokenUsage(500, 25, 256, 244))
    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        if self.block:
            async def wait():
                blocked.set()
                await asyncio.sleep(30)
                raise AssertionError("Stopped compression unexpectedly completed")
            return token.run_async(wait)
        return self._do_chat(messages)


async def main(page):
    client = Client()
    with patch("paperpilot.ai_service.get_client", return_value=client):
        app.main(page)
    page.title = f"PaperPilot Native Context {os.getpid()}"
    page.window.width, page.window.height = 1000, 750
    panel.set_agent_project(project.id, project.name, project.description)
    page.update()
    with patch("paperpilot.ai_service.get_client", return_value=client), \
         patch("paperpilot.ai_service.get_task_model", return_value="deepseek-flash"), \
         patch("paperpilot.ai_service.get_task_model_override", return_value=""):
        async def inputs(actions):
            for attempt in range(3):
                proc = await asyncio.create_subprocess_exec(sys.executable, "-B",
                    str(Path(__file__).with_name("agent_native_input.py")), json.dumps(actions), page.title,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                out, err = await proc.communicate()
                if not proc.returncode:
                    await asyncio.sleep(.8)
                    return
                if b"FOREGROUND_NOT_ACQUIRED" not in err:
                    raise AssertionError(err.decode())
                await asyncio.sleep(.5)
            raise AssertionError("Native input focus was not acquired")
        try:
            await asyncio.sleep(2)
            assert "1,000,000" in panel._context_text.value
            await screenshot(page, "native-context-before.png")
            await inputs([["click",1124,1050],["text","/"]])
            assert panel._command_menu.visible and all(r.visible for r in panel._command_rows.values())
            await screenshot(page, "native-context-menu.png")
            await inputs([["down"]])
            assert panel._slash_index == 1
            await inputs([["up"]])
            assert panel._slash_index == 0
            await inputs([["escape"]])
            assert not panel._command_menu.visible
            await inputs([["click",1124,1050],["text","compact"]])
            assert panel._command_menu.visible and panel._command_rows["compact"].visible
            assert not panel._command_rows["new"].visible
            await screenshot(page, "native-context-filtered.png")
            print("READY native slash menu; window", page.title, flush=True)
            if os.environ.get("PAPERPILOT_UI_REVIEW"):
                await asyncio.sleep(20)
            await inputs([["click",1150,942]])
            cm = ctx.ai_service.get_conversation(project.id, project.name, session_id=panel._agent_session_id)
            assert cm.compressed_summaries and not panel._thinking_active
            assert not panel._command_menu.visible and not panel._agent_input.value
            assert len(requests) == 1 and len(cm._history) == 12
            card = next(c for c in panel._agent_msg_list.controls if c.data == "compression")
            assert not card.content.expanded
            await screenshot(page, "native-context-compacted.png")
            print("PASS native click: manual compaction, meter and original history retained", flush=True)
            # The checkpoint marker is recorded where the maintenance happened,
            # at the end of the preserved transcript.
            await inputs([["click",1150,914]])
            assert card.content.expanded
            await screenshot(page, "native-context-expanded.png")
            await inputs([["click",1124,1050],["text","继续核对证据"],["enter"]])
            assert len(requests) == 2 and any("DOI:10.fixture/example" in m["content"] for m in requests[-1])
            assert "上下文" in panel._context_text.value
            await screenshot(page, "native-context-continued.png")

            # A new compression must be interruptible without adding /compact to
            # the actual chat or replacing the last user goal.
            original = copy.deepcopy(cm.build_api_messages(ctx.ai_service.chat_system_prompt(cm,project.name,project.description)))
            goal = copy.deepcopy(cm._meta["last_run"])
            client.block = True
            await inputs([["click",1124,1050],["text","/compact"],["enter"]])
            assert blocked.is_set() and panel._agent_send_button.icon == ft.Icons.STOP
            with patch.object(panel,"stop_agent_run",wraps=panel.stop_agent_run) as native_stop:
                await inputs([["click",1459,1054]])
                assert native_stop.call_count == 1, "Physical stop click did not reach the stop callback"
            for _ in range(20):
                if not panel._thinking_active:
                    break
                await asyncio.sleep(.1)
            assert not panel._thinking_active and cm._meta["last_run"] == goal
            assert cm.build_api_messages(ctx.ai_service.chat_system_prompt(cm,project.name,project.description)) == original
            await screenshot(page, "native-context-stopped.png")
            print("PASS native stop: maintenance cancellation preserved research goal and context", flush=True)
            await inputs([["click",1150,982]])
            assert page._dialogs.controls[-1].title.value == "上下文窗口"
            await screenshot(page,"native-context-dialog.png")
            await inputs([["text","950000"],["tab"],["tab"],["enter"]])
            assert not page._dialogs.controls and "950,000" in panel._context_text.value
            await inputs([["click",1150,982],["tab"],["enter"]])
            assert not page._dialogs.controls
            print("PASS native context dialog: capacity saved per model and close responded",flush=True)
            ctx.state.dark_mode = False
            apply_theme(page,"slate",False)
            panel.refresh_agent_panel_theme()
            panel._set_agent_panel_width(320)
            page.update()
            await asyncio.sleep(.7)
            await screenshot(page, "native-context-narrow.png")
            (Path.cwd()/"native-context-result.json").write_text(json.dumps(dict(
                native_slash_menu=True, escape=True, arrows=True, filter=True, click_compact=True, expand_summary=True,
                history_preserved=True, context_repriced=True, continuation=True,
                stop_compression=True, goal_preserved=True, narrow_light=True,
                capacity_saved=True, context_dialog_closed=True,
                requests=len(requests)),ensure_ascii=False),encoding="utf-8")
        except Exception:
            failures.append(True)
            traceback.print_exc()
            await screenshot(page,"native-context-failure.png")
        finally:
            await page.window.close()


ft.run(main)
if failures:
    raise SystemExit(1)
