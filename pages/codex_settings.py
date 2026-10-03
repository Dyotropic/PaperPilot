"""Subscription authentication controls; all credentials stay in Codex's profile."""
import asyncio
import os
from pathlib import Path

import flet as ft

from pages.context import FS_SM, SP_SM, SP_MD, text_secondary
from paperpilot.codex_transport import get_server, CodexError


class CodexSettings:
    def __init__(self, page, saved, on_models):
        self.page, self.on_models = page, on_models
        self.cli = ft.TextField(label="Codex CLI 路径（可选）", hint_text="留空自动查找 codex",
                               value=saved.get("codex_cli", ""), expand=True)
        self.home = ft.TextField(label="Codex 登录目录（可选）", hint_text="留空使用项目 cache 下的独立目录",
                                value=saved.get("codex_home", ""), expand=True)
        self.status = ft.Text("尚未检查登录，请登录或刷新状态。", size=FS_SM)
        self.pending = None
        self.busy = False
        self.buttons = []
        paths = ft.Container(content=ft.Column([self.cli,self.home],spacing=SP_SM), visible=False)
        def toggle_paths(e):
            paths.visible = not paths.visible
            paths.update()
        def button(label, action):
            control = ft.TextButton(content=ft.Text(label), on_click=lambda e: self.dispatch(action))
            self.buttons.append(control)
            return control
        self.control = ft.Column([
            ft.Text("使用官方 Codex 的 ChatGPT 登录及订阅额度，账号决定可用模型。",
                    size=FS_SM, color=text_secondary()),
            ft.Row([button("ChatGPT 登录", "login"), button("刷新登录与模型", "refresh"),
                    button("复用本机 Codex 登录", "import")], spacing=SP_SM, wrap=True),
            ft.Row([button("取消登录", "cancel"), button("退出项目登录", "logout")],
                   spacing=SP_SM, wrap=True),
            self.status,
            ft.TextButton(content=ft.Text("登录路径选项 ▾"), on_click=toggle_paths),
            paths,
        ], spacing=SP_MD, tight=True, visible=False)

    def values(self):
        return dict(codex_cli=(self.cli.value or "").strip(), codex_home=(self.home.value or "").strip())

    def update(self, text, error=False):
        self.status.value = text
        self.status.color = ft.Colors.ERROR if error else ft.Colors.OUTLINE
        try: self.control.update()
        except (RuntimeError, AssertionError): pass  # Page may have been left during OAuth.

    def dispatch(self, action):
        if self.busy:
            return
        values = self.values()
        self.busy = True
        self.update("正在处理 Codex 登录…")
        async def run():
            try:
                server = await asyncio.to_thread(get_server, values["codex_cli"], values["codex_home"])
                if action == "login":
                    result = await asyncio.to_thread(server.login)
                    self.pending = (server, result["loginId"])
                    self.page.launch_url(result["authUrl"])
                    self.update("已打开官方登录页面；完成后点击刷新登录与模型。")
                    self.page.run_task(self.watch_login, server, result["loginId"])
                elif action == "cancel":
                    target = self.pending[0] if self.pending else server
                    await asyncio.to_thread(target.cancel_login)
                    self.pending = None
                    self.update("登录已取消。")
                elif action == "logout":
                    await asyncio.to_thread(server.logout)
                    self.pending = None
                    self.on_models()
                    self.update("已退出项目登录，本机其他 Codex 登录保留。")
                else:
                    if action == "import":
                        source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
                        await asyncio.to_thread(server.import_login, source)
                    await self.refresh(server)
            except Exception as exc:
                from paperpilot.codex_transport import _safe_message
                self.update(_safe_message(exc), error=True)
            finally:
                self.busy = False
        self.page.run_task(run)

    async def refresh(self, server):
        account = await asyncio.to_thread(server.account, True)
        if not account or account.get("type") != "chatgpt":
            self.update("尚未通过 ChatGPT 登录；API Key 登录不能使用订阅额度。", error=True)
            return
        models = await asyncio.to_thread(server.models, True)
        if not models:
            self.update("已登录，但账号模型目录为空，请稍后刷新。", error=True)
            return
        self.on_models()
        plan = account.get("planType") or "ChatGPT"
        self.update(f"已登录（{plan}），模型目录已刷新。")

    async def watch_login(self, server, login_id):
        # Poll only this explicitly started login, and stop on cancellation or timeout.
        for _ in range(450):
            await asyncio.sleep(2)
            if self.pending != (server, login_id):
                return
            try:
                if server.login_id != login_id:
                    self.pending = None
                    await self.refresh(server)
                    return
            except CodexError as exc:
                self.update(str(exc), error=True)
                return
        if self.pending == (server, login_id):
            self.pending = None
            await asyncio.to_thread(server.cancel_login)
            self.update("登录等待已超时，请重新登录。", error=True)
