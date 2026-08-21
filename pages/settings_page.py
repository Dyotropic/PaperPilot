"""设置页（index 2）。"""
import threading

import flet as ft

from pages.context import (
    ctx, THEMES, _border, apply_theme,
    FS_XS, FS_SM, FS_MD, FS_LG, FS_XL, FS_XXL, FS_HERO,
    FW_REGULAR, FW_MEDIUM, FW_SEMIBOLD, FW_BOLD,
    R_SM, R_MD, R_LG, R_XL, SP_XS, SP_SM, SP_MD, SP_LG, SP_XL, SP_XXL,
    text_primary, text_secondary, text_tertiary, border_color,
    seed_color, app_bg, surface, surface_hi, accent_container,
    subtle_shadow, card,
)
from paperpilot.llm_client import PROVIDERS, MODEL_CATALOG


# ── 多模型 LLM 配置 UI ──

_CUSTOM = "--custom--"  # 模型下拉"自定义…"哨兵


def _model_options_for(provider: str) -> list:
    """某 provider 的内置模型下拉选项（+自定义）。"""
    opts = [ft.dropdown.Option(mid, label) for mid, label in MODEL_CATALOG.get(provider, [])]
    opts.append(ft.dropdown.Option(_CUSTOM, "自定义…"))
    return opts


