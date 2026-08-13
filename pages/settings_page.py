"""设置页（index 2）。"""
import flet as ft

from pages.context import ctx, THEMES, _border, apply_theme


# ── 设置持久化 ──

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


def _make_model_selector():
    """创建模型选择下拉框，读取/写入 config.yaml。"""
    from paperpilot.config import load_config as _lc, save_config as _sc

    current = _lc().get("deepseek", {}).get("model", "deepseek-v4-flash")

    model_options = [
        ft.dropdown.Option("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ft.dropdown.Option("deepseek-chat", "DeepSeek V3 (chat) - 7月下线"),
    ]

    model_dd = ft.Dropdown(
        options=model_options,
        value=current if current in ("deepseek-v4-flash", "deepseek-chat") else "deepseek-v4-flash",
        expand=True,
    )

    model_status = ft.Text("", size=13)

    def on_change_model(e):
        _sc(updates={"deepseek": {"model": e.control.value}})
        model_status.value = f"已切换至 {e.control.value}，下次搜索生效"
        model_status.color = ft.Colors.GREEN
        model_status.update()

    model_dd.on_change = on_change_model

    return ft.Column([
        ft.Row([model_dd], spacing=8),
        model_status,
    ], spacing=4)


def build_settings_page(ctx):
    from paperpilot.config import load_config, save_config as do_save

    current_config = load_config()
    current_key = current_config.get("deepseek", {}).get("api_key", "")

    def mask_key(key: str) -> str:
        if not key:
            return ""
        if len(key) <= 8:
            return key[:3] + "****" + key[-1:]
        return key[:3] + "****" + key[-4:]

    key_status = ft.Text("", size=13)
    if current_key:
        key_status.value = f"已配置: {mask_key(current_key)}"
        key_status.color = ft.Colors.GREEN
    else:
        key_status.value = "未配置 API Key，关键词提取和翻译功能不可用"
        key_status.color = ft.Colors.ORANGE

    api_key_field = ft.TextField(
        label="DeepSeek API Key",
        hint_text="sk-...",
        value=current_key,
        password=True,
        can_reveal_password=True,
        expand=True,
    )

    save_status = ft.Text("", size=13)

    def on_save_key(e):
        new_key = api_key_field.value.strip()
        if not new_key:
            save_status.value = "API Key 不能为空"
            save_status.color = ft.Colors.ERROR
            save_status.update()
            return
        do_save(updates={"deepseek": {"api_key": new_key}})
        save_status.value = "API Key 已保存，下次搜索生效"
        save_status.color = ft.Colors.GREEN
        save_status.update()
        key_status.value = f"已配置: {mask_key(new_key)}"
        key_status.color = ft.Colors.GREEN
        key_status.update()

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
                    ft.Colors.ON_SURFACE if ctx.state.theme_name == name else ft.Colors.OUTLINE_VARIANT
                ),
                ink=True,
                on_click=lambda e, n=name: on_select_theme(n),
            ),
            ft.Text(t["label"], size=12, text_align=ft.TextAlign.CENTER),
        ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        for name, t in THEMES.items()
    ], spacing=20, alignment=ft.MainAxisAlignment.CENTER)

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

    return ft.Column([
        ft.Text("设置", size=22),
        ft.Divider(height=16),
        ft.Text("DeepSeek API", size=16, weight=ft.FontWeight.W_500),
        ft.Text("用于关键词提取和中英翻译，密钥仅存储在本地 config.yaml", size=13),
        key_status,
        ft.Row([
            api_key_field,
            ft.FilledTonalButton(
                content=ft.Text("保存"), icon=ft.Icons.SAVE, on_click=on_save_key,
            ),
        ], spacing=8),
        save_status,
        ft.Divider(height=12),
        ft.Text("模型", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择 DeepSeek API 模型，7月后 V3 将下线", size=13),
        _make_model_selector(),
        ft.Divider(height=16),
        ft.Text("数据源", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择从哪些来源获取论文", size=13),
        arxiv_switch,
        openalex_switch,
        ft.Divider(height=16),
        ft.Text("检索数量", size=16, weight=ft.FontWeight.W_500),
        ft.Text("每个来源的最大检索结果数", size=13),
        max_results_slider,
        ft.Container(height=8),
        ft.Text("结果显示与精排", size=16, weight=ft.FontWeight.W_500),
        ft.Text("控制最终显示的论文数量和送入精排的候选数", size=13),
        top_k_slider,
        ce_candidates_slider,
        ft.Divider(height=16),
        ft.Text("外观", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择配色主题和夜间模式", size=13),
        theme_selector,
        ft.Container(height=8),
        dark_switch,
        ft.Divider(height=16),
        ft.Text("离线模式", size=16, weight=ft.FontWeight.W_500),
        ft.Text("Embedding 模型：paraphrase-multilingual-MiniLM-L12-v2", size=13),
        ft.Text("已下载至本地缓存，无需联网", size=13, color=ft.Colors.GREEN),
    ], spacing=8, scroll=ft.ScrollMode.AUTO)
