"""Native production settings, fixture app-server auth; no browser/paid models."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

if not os.environ.get("PAPERPILOT_VALIDATION_ROOT"):
    raise SystemExit("Run via tools/run_validation.py")

import flet as ft
import app
from pages.context import ctx
from paperpilot.config import save_config, load_config, BASE_DIR
from paperpilot.codex_transport import get_server, close_servers
from tools.validate_llm_settings_ui import screenshot, descendants

work = Path(os.environ["PAPERPILOT_VALIDATION_ROOT"])
os.environ.update(USERPROFILE=str(work/"home"),LOCALAPPDATA=str(work/"home"/"AppData"/"Local"),
                  APPDATA=str(work/"home"/"AppData"/"Roaming"))
original_popen = subprocess.Popen
failures = []


async def validate(page):
    profile = work / "profile"
    save_config(dict(llm=dict(provider="codex",model="codex-default",codex_cli=sys.executable,
        codex_home=str(profile),api_keys=dict(gemini="synthetic-gemini-key"))))
    def popen(command, **kwargs):
        if command[0] == str(Path(sys.executable).resolve()) and command[1] == "app-server":
            return original_popen([sys.executable,"-B",str(BASE_DIR/"tools"/"codex_fixture_server.py")],**kwargs)
        return original_popen(command,**kwargs)
    with patch("subprocess.Popen",side_effect=popen):
        app.main(page)
        page.title = f"PaperPilot Native Codex Settings {os.getpid()}"
        page.window.width, page.window.height = 1100, 840
        ctx.switch_page(2)
        page.update()
        await asyncio.sleep(1)
        controls = [c for root in page.controls for c in descendants(root)]
        def field(label):
            return next(c for c in controls if isinstance(c,ft.TextField) and c.label == label)
        def button(label):
            return next(c for c in controls if isinstance(c,(ft.TextButton,ft.FilledButton,ft.FilledTonalButton))
                and isinstance(c.content,ft.Text) and c.content.value == label)
        provider = next(c for c in controls if isinstance(c,ft.Dropdown) and "codex" in {o.key for o in c.options})
        model = next(c for c in controls if isinstance(c,ft.Dropdown) and "codex-default" in {o.key for o in c.options})
        async def wait_text(part):
            for _ in range(150):
                await asyncio.sleep(.05)
                if any(isinstance(c,ft.Text) and part in (c.value or "") for c in controls): return
            raise AssertionError("Settings result missing: "+part)
        async def click(label,expected):
            control = button(label); control.on_click(SimpleNamespace(control=control))
            await wait_text(expected)
        try:
            assert not field("API Key").visible and not field("Base URL（可选）").visible
            assert len(provider.options) == 9
            await click("刷新登录与模型","尚未通过 ChatGPT 登录")
            urls = []
            with patch.object(page,"launch_url",side_effect=urls.append):
                await click("ChatGPT 登录","已打开官方登录页面")
                assert urls == ["https://auth.openai.com/fixture"]
                await click("取消登录","登录已取消")
                # Clear the prior success text so the next callback is awaited.
                status = next(c for c in controls if isinstance(c,ft.Text) and c.value == "登录已取消。")
                status.value = ""; status.update()
                await click("ChatGPT 登录","已打开官方登录页面")
                get_server().rpc("fixture/login/complete")
                await wait_text("模型目录已刷新")
            assert "fixture-research" in {o.key for o in model.options}
            await click("测试","连接成功（模型 codex-default）")
            button("保存").on_click(SimpleNamespace(control=button("保存")))
            assert load_config()["llm"]["provider"] == "codex"
            assert load_config()["llm"]["codex_home"] == str(profile)
            assert load_config()["llm"]["api_keys"]["gemini"] == "synthetic-gemini-key"
            await screenshot(page,"native-codex-settings.png")
            provider.value = "gemini"; provider.on_select(SimpleNamespace(control=provider))
            assert field("API Key").visible and field("API Key").value == "synthetic-gemini-key"
            provider.value = "codex"; provider.on_select(SimpleNamespace(control=provider))
            await click("退出项目登录","已退出项目登录")
            await click("刷新登录与模型","尚未通过 ChatGPT 登录")
            (work/"native-codex-settings.json").write_text(json.dumps(dict(native_app=True,callback_driven=True,
                fixture_backend=True,provider_options=9,login_cancel_refresh=True,login_completion=True,
                dynamic_models=True,test_connection=True,save_and_key_isolation=True,logout=True,
                real_browser_oauth=False,paid_models=False),indent=2),encoding="utf-8")
            print("PASS native Codex settings: auth callbacks, models, SDK bridge, save/key isolation, logout",flush=True)
        except Exception:
            failures.append(True)
            await screenshot(page,"native-codex-failure.png")
            raise
        finally:
            close_servers()
            await page.window.close()


async def main(page):
    try: await validate(page)
    except Exception:
        if not failures: failures.append(True)
        await page.window.close()
        raise


if __name__ == "__main__":
    ft.run(main)
    if failures: raise SystemExit(1)