def _build_llm_service_card(ctx):
    """构建设置页「AI 模型服务」卡片：Provider/Key/Model/高级(任务模型)/测试。"""
    from paperpilot.config import load_config, save_config as do_save
    from paperpilot.llm_client import _load_llm_cfg

    cur = _load_llm_cfg()
    cur_provider = cur.get("provider", "deepseek") or "deepseek"
    cur_model = cur.get("model", "") or PROVIDERS[cur_provider]["default_model"]

    # ── 控件 ──
    provider_dd = ft.Dropdown(
        options=[ft.dropdown.Option(k, v["label"])
                 for k, v in PROVIDERS.items()],
        value=cur_provider, expand=True,
    )
    model_dd = ft.Dropdown(
        options=_model_options_for(cur_provider),
        value=cur_model, expand=True,
    )
    custom_model_field = ft.TextField(
        label="自定义模型 ID", hint_text="如 deepseek-v4-lab / 其他模型名",
        value=cur_model if cur_model not in dict(MODEL_CATALOG.get(cur_provider, [])) else "",
        expand=True, visible=False,
    )
    api_key_field = ft.TextField(
        label="API Key",
        hint_text=PROVIDERS[cur_provider]["key_hint"],
        value=cur.get("api_key", "") or "",
        password=True, can_reveal_password=True, expand=True,
    )
    base_url_field = ft.TextField(
        label="Base URL（可选）", hint_text="留空用内置默认地址",
        value=cur.get("base_url", "") or "", expand=True,
    )
    score_model_field = ft.TextField(
        label="精排打分模型（可选）", hint_text="留空用主模型",
        value=cur.get("score_model", "") or "", expand=True,
    )
    chat_model_field = ft.TextField(
        label="对话模型（可选）", hint_text="留空用主模型",
        value=cur.get("chat_model", "") or "", expand=True,
    )
    reasoning_model_field = ft.TextField(
        label="推理模型（两步推理，可选）", hint_text="留空=单步直答；填写才启用先推理再生成",
        value=cur.get("reasoning_model", "") or "", expand=True,
    )
    key_status = ft.Text("", size=13)
    test_status = ft.Text("", size=13)
    save_status = ft.Text("", size=13)

    # 已配置提示
    if cur.get("api_key") or cur_provider == "ollama":
        provider_label = PROVIDERS[cur_provider]["label"]
        key_status.value = f"已配置：{provider_label}"
        key_status.color = ft.Colors.GREEN
    else:
        key_status.value = "未配置 API Key，AI 精读/对话/关键词提取不可用"
        key_status.color = ft.Colors.ORANGE

    def on_change_provider(e):
        new_p = provider_dd.value or "deepseek"
        model_dd.options = _model_options_for(new_p)
        model_dd.value = PROVIDERS[new_p]["default_model"]
        api_key_field.hint_text = PROVIDERS[new_p]["key_hint"]
        custom_model_field.visible = False
        custom_model_field.value = ""
        # Provider 切换后重置为未保存态
        api_key_field.value = ""
        model_dd.update(); api_key_field.update(); custom_model_field.update()

    provider_dd.on_change = on_change_provider

    def on_change_model(e):
        custom_model_field.visible = (model_dd.value == _CUSTOM)
        custom_model_field.update()

    model_dd.on_change = on_change_model

    def on_test(e):
        test_status.value = "正在测试连通性..."
        test_status.color = ft.Colors.OUTLINE
        test_status.update()
        save_status.value = ""; save_status.update()

        # 用当前控件值（未保存）构造配置测试
        p = provider_dd.value or "deepseek"
        key = (api_key_field.value or "").strip()
        model = custom_model_field.value.strip() if model_dd.value == _CUSTOM \
            else (model_dd.value or "").strip()

        def _run():
            from paperpilot.llm_client import OpenAICompatClient, AnthropicClient
            base = (base_url_field.value or "").strip()
            base_url = base or PROVIDERS[p]["base_url"]
            if p == "anthropic":
                c = AnthropicClient(api_key=key, model=model or PROVIDERS[p]["default_model"],
                                    base_url=base)
            else:
                c = OpenAICompatClient(provider=p, base_url=base_url,
                                       api_key=key, model=model or PROVIDERS[p]["default_model"])
            ok, msg = c.test_connection()
            def _show():
                test_status.value = msg
                test_status.color = ft.Colors.GREEN if ok else ft.Colors.ERROR
                test_status.update()
            ctx.page.run_task(_show)

        threading.Thread(target=_run, daemon=True).start()

    def _collect_model(e=None, field=None):
        """保存时解析模型：自定义优先，否则下拉值。"""
        if model_dd.value == _CUSTOM:
            v = custom_model_field.value.strip()
            if v:
                return v
            return ""
        return model_dd.value or ""

    def on_save(e):
        from paperpilot.llm_client import PROVIDERS as _P
        p = provider_dd.value or "deepseek"
        model = _collect_model()
        if not model:
            save_status.value = "请选择或输入一个模型"
            save_status.color = ft.Colors.ERROR
            save_status.update()
            return
        updates = {
            "llm": {
                "provider": p,
                "api_key": (api_key_field.value or "").strip(),
                "base_url": (base_url_field.value or "").strip(),
                "model": model,
                "score_model": (score_model_field.value or "").strip(),
                "chat_model": (chat_model_field.value or "").strip(),
                "reasoning_model": (reasoning_model_field.value or "").strip(),
            }
        }
        do_save(updates)
        label = _P[p]["label"]
        save_status.value = f"已保存：{label} / {model}，下次 AI 调用生效"
        save_status.color = ft.Colors.GREEN
        key_status.value = (f"已配置：{label} / {model}"
                            if (api_key_field.value or p == "ollama")
                            else "未配置 API Key")
        if not (api_key_field.value or p == "ollama"):
            key_status.color = ft.Colors.ORANGE
        else:
            key_status.color = ft.Colors.GREEN
        save_status.update()
        key_status.update()

    # 高级选项折叠
    advanced_wrap = ft.Container(
        content=ft.Column([
            base_url_field,
            score_model_field,
            chat_model_field,
            reasoning_model_field,
            ft.Text(
                "「推理模型」非空才启用两步推理（先深度推理再生成回复）。"
                "多数模型单步即可，填了会增加一次 API 调用。",
                size=FS_SM, color=text_secondary(),
            ),
        ], spacing=SP_MD, tight=True),
        visible=False, padding=ft.padding.Padding(top=SP_SM),
    )

    advanced_toggle = ft.TextButton(
        content=ft.Text("高级选项 ▾", size=FS_MD), on_click=lambda e: (
            advanced_wrap.__setattr__("visible", not advanced_wrap.visible)
            or advanced_wrap.update()
        ),
    )

    return card(
        ft.Column([
            ft.Text("AI 模型服务", size=FS_LG, weight=FW_SEMIBOLD, color=text_primary()),
            ft.Text("选择 AI 服务商并配置密钥；支持多家主流模型", size=FS_SM, color=text_secondary()),
            ft.Divider(height=1, color=border_color()),
            key_status,
            ft.Row([
                provider_dd,
                model_dd,
            ], spacing=SP_SM),
            custom_model_field,
            ft.Row([
                api_key_field,
                ft.FilledTonalButton(
                    content=ft.Text("测试"), on_click=on_test,
                ),
                ft.FilledButton(
                    content=ft.Text("保存"), icon=ft.Icons.SAVE, on_click=on_save,
                ),
            ], spacing=SP_SM),
            test_status,
            save_status,
            advanced_toggle,
            advanced_wrap,
        ], spacing=SP_MD, tight=True),
        padding=SP_XL,
    )

def _save_setting(key: str, value):
    """保存单个搜索/数据源设置到 config.yaml。"""
    from paperpilot.config import save_config
    section, field = key.split(".", 1)
    save_config({section: {field: value}})


def _on_slider_saved(e, key: str):
    _save_setting(key, int(e.control.value))


# ── 设置页控件（模块级，供 app.py main() 恢复配置值）──
arxiv_switch = ft.Switch(label="arXiv", value=True)
arxiv_switch.on_change = lambda e: _save_setting("data_sources.arxiv", e.control.value)
openalex_switch = ft.Switch(label="OpenAlex", value=True)
openalex_switch.on_change = lambda e: _save_setting("data_sources.openalex", e.control.value)
max_results_slider = ft.Slider(min=100, max=500, value=250, divisions=40,
                                label="{value} 篇")
