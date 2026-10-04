"""Long-task controls and one-shot, reviewable edit approvals."""
import asyncio
import threading

import flet as ft

from pages.context import (ctx, FS_XS, FS_SM, FS_MD, FS_LG, SP_XS, SP_SM, SP_MD, R_LG,
                           text_primary, text_secondary, border_color, AGENT_DETAILS_HEIGHT)
from pages.components import open_dialog, close_dialog
from pages.agent_requests_ui import RequestCard
from paperpilot.agent_loop import (TaskController, create_task, saved_task, default_workspace,
                                  loop_settings, live_controller, STATUS_TEXT, DEFAULT_BUDGET)
from paperpilot.agent_workspace import MODES, Workspace, linked
from paperpilot.file_paths import io_path


class AgentTaskView:
    def __init__(self, service, get_conversation, identity, begin_run, send_message, team_notify=None,
                 add_card=None, on_card_layout=None):
        self.service, self.get_conversation, self.identity = service, get_conversation, identity
        self.begin_run, self.send_message, self.team_notify = begin_run, send_message, team_notify
        self.add_card = add_card
        self.on_card_layout = on_card_layout
        self.enabled = ft.Checkbox(label="长任务", value=False, tooltip="勾选后发送目标，持续执行至完成",
                                   on_change=lambda e: self.refresh())
        self.permission = ft.Dropdown(value=loop_settings()["mode"], text_size=FS_SM, dense=True,
            tooltip="长任务的操作权限", border=ft.InputBorder.NONE, border_radius=R_LG,
            options=[ft.dropdown.Option(k, v) for k, v in MODES.items()], expand=True)
        self.settings = ft.Container(content=ft.Row([self.enabled, self.permission], spacing=SP_SM),
            padding=ft.padding.Padding(left=SP_SM, top=SP_XS, right=SP_SM, bottom=SP_XS))
        self.body = ft.Column(spacing=SP_XS, tight=True)
        self.host = ft.Container(content=self.body, key=ft.ValueKey("agent-long-task-host"))
        self.pending = False
        self.approval_card = self.question_card = None
        self.cards = []
        self.last_identity = None

    def notify(self, identity):
        if identity != self.identity() or self.pending or not ctx.page:
            return
        self.pending = True
        try:
            ctx.page.run_task(self._update_async)
        except RuntimeError:
            self.pending = False

    async def _update_async(self):
        try:
            await asyncio.sleep(.08)
            self.refresh()
        finally:
            self.pending = False

    def refresh(self, *, recover=False):
        cm = self.get_conversation()
        identity = self.identity()
        changed_identity = identity != self.last_identity
        if changed_identity:
            for card in self.cards:
                card.expire()
            self.cards = []
            self.approval_card = self.question_card = None
            self.last_identity = identity
        error = None
        try:
            state = saved_task(cm, recover=recover)
        except ValueError as exc:
            state, error = None, str(exc)
        self.enabled.disabled = error is not None
        if error:
            self.enabled.value = False
        controller = live_controller(cm)
        if changed_identity:
            self.permission.value = state["mode"] if state else loop_settings()["mode"]
        self.permission.visible = bool(self.enabled.value or state)
        self.permission.disabled = controller is not None
        self.permission.color = text_primary()
        controls = []
        if error:
            controls.append(ft.Text(error, size=FS_SM, color=text_primary()))
        if state:
            active = next((s for s in state["plan"] if s["status"] == "running"), None)
            label = STATUS_TEXT[state["status"]]
            if active and state["status"] == "running":
                label += " · " + active["title"]
            controls.append(ft.Row([
                ft.Text(label, size=FS_SM, color=text_secondary(), expand=True, max_lines=2),
                ft.IconButton(icon=ft.Icons.CHECKLIST, tooltip="查看任务清单与详情", icon_size=FS_LG, on_click=self.details),
                ft.TextButton("继续任务", on_click=self.resume_or_steer,
                    visible=not controller and state["status"] in {"paused", "failed"}),
            ], spacing=SP_XS))
            if controller and controller.approval:
                self.on_approval(identity, controller.approval.details, controller.respond)
            elif self.approval_card:
                self.approval_card.expire()
            if controller and controller.question:
                self.on_question(identity, controller.question.details, controller.respond_question)
            elif self.question_card:
                self.question_card.expire()
        self.enabled.disabled = error is not None or controller is not None
        self.host.visible = bool(controls)
        self.body.controls = controls
        try:
            self.host.update()
            self.settings.update()
        except RuntimeError:
            pass

    def start(self, objective, *, attachments=None, on_started=None):
        if live_controller(self.get_conversation()):
            return False
        return self._launch(objective, attachments=attachments or [], on_started=on_started)

    def _launch(self, objective, *, attachments=None, resume=False, on_started=None):
        """Sending starts immediately; the resume button itself confirms recovery."""
        cm, identity = self.get_conversation(), self.identity()
        try:
            state = saved_task(cm, recover=True)
            if not isinstance(objective, str) or not objective.strip() or len(objective) > 10000:
                raise ValueError("目标须为 1–10000 字符")
            if resume and (not state or state["status"] not in {"paused", "failed"}):
                raise ValueError("只有暂停或失败的任务可确认恢复")
            workspace = state["workspace"] if resume else default_workspace(cm)
            if not resume:
                if any(linked(p) for p in (workspace, *workspace.parents)):
                    raise ValueError("默认工作区路径包含链接")
                io_path(workspace).mkdir(parents=True, exist_ok=True)
            Workspace(workspace, self.permission.value)
            run = self.begin_run(cm, identity[0], objective, "loop", attachments=attachments)
            if run is None:
                raise ValueError("另一个 Agent 操作仍在运行，请先停止或等待结束")
            try:
                if not resume:
                    # Preserve every requirement from the message, including
                    # long inputs, without asking the user to repeat it in a form.
                    criteria = [objective[i:i+1000].strip() for i in range(0, len(objective), 1000)
                                if objective[i:i+1000].strip()]
                    create_task(cm, identity[0], objective, criteria, workspace, self.permission.value)
                controller = TaskController(self.service, cm, run, resume=resume, limits=DEFAULT_BUDGET,
                    mode=self.permission.value, on_change=self.notify, on_approval=self.on_approval,
                    on_team_change=self.team_notify, on_question=self.on_question, on_message=self.on_message)
            except Exception:
                run.fail("长任务启动失败")
                run.finish()
                raise
        except (ValueError, OSError) as exc:
            self.send_message(f"长任务未启动：{exc}", role="system")
            return False
        if on_started:
            on_started()
        self.send_message("确认恢复长任务" if resume else objective, role="user",
                          attachments=attachments or [], attachment_directory=cm.storage_directory)
        self.refresh()
        def work():
            try:
                text = controller.execute()
                async def show():
                    if identity == self.identity():
                        self.send_message(text, role="agent")
                        self.refresh()
                        if ctx.refresh_library:
                            ctx.refresh_library()
                ctx.page.run_task(show)
            except Exception:
                async def failed():
                    if identity == self.identity():
                        self.send_message("长任务运行或记录保存失败；请检查任务详情和课题目录，未宣称完成。", role="system")
                try:
                    ctx.page.run_task(failed)
                except RuntimeError:
                    pass
            finally:
                run.finish()
                self.notify(identity)
        threading.Thread(target=work, name="PaperPilot-Goal", daemon=True).start()
        return True

    def resume_or_steer(self, e=None):
        cm = self.get_conversation()
        state, controller = saved_task(cm, recover=True), live_controller(cm)
        if not state:
            return
        if not controller:
            self._launch(state["objective"], resume=True)
            return
        identity = self.identity()
        field = ft.TextField(label="补充要求（将在完整工具批次结束后读取）", multiline=True, min_lines=3, max_lines=6)
        error = ft.Text("", size=FS_SM, color=text_primary())
        def submit(e):
            try:
                if identity != self.identity():
                    raise ValueError("会话已切换，请重新操作")
                controller.enqueue_instruction(field.value)
                close_dialog(ctx.page, dialog)
            except ValueError as exc:
                error.value = str(exc)
                error.update()
        dialog = ft.AlertDialog(title=ft.Text("追加任务要求"), content=ft.Column([field, error], tight=True),
            actions=[ft.TextButton("取消", on_click=lambda e: close_dialog(ctx.page, dialog)),
                     ft.FilledButton("加入下一轮", on_click=submit)])
        open_dialog(ctx.page, dialog)

    def on_message(self, identity, text, role):
        async def show():
            if identity == self.identity():
                self.send_message(text, role=role)
        try:
            ctx.page.run_task(show)
        except (RuntimeError, AttributeError):
            pass

    def _request_card(self, identity, details, respond, kind):
        async def show():
            if identity != self.identity():
                return
            controller = live_controller(self.get_conversation())
            request = (controller.approval if kind == "approval" else controller.question) if controller else None
            if not request or request.details["request_id"] != details["request_id"]:
                return
            attribute = "approval_card" if kind == "approval" else "question_card"
            previous = getattr(self, attribute)
            if previous and previous.details["request_id"] == details["request_id"]:
                return
            if previous:
                previous.expire()
            def reply(request_id, answer):
                return identity == self.identity() and respond(request_id, answer)
            card = RequestCard(details, kind, reply, task=controller.state, on_layout=self.on_card_layout)
            setattr(self, attribute, card)
            self.cards.append(card)
            self.cards = self.cards[-200:]
            if self.add_card:
                self.add_card(card.control)
        if not ctx.page:
            return
        try:
            ctx.page.run_task(show)
        except RuntimeError:
            pass

    def on_approval(self, identity, details, respond):
        self._request_card(identity, details, respond, "approval")

    def on_question(self, identity, details, respond):
        self._request_card(identity, details, respond, "question")

    def details(self, e=None):
        state = saved_task(self.get_conversation(), recover=True)
        if not state:
            return
        rows = [ft.Text(state["objective"], size=FS_MD, color=text_primary()),
                ft.Text("任务要求：\n" + "\n".join(state["criteria"]), size=FS_SM, color=text_primary()),
                ft.Text(f"工作区：{state['workspace']}\n状态：{STATUS_TEXT[state['status']]}\n{state['reason']}",
                        size=FS_SM, color=text_primary())]
        for step in state["plan"]:
            status = {"pending": "待处理", "running": "正在处理", "done": "已完成", "cancelled": "已撤销"}[step["status"]]
            rows.append(ft.Text(f"{status} · {step['title']}", size=FS_SM, color=text_primary()))
        used = state["used"]
        rows.append(ft.Text(f"用时 {used.get('seconds', 0):.1f} 秒；"
            f"请求 {used.get('requests', 0)} 次；"
            f"token {used.get('tokens', 0):,}；"
            f"用量不完整并按预留计数的请求 {used.get('estimated_requests', 0)} 次。",
            size=FS_SM, color=text_primary()))
        if not state["plan"]:
            rows.append(ft.Text("暂未建立任务清单；Agent 可根据实际资料逐步处理。", size=FS_SM, color=text_secondary()))
        dialog = ft.AlertDialog(title=ft.Text("任务清单与详情"),
            content=ft.Container(width=520, height=AGENT_DETAILS_HEIGHT, content=ft.Column(rows, scroll=ft.ScrollMode.AUTO, spacing=SP_SM)),
            actions=[ft.TextButton("关闭", on_click=lambda e: close_dialog(ctx.page, dialog))])
        open_dialog(ctx.page, dialog)
        return dialog
