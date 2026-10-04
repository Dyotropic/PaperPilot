"""Inline human decisions: approval and information have distinct semantics."""
import flet as ft

from pages.context import (ctx, FS_SM, FS_MD, FS_LG, FW_SEMIBOLD, R_LG, SP_XS, SP_SM, SP_MD, SP_LG,
    text_primary, text_secondary, border_color, surface, surface_hi, seed_color,
    AGENT_REVIEW_HEIGHT, AGENT_REVIEW_LINE_HEIGHT, AGENT_REQUEST_HEIGHT,
    DIFF_ADDED_LIGHT, DIFF_ADDED_DARK, DIFF_REMOVED_LIGHT, DIFF_REMOVED_DARK)
from paperpilot.agent_presentation import edit_scope, user_text


def _text(value, **kwargs):
    return ft.Text(value, size=FS_MD, color=text_primary(), no_wrap=False, **kwargs)


class RequestCard:
    def __init__(self, details, kind, respond, *, task=None, on_layout=None):
        self.details, self.kind, self.respond = details, kind, respond
        self.task = task
        self.on_layout = on_layout
        self.finished = False
        self.status = ft.Text("", size=FS_SM, color=text_secondary())
        self.actions = []
        self.fields, self.selected = {}, {}
        self.option_buttons = {}
        self.body = ft.Column(spacing=SP_SM, tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.control = ft.Container(content=self.body, bgcolor=surface(),
            border=ft.Border.all(1, border_color()), border_radius=R_LG,
            padding=ft.padding.Padding(left=SP_MD, top=SP_MD, right=SP_MD, bottom=SP_MD),
            margin=ft.margin.Margin(left=SP_SM, right=SP_SM, top=SP_XS, bottom=SP_XS),
            key=ft.ValueKey("agent-" + kind + "-" + details["request_id"]), data="agent_request")
        if kind == "approval":
            self._approval()
        else:
            self._question()

    def _heading(self, title, icon):
        return ft.Row([ft.Icon(icon, size=FS_LG, color=text_secondary()),
            _text(title, weight=FW_SEMIBOLD, expand=True)], spacing=SP_SM)

    def _approval(self):
        preview = self.details["preview"]
        is_file = "diff" in preview
        title = ("创建文件" if preview.get("new") else "修改文件") if is_file else "保存到文献库"
        target = preview["path"] if is_file else f"当前课题 · {preview['count']} 篇文献"
        explanation = user_text(preview.get("reason", ""), self.task).strip()
        if not explanation:
            explanation = ("本次会新建文件并写入内容。" if preview.get("new") else "本次会更新文件中的内容。") if is_file else "将下面选定的文献保存到当前课题，便于后续阅读和整理。"
        self.body.controls = [self._heading("需要你批准修改", ft.Icons.EDIT_NOTE),
            _text(explanation), _text(f"{title} · {target}", weight=FW_SEMIBOLD)]
        if is_file:
            added, removed = edit_scope(preview)
            self.body.controls.append(ft.Text(f"新增 {added} 行 · 移除 {removed} 行", size=FS_SM, color=text_secondary()))
            lines = []
            for line in preview["diff"].splitlines():
                if line.startswith(("---", "+++", "@@")):
                    continue
                color = text_primary()
                if line.startswith("+"):
                    color = DIFF_ADDED_DARK if ctx.state.dark_mode else DIFF_ADDED_LIGHT
                elif line.startswith("-"):
                    color = DIFF_REMOVED_DARK if ctx.state.dark_mode else DIFF_REMOVED_LIGHT
                lines.append(ft.Text(line, size=FS_SM, font_family="Consolas", color=color,
                    selectable=True, no_wrap=False))
            details = ft.Container(content=ft.ListView(controls=lines, expand=True, spacing=SP_XS),
                height=min(AGENT_REVIEW_HEIGHT, max(1, len(lines)) * AGENT_REVIEW_LINE_HEIGHT + SP_LG),
                bgcolor=surface_hi(), border_radius=R_LG,
                padding=ft.padding.Padding(left=SP_SM, top=SP_SM, right=SP_SM, bottom=SP_SM), visible=False)
        else:
            details = ft.Container(content=ft.ListView(controls=[_text(t, selectable=True) for t in preview["titles"]],
                expand=True, spacing=SP_SM), height=AGENT_REVIEW_HEIGHT, visible=False)
        def toggle(e):
            details.visible = not details.visible
            expand.content = "收起修改内容" if details.visible else "查看修改内容"
            self.control.update()
            # Layout changes can move the one-shot decision below the viewport.
            # Scroll only on this explicit review action, not on general updates.
            if self.on_layout:
                self.on_layout()
        expand = ft.TextButton("查看修改内容", icon=ft.Icons.UNFOLD_MORE, on_click=toggle)
        self.preview = details
        self.body.controls.extend([expand, details,
            ft.Text("仅批准这次修改，执行前会核对文件是否变化。" if is_file else "仅批准这批文献入库。",
                size=FS_SM, color=text_secondary()), self.status])
        self.actions = [ft.TextButton("拒绝", on_click=lambda e: self.decide(False)),
            ft.FilledButton("批准这次修改", on_click=lambda e: self.decide(True))]
        self.body.controls.append(ft.Row(self.actions, wrap=True, spacing=SP_SM))

    def _question(self):
        self.body.controls.append(self._heading("需要你补充信息", ft.Icons.HELP_OUTLINE))
        questions = ft.Column(spacing=SP_MD, tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        for question in self.details["questions"]:
            key = question["id"]
            section = [_text(user_text(question["question"], self.task), weight=FW_SEMIBOLD)]
            for option in question.get("options", []):
                label = option["label"]
                def choose(e, qid=key, answer=label):
                    if self.finished:
                        return
                    self.selected[qid] = answer
                    self.status.value = ""
                    for (current_id, current_label), button in self.option_buttons.items():
                        if current_id == qid:
                            button.icon = ft.Icons.RADIO_BUTTON_CHECKED if current_label == answer else ft.Icons.RADIO_BUTTON_UNCHECKED
                    self.control.update()
                button = ft.TextButton(content=ft.Column([
                    _text(label), ft.Text(option.get("description", ""), size=FS_SM,
                        color=text_secondary(), no_wrap=False, visible=bool(option.get("description")))],
                    spacing=SP_XS, tight=True, horizontal_alignment=ft.CrossAxisAlignment.START),
                    icon=ft.Icons.RADIO_BUTTON_UNCHECKED, on_click=choose)
                self.option_buttons[key, label] = button
                section.append(button)
            field = ft.TextField(hint_text="也可以写下你的回答" if question.get("options") else "写下你的回答",
                multiline=True, min_lines=1, max_lines=3, text_size=FS_MD, border_radius=R_LG,
                border_color=border_color(), focused_border_color=seed_color(),
                content_padding=ft.padding.Padding(left=SP_MD, top=SP_SM, right=SP_MD, bottom=SP_SM))
            def clear_error(e):
                self.status.value = ""
                self.status.update()
            field.on_change = clear_error
            self.fields[key] = field
            section.append(field)
            questions.controls.append(ft.Column(section, spacing=SP_SM, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH))
        self.body.controls.append(ft.Container(content=ft.Column([questions], scroll=ft.ScrollMode.AUTO),
            height=AGENT_REQUEST_HEIGHT if len(self.details["questions"]) > 1 else None))
        def submit(e):
            answers = {}
            for key, field in self.fields.items():
                selected, note = self.selected.get(key, ""), (field.value or "").strip()
                answer = "\n".join(value for value in (selected, note) if value)
                if not answer:
                    self.status.value = "请选择一个答案或写下回复，再发送。"
                    self.status.update()
                    return
                if len(answer) > 4000:
                    self.status.value = "每题回复最多 4000 字符，请精简后再发送。"
                    self.status.update()
                    return
                answers[key] = answer
            self.decide(answers)
        self.actions = [ft.TextButton("稍后回答", on_click=lambda e: self.decide(None)),
            ft.FilledButton("发送回复", on_click=submit)]
        self.body.controls.extend([self.status, ft.Row(self.actions, wrap=True, spacing=SP_SM)])

    def decide(self, answer):
        if self.finished:
            return
        if not self.respond(self.details["request_id"], answer):
            self.expire()
            return
        self.finished = True
        self.status.value = ("已批准本次修改" if answer is True else "已拒绝本次修改") if self.kind == "approval" else (
            "已发送回复" if answer is not None else "已保留问题，稍后继续")
        self._disable()
        self.control.update()

    def _disable(self):
        for control in [*self.actions, *self.fields.values(), *self.option_buttons.values()]:
            control.disabled = True

    def expire(self):
        if self.finished:
            return
        self.finished = True
        self.status.value = "这次请求已撤回。"
        self._disable()
        try:
            self.control.update()
        except RuntimeError:
            pass

    def retheme(self):
        self.control.bgcolor = surface()
        self.control.border = ft.Border.all(1, border_color())
        if self.kind == "approval" and "diff" in self.details["preview"]:
            self.preview.bgcolor = surface_hi()
        def visit(control):
            if isinstance(control, ft.Text):
                control.color = text_secondary() if control.size == FS_SM else text_primary()
                if control.font_family == "Consolas":
                    control.color = text_primary()
                    if (control.value or "").startswith("+"):
                        control.color = DIFF_ADDED_DARK if ctx.state.dark_mode else DIFF_ADDED_LIGHT
                    elif (control.value or "").startswith("-"):
                        control.color = DIFF_REMOVED_DARK if ctx.state.dark_mode else DIFF_REMOVED_LIGHT
            elif isinstance(control, ft.Icon):
                control.color = text_secondary()
            elif isinstance(control, ft.TextField):
                control.color = text_primary()
                control.border_color = border_color()
                control.focused_border_color = seed_color()
            children = list(getattr(control, "controls", []) or [])
            content = getattr(control, "content", None)
            if isinstance(content, ft.Control):
                children.append(content)
            for child in children:
                visit(child)
        visit(self.body)