max_results_slider.on_change = lambda e: _on_slider_saved(e, "search.max_results")
top_k_slider = ft.Slider(min=10, max=200, value=50, divisions=19,
                          label="显示 {value} 篇")
top_k_slider.on_change = lambda e: _on_slider_saved(e, "search.top_k")
ce_candidates_slider = ft.Slider(min=10, max=200, value=100, divisions=19,
                                  label="精排候选 {value} 篇")
ce_candidates_slider.on_change = lambda e: _on_slider_saved(e, "search.ce_candidates")


def build_settings_page(ctx):
    from paperpilot.config import load_config, save_config as do_save

    # ── 主题切换 ──
    theme_buttons: dict[str, ft.Container] = {}

    def on_select_theme(name):
        ctx.state.theme_name = name
        apply_theme(ctx.page, name, ctx.state.dark_mode)
        do_save(updates={"ui": {"theme": name, "dark_mode": ctx.state.dark_mode}})
        # 就地更新主题按钮边框（避免重建页面导致滚回顶部）
        for n, btn in theme_buttons.items():
            btn.border = _border(
                ft.Colors.ON_SURFACE if n == name else ft.Colors.OUTLINE_VARIANT
            )
            btn.update()
        # 导航栏重建
        ctx.top_nav_ref.content = ctx.build_nav(2)
        ctx.top_nav_ref.update()

    def on_toggle_dark(e):
        ctx.state.dark_mode = e.control.value
        apply_theme(ctx.page, ctx.state.theme_name, ctx.state.dark_mode)
        do_save(updates={"ui": {"theme": ctx.state.theme_name, "dark_mode": ctx.state.dark_mode}})
        # 按钮边框不随夜间模式变化，只重建导航栏
        ctx.top_nav_ref.content = ctx.build_nav(2)
        ctx.top_nav_ref.update()

    theme_selector = ft.Row([
        ft.Column([
            ft.Container(
                width=44, height=44, border_radius=22,
                bgcolor=t["seed"],
                border=_border(
                    seed_color() if ctx.state.theme_name == name else border_color()
                ),
                shadow=subtle_shadow(),
                ink=True,
                on_click=lambda e, n=name: on_select_theme(n),
            ),
            ft.Text(t["label"], size=FS_SM, color=text_secondary(),
                    text_align=ft.TextAlign.CENTER),
        ], spacing=SP_XS, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        for name, t in THEMES.items()
    ], spacing=SP_XL, alignment=ft.MainAxisAlignment.CENTER)

    # 收集按钮引用用于就地更新（避免重建页面导致滚回顶部）
    for i, name in enumerate(THEMES):
        col = theme_selector.controls[i]
        btn = col.controls[0]  # Container 是 Column 的第一个子控件
        theme_buttons[name] = btn

    dark_switch = ft.Switch(
        label="夜间模式",
        value=ctx.state.dark_mode,
        on_change=on_toggle_dark,
    )

    def _section(title: str, desc: str, *controls):
        """构建设置分区卡片。"""
        return card(
            ft.Column([
                ft.Text(title, size=FS_LG, weight=FW_SEMIBOLD, color=text_primary()),
                ft.Text(desc, size=FS_SM, color=text_secondary()),
                ft.Divider(height=1, color=border_color()),
                *controls,
            ], spacing=SP_MD, tight=True),
            padding=SP_XL,
        )

    return ft.Column([
        ft.Row([
            ft.Text("设置", size=FS_HERO, weight=FW_BOLD, color=text_primary()),
        ], alignment=ft.MainAxisAlignment.START),
        ft.Text("配置 PaperPilot 的 AI 服务、数据源与外观", size=FS_MD, color=text_secondary()),
        ft.Container(height=SP_SM),
        _build_llm_service_card(ctx),
        _section(
            "数据源", "选择从哪些来源获取论文",
            arxiv_switch,
            openalex_switch,
        ),
        _section(
            "检索数量", "每个来源的最大检索结果数",
            max_results_slider,
        ),
        _section(
            "结果显示与精排", "控制最终显示的论文数量和送入精排的候选数",
            top_k_slider,
            ce_candidates_slider,
        ),
        _section(
            "外观", "选择配色主题和夜间模式",
            theme_selector,
            dark_switch,
        ),
        _section(
            "离线模式", "本地语义模型",
            ft.Text("Embedding 模型：paraphrase-multilingual-MiniLM-L12-v2",
                    size=FS_MD, color=text_primary()),
            ft.Text("已下载至本地缓存，无需联网", size=FS_MD, color=ft.Colors.GREEN),
        ),
    ], spacing=SP_MD, scroll=ft.ScrollMode.AUTO)
