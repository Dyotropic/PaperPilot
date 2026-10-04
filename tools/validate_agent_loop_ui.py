"""Real Flet window/control callbacks; scripted models, no paid APIs.

Exercises actual inline approval/question cards, direct start, denial/resume,
composer replies, themes, narrow layout and chat isolation. No physical input claim.
"""
import asyncio
import json
import os
from pathlib import Path
import traceback
from unittest.mock import patch

if not os.environ.get("PAPERPILOT_VALIDATION_ROOT"):
    raise SystemExit("Use tools/run_validation.py")
work = Path(os.environ["PAPERPILOT_VALIDATION_ROOT"])
os.environ.update(USERPROFILE=str(work / "home"), LOCALAPPDATA=str(work / "localapp"), APPDATA=str(work / "appdata"))

import flet as ft
import app
import pages.agent_panel as panel
from pages.context import ctx, apply_theme
from pages.components import close_dialog
from paperpilot import library
from paperpilot.agent_loop import saved_task, default_workspace
from paperpilot.agent_attachments import prepare_selection
from paperpilot.llm_usage import UsageStore, TokenUsage, usage_scope
from paperpilot.llm_client import ChatResult
from tools.validate_agent_loop import Actor, ScriptClient, plan, running, done, finish
from tools.validate_llm_settings_ui import screenshot as foreground_capture
from tools.agent_ui_capture import screenshot as surface_capture

failures = []
captures = {}


