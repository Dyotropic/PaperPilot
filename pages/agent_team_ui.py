"""Right-panel team roster and read-only, live child conversations."""
import asyncio

import flet as ft

from pages.context import (ctx, FS_XS, FS_SM, FS_MD, FS_LG, FW_SEMIBOLD,
    R_SM, SP_XS, SP_SM, SP_MD, text_primary, text_secondary,
    seed_color, surface, surface_hi)
from pages.components import open_dialog, close_dialog
from paperpilot.agent_team import STATUS_TEXT, TERMINAL, saved_teams, stop_saved_agent


class AgentTeamView:
    def __init__(self, get_conversation, identity, on_usage=None):
        self.get_conversation, self.identity = get_conversation, identity
        self.on_usage = on_usage
        self.records = []
        self.expanded = True
        self.team_id = None
        self.dialog = None
        self.selected_agent = None
        self.selected_cm = None
        self._last_identity = None
        self._last_seen_team = None
        self._pending = False
        self.body = ft.Column(spacing=SP_XS, tight=True)
        self.host = ft.Container(key=ft.ValueKey("agent-team-host"), content=self.body)

    def notify(self, identity):
        """Coalesce background events; never update another chat's team view."""
        if identity != self.identity() or self._pending or not ctx.page:
            return
        self._pending = True
        try:
            ctx.page.run_task(self._update_async)
        except RuntimeError:
            self._pending = False  # Window shutdown must not fail a durable child task.

    async def _update_async(self):
        try:
            await asyncio.sleep(.08)
            self.refresh()
            if self.on_usage:
                self.on_usage()
        finally:
            self._pending = False

    def refresh(self, *, recover=False):
        try:
            cm = self.get_conversation()
            identity = str(cm.storage_directory.resolve())
            if identity != self._last_identity:
                self._last_identity = identity
                self._last_seen_team, self.team_id = None, None
                if self.dialog:
                    self.close()
            self.records = saved_teams(cm, recover=recover)
            latest = self.records[0]["team_id"] if self.records else None
            if latest != self._last_seen_team:
                self._last_seen_team = latest
                self.team_id = latest
            if self.team_id not in {t["team_id"] for t in self.records}:
                self.team_id = self.records[0]["team_id"] if self.records else None
            team = next((t for t in self.records if t["team_id"] == self.team_id), None)
            controls = []
            if team and team["agents"]:
                done = sum(a["state"] in TERMINAL for a in team["agents"])
                controls.append(ft.Row([
                    ft.TextButton(content=ft.Row([
                        ft.Icon(ft.Icons.GROUPS_OUTLINED, size=FS_LG, color=seed_color()),
                        ft.Text(f"子智能体 {done}/{len(team['agents'])}", size=FS_SM,
                            color=text_primary(), weight=FW_SEMIBOLD),
                        ft.Icon(ft.Icons.EXPAND_LESS if self.expanded else ft.Icons.EXPAND_MORE, size=FS_LG),
                    ], spacing=SP_XS, tight=True), on_click=self.toggle, expand=True,
                        tooltip="展开或折叠子智能体"),
                    ft.IconButton(icon=ft.Icons.HISTORY, icon_size=FS_LG,
                        tooltip="选择历史团队", on_click=self.show_history),
                ], spacing=SP_XS))
                controls.append(ft.Text(STATUS_TEXT.get(team["state"], team["state"]),
                    size=FS_XS, color=text_secondary()))
                if self.expanded:
                    rows = []
                    for agent in team["agents"]:
                        label = STATUS_TEXT.get(agent["state"], agent["state"])
                        icon = (ft.Icons.CHECK_CIRCLE_OUTLINE if agent["state"] == "completed" else
                                ft.Icons.ERROR_OUTLINE if agent["state"] in {"failed", "timed_out"} else
                                ft.Icons.PAUSE_CIRCLE_OUTLINE if agent["state"] in {"cancelled", "interrupted"} else
                                ft.Icons.MORE_HORIZ)
                        rows.append(ft.Container(key=ft.ValueKey(f"team-agent-{agent['agent_id']}"),
                            content=ft.Row([
                                ft.Icon(icon, size=FS_LG, color=seed_color()),
                                ft.Column([
                                    ft.Text(agent["name"], size=FS_SM, color=text_primary(),
                                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                    ft.Text(label, size=FS_XS, color=text_secondary()),
                                ], spacing=0, expand=True),
                                ft.Icon(ft.Icons.CHEVRON_RIGHT, size=FS_LG, color=text_secondary()),
                            ], spacing=SP_SM), bgcolor=surface_hi(), border_radius=R_SM,
                            padding=ft.padding.Padding(left=SP_SM, right=SP_SM, top=SP_XS, bottom=SP_XS),
                            tooltip="查看完整子 Agent 对话和结果",
                            on_click=lambda e, a=agent["agent_id"], t=team["team_id"]: self.open_agent(t, a)))
                    controls.append(ft.Container(height=SP_MD * 8, content=ft.ListView(controls=rows, spacing=SP_XS)))
            self.body.controls = controls
            self.host.padding = ft.padding.Padding(left=SP_SM, right=SP_SM,
                top=SP_XS if controls else 0, bottom=SP_XS if controls else 0)
            self.host.bgcolor = surface()
            with_update(self.host)
            if self.dialog and self.dialog.open:
                self._refresh_dialog()
        except (ValueError, OSError) as exc:
            self.body.controls = [ft.Text(f"团队记录未加载：{exc}", size=FS_XS, color=text_secondary())]
            with_update(self.host)

    def toggle(self, e=None):
        self.expanded = not self.expanded
        self.refresh()

    def show_history(self, e=None):
        options = []
        dialog = ft.AlertDialog(title=ft.Text("历史 Agent Team"), scrollable=True)
        def choose(team_id):
            self.team_id = team_id
            close_dialog(ctx.page, dialog)
            self.refresh()
        for team in self.records:
            options.append(ft.TextButton(content=ft.Text(
                f"{team['goal'][:60] or '科研任务'} · {STATUS_TEXT.get(team['state'], team['state'])}",
                size=FS_SM, color=text_primary()),
                on_click=lambda e, t=team["team_id"]: choose(t)))
        dialog.content = ft.Column(options, tight=True, spacing=SP_SM)
        dialog.actions = [ft.TextButton("关闭", on_click=lambda e: close_dialog(ctx.page, dialog))]
        open_dialog(ctx.page, dialog)

    def open_agent(self, team_id, agent_id):
        self.selected_cm = self.get_conversation()
        self.selected_agent = (team_id, agent_id)
        self.dialog = ft.AlertDialog(modal=False, title=ft.Text("子 Agent", size=FS_LG),
            content=ft.Container(width=min(720, max(240, (ctx.page.width or 1000) - SP_MD * 4)),
                height=max(160, min(520, (ctx.page.height or 750) - SP_MD * 12)),
                content=ft.ListView(spacing=SP_MD)),
            actions=[])
        self._refresh_dialog()
        open_dialog(ctx.page, self.dialog)

    def _refresh_dialog(self):
        team_id, agent_id = self.selected_agent
        team = next((t for t in saved_teams(self.selected_cm) if t["team_id"] == team_id), None)
        agent = next((a for a in team["agents"] if a["agent_id"] == agent_id), None) if team else None
        if not agent:
            return
        self.dialog.title.value = f"{agent['name']} · {STATUS_TEXT.get(agent['state'], agent['state'])}"
        content = [ft.Text(f"模型 {agent['model']} · {agent['turns']} 次任务 · 读取 {len(agent['sources_read'])} 段资料",
            size=FS_XS, color=text_secondary()),
            ft.Text("只读分析 · 由主 Agent 审查和执行项目修改", size=FS_XS, color=text_secondary())]
        for message in agent["messages"]:
            if message["role"] == "system":
                continue
            role = "子 Agent" if message["role"] == "assistant" else "任务 / 资料"
            text = message["content"]
            if message.get("tool_calls"):
                text += "\n\n" + "\n".join(
                    f"工具 `{call['function']['name']}`：`{call['function']['arguments']}`" for call in message["tool_calls"])
            content.append(ft.Column([
                ft.Text(role, size=FS_XS, color=text_secondary(), weight=FW_SEMIBOLD),
                ft.Markdown(text, selectable=True,
                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB),
            ], spacing=SP_XS, tight=True))
        if agent.get("partial"):
            content.append(ft.Markdown(agent["partial"] + "\n\n[回复未完成]", selectable=True))
        if agent.get("error"):
            content.append(ft.Text(agent["error"], size=FS_SM, color=text_primary(), selectable=True))
        if agent.get("released"):
            content.append(ft.Text("运行资源已回收；完整记录仍保留。", size=FS_XS, color=text_secondary()))
        self.dialog.content.content.controls = content
        actions = []
        if agent["state"] not in TERMINAL:
            actions.append(ft.TextButton("停止此子 Agent", disabled=agent["state"] == "stopping",
                on_click=lambda e: self.stop_child()))
        actions.append(ft.TextButton("关闭", on_click=lambda e: self.close()))
        self.dialog.actions = actions
        with_update(self.dialog)

    def stop_child(self):
        stop_saved_agent(self.selected_cm, *self.selected_agent)
        self.refresh()

    def close(self):
        close_dialog(ctx.page, self.dialog)
        self.dialog = None
        self.selected_agent = None


def with_update(control):
    try:
        control.update()
    except RuntimeError:
        pass  # A saved record can arrive before mounting or after window close.