async def screenshot(page, name):
    try:
        await foreground_capture(page, name)
        captures[name] = "foreground"
    except AssertionError:
        # Focus may be unavailable on a managed desktop. Capture only the owned
        # native surface and check the content area, never another application's.
        await surface_capture(page, name)
        from PIL import Image
        with Image.open(Path.cwd() / name) as im:
            center = im.crop((im.width // 4, im.height // 4, im.width * 3 // 4, im.height * 3 // 4))
            usable = any(high - low > 30 for low, high in center.getextrema())
        captures[name] = "owned_surface" if usable else "unavailable"
        if not usable:
            (Path.cwd() / name).unlink()
        print(f"VISUAL {name}: {captures[name]}", flush=True)


async def main(page):
    actor = None
    def factory(task=None):
        return ScriptClient(lambda messages: actor(messages))
    with patch("paperpilot.ai_service.get_client", side_effect=factory), \
         patch("paperpilot.ai_service.get_task_model", return_value="deepseek-flash"):
        try:
            app.main(page)
            page.title = f"PaperPilot Native Loop {os.getpid()}"
            page.window.width, page.window.height = 1100, 850
            project = library.create_project("长任务原生验收", "临床队列证据与偏差")
            panel.set_agent_project(project.id, project.name, project.description)
            cm = ctx.ai_service.get_conversation(project.id, project.name, session_id=panel._agent_session_id)
            # Real isolated accounting and visible controls, not a patched label.
            store = UsageStore()
            with usage_scope(project_id=project.id, session_id=cm.session_id):
                for usage in (TokenUsage(input_tokens=12000, output_tokens=1250, cache_hit_tokens=6000, cache_miss_tokens=6000),
                              TokenUsage(input_tokens=3000, output_tokens=300, cache_hit_tokens=2400, cache_miss_tokens=600),
                              TokenUsage(input_tokens=1000, output_tokens=50), None):
                    store.record(ChatResult(usage=usage), [], task="chat", provider="deepseek", model="fixture")
                store.record(ChatResult(usage=TokenUsage(input_tokens=5000, output_tokens=1000)), [], task="score")
            panel.refresh_agent_usage()
            rows = panel._agent_panel_ref.content.controls
            usage_row = next(row for row in rows if isinstance(row, ft.Row) and panel._usage_text in row.controls)
            assert rows.index(usage_row) < rows.index(panel._agent_msg_list) and usage_row.visible
            assert panel._usage_text.visible and "输入 16,000 · 输出 1,600" in panel._usage_text.value
            assert "56.0%" in panel._usage_text.value and "4 次请求（部分无用量）" in panel._usage_text.value
            usage_row.controls[1].on_click(None)
            usage_dialog = page._dialogs.controls[-1]
            assert usage_dialog.title.value == "用量详情"
            close_dialog(page, usage_dialog)
            actor = Actor(cm, [plan("保存队列核对报告"), running(),
                ("read_attachment", dict(index=0)),
                ("write_file", dict(path="cohort.md", content="# 队列核对\n样本 30 与 32；未推断因果关系。\n",
                    reason="把核对结果和研究范围保存成一份报告，方便你之后复查样本信息。")),
                ("report_blocker", dict(reason="用户拒绝报告写入，请确认权限后继续。", evidence_ids=[]))])
            view = panel._task_view
            view.enabled.value = True
            view.enabled.on_change(None)
            assert {o.key for o in view.permission.options} == {"read_only", "ask_edit", "direct_edit"}
            assert view.permission.value == "ask_edit"
            attachment = work / "cohort-note.md"
            attachment.write_text("样本 30 与 32，只有观察记录，不能推断因果。", encoding="utf-8")
            composer = panel._attachment_composer
            composer.drafts[(project.id, cm.session_id)], composer.notes[(project.id, cm.session_id)] = prepare_selection([attachment])
            composer.refresh()
            # Invalid goals must leave the composer and attachment draft intact.
            invalid_goal = "过长的任务" * 2100
            panel._agent_input.value = invalid_goal
            panel._agent_input.on_submit(None)
            assert panel._agent_input.value == invalid_goal and composer.pending
            assert not panel._thinking_active and saved_task(cm) is None
            panel._agent_input.value = "核对临床队列证据，保存报告并说明研究范围"
            page.update()
            await screenshot(page, "native-loop-start.png")
            panel._agent_input.on_submit(None)
            assert not getattr(view, "config_dialog", None)
            assert panel._agent_input.value == ""
            assert not composer.pending
            assert saved_task(cm)["criteria"] == ["核对临床队列证据，保存报告并说明研究范围"]
            assert all(value is None for value in saved_task(cm)["limits"].values())
            async def wait_for(predicate, label):
                for _ in range(100):
                    if predicate():
                        return
                    await asyncio.sleep(.1)
                raise AssertionError(label)
            await wait_for(lambda: view.approval_card is not None and not view.approval_card.finished, "Missing inline edit approval")
            assert view.approval_card.control in panel._agent_msg_list.controls
            assert not view.approval_card.preview.visible
            assert not any(isinstance(item, ft.AlertDialog) and item.open for item in page._dialogs.controls)
            await screenshot(page, "native-loop-approval.png")
            approval = view.approval_card
            # Preview is bounded and optional, and cannot itself approve an edit.
            approval.body.controls[4].on_click(None)
            assert approval.preview.visible
            await screenshot(page, "native-loop-diff.png")
            approval.actions[0].on_click(None)
            await wait_for(lambda: not panel._thinking_active, "Denied task did not pause")
            assert saved_task(cm)["status"] == "paused"
            assert any(m.get("attachments") for m in cm._history if m["role"] == "user" and not m.get("internal"))
            assert any("样本 30 与 32" in json.dumps(request, ensure_ascii=False) for request in actor.requests)
            assert not (default_workspace(cm) / "cohort.md").exists()
            # A pre-change paused record may already exceed its old thresholds.
            state = saved_task(cm)
            used_before = state["used"]["requests"]
            state["limits"] = dict(seconds=10, requests=2, tokens=2000)
            cm.set_task_state(state)
            actor.actions = [("write_file", dict(path="cohort.md", content="# 队列核对\n样本 30 与 32；未推断因果关系。\n")),
                             done("write_file"), finish("write_file")]
            view.resume_or_steer()
            assert not getattr(view, "config_dialog", None)
            await wait_for(lambda: view.approval_card is not None and not view.approval_card.finished, "Resume reused approval")
            assert view.approval_card is not approval
            view.approval_card.actions[1].on_click(None)
            await wait_for(lambda: not panel._thinking_active, "Approved task did not settle")
            assert saved_task(cm)["status"] == "completed"
            assert all(value is None for value in saved_task(cm)["limits"].values())
            assert saved_task(cm)["used"]["requests"] > used_before
            assert "未推断因果" in (default_workspace(cm) / "cohort.md").read_text(encoding="utf-8")
            await screenshot(page, "native-loop-completed.png")
            details = view.details()
            # Close the task detail dialog before switching chats.
            close_dialog(page, details)
            await wait_for(lambda: not view.permission.disabled, "Permission did not unlock after completion")
            # Long bilingual requirements must not fall back to a generic form criterion.
            objective = "逐项核对记录，说明局限。Check sources and scope. " * 65
            actor = Actor(cm, [plan("重读报告"), running(), ("read_file", dict(path="cohort.md")),
                              done("read_file"), finish("read_file")])
            view.permission.value = "read_only"
            panel._agent_input.value = objective
            panel._agent_input.on_submit(None)
            await wait_for(lambda: not panel._thinking_active, "Read-only long message did not settle")
            state = saved_task(cm)
            assert state["status"] == "completed" and state["mode"] == "read_only"
            assert state["objective"] == objective.strip() and len(state["criteria"]) > 1
            assert "".join(state["criteria"]).replace(" ", "") == objective.replace(" ", "")
            assert view.approval_card is None or view.approval_card.finished
            # Direct edits also start immediately, without a setup or write approval.
            actor = Actor(cm, [plan("创建范围记录"), running(), ("write_file", dict(path="scope.md", content="仅基于队列记录。")),
                              done("write_file"), finish("write_file")])
            view.permission.value = "direct_edit"
            panel._agent_input.value = "保存 scope.md 并说明资料范围"
            panel._agent_input.on_submit(None)
            await wait_for(lambda: not panel._thinking_active, "Direct edit did not settle")
            assert saved_task(cm)["status"] == "completed" and (default_workspace(cm)/"scope.md").exists()
            assert view.approval_card is None or view.approval_card.finished
            assert not getattr(view, "config_dialog", None)
            # The model can ask before there are any failures or fixed plan steps.
            questions = [dict(id="scope", question="这次先核对现有报告，还是检索新的文献？", options=[
                dict(label="核对现有报告", description="先复查样本信息和结论范围"),
                dict(label="检索新的文献", description="补充课题相关研究后再整理")])]
            actor = Actor(cm, [("request_user_input", dict(questions=questions)),
                ("read_file", dict(path="cohort.md")),
                finish("read_file", summary="已核对报告中的样本 30 与 32。现有记录支持观察性比较，不能据此推断因果。")])
            view.permission.value = "read_only"
            ctx.state.dark_mode = False
            apply_theme(page, "slate", False)
            panel.refresh_agent_panel_theme()
            assert panel._usage_text in usage_row.controls and panel._usage_text.visible
            panel._agent_input.value = "继续核对课题资料，先询问我希望使用哪部分材料。"
            panel._agent_input.on_submit(None)
            await wait_for(lambda: view.question_card is not None and not view.question_card.finished, "Missing inline question")
            card = view.question_card
            assert card.control in panel._agent_msg_list.controls and saved_task(cm)["plan"] == []
            before = saved_task(cm)["used"]["requests"]
            card.actions[1].on_click(None)
            assert "请选择" in card.status.value and not card.finished
            card.option_buttons["scope", "核对现有报告"].on_click(None)
            assert card.status.value == ""
            card.fields["scope"].value = "补充" * 2100
            card.actions[1].on_click(None)
            assert "4000" in card.status.value and not card.finished
            assert saved_task(cm)["status"] == "waiting_input"
            card.fields["scope"].on_change(None)
            card.fields["scope"].value = "只核对样本与因果边界"
            page.update()
            await screenshot(page, "native-loop-question.png")
            panel._set_agent_panel_width(320)
            await screenshot(page, "native-loop-question-narrow.png")
            panel._set_agent_panel_width(380)
            # Theme changes must retain both the choice and the draft reply.
            ctx.state.dark_mode = True
            apply_theme(page, "slate", True)
            panel.refresh_agent_panel_theme()
            assert card.fields["scope"].value == "只核对样本与因果边界"
            assert card.selected["scope"] == "核对现有报告"
            await screenshot(page, "native-loop-question-dark.png")
            card.actions[1].on_click(None)
            await wait_for(lambda: not panel._thinking_active, "Question reply did not continue")
            assert saved_task(cm)["status"] == "completed"
            assert saved_task(cm)["user_answers"][-1]["answers"]["scope"] == "核对现有报告\n只核对样本与因果边界"
            assert saved_task(cm)["used"]["requests"] > before
            assert card.finished
            # A single free-form question also accepts the ordinary composer.
            actor = Actor(cm, [("request_user_input", dict(questions=[dict(id="scope", question="这份报告需要重点核对什么？")])),
                ("read_file", dict(path="cohort.md")), finish("read_file", summary="样本信息已核对。")])
            panel._agent_input.value = "帮我核对报告，先问清侧重点。"
            panel._agent_input.on_submit(None)
            await wait_for(lambda: view.question_card is not card and not view.question_card.finished, "Missing freeform question")
            panel._agent_input.value = "重点检查样本范围"
            panel._agent_input.on_submit(None)
            await wait_for(lambda: not panel._thinking_active, "Composer answer did not continue")
            assert panel._agent_input.value == ""
            assert saved_task(cm)["user_answers"][-1]["answers"]["scope"] == "重点检查样本范围"
            panel._new_agent_session()
            current = ctx.ai_service.get_conversation(project.id, project.name, session_id=panel._agent_session_id)
            assert current.session_id != cm.session_id and saved_task(current) is None
            assert saved_task(cm)["status"] == "completed"
            corrupt = dict(version=1, session_id=current.session_id, status=[])
            current._meta["agent_task"] = corrupt
            view.refresh()
            assert view.enabled.disabled and current._meta["agent_task"] == corrupt
            current._meta.pop("agent_task")
            view.refresh()
            assert not view.enabled.disabled
            ctx.state.dark_mode = False
            apply_theme(page, "slate", False)
            panel.refresh_agent_panel_theme()
            panel._set_agent_panel_width(320)
            view.enabled.value = True
            view.enabled.on_change(None)
            assert view.permission.visible
            await screenshot(page, "native-loop-narrow-light.png")
            (Path.cwd() / "native-loop-result.json").write_text(json.dumps(dict(native_window=True,
                callbacks=True, physical_input=False, paid_models=False, modes=3,
                visible_top_usage=True, weighted_cache=True, missing_usage=True, top_usage_details=True,
                denial=True, confirmed_resume=True, fresh_approval=True, file_written=True,
                immediate_start=True, no_budget_dialog=True, legacy_limits_removed=True, long_bilingual_input=True,
                attachment_direct_send=True, failed_start_preserves_draft=True,
                inline_approval=True, no_modal_approval=True, optional_bounded_diff=True,
                inline_question=True, blank_answer_not_submitted=True, choices_and_freeform=True,
                theme_preserves_answer_draft=True, composer_reply=True, no_mandatory_plan=True,
                session_isolation=True, corrupt_checkpoint_preserved=True, narrow_light=True, captures=captures), indent=2), encoding="utf-8")
            print("PASS native long task: immediate start, three inline modes, deny/resume/approve, legacy limits removed, bilingual goal and isolation", flush=True)
        except Exception:
            failures.append(True)
            traceback.print_exc()
            try:
                await screenshot(page, "native-loop-failure.png")
            except Exception:
                pass
        finally:
            await page.window.close()


if __name__ == "__main__":
    ft.run(main)
    if failures:
        raise SystemExit(1)
