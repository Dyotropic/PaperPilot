"""PDF 浏览模块：pywebview + PDF.js 统一渲染 + PyMuPDF 轻量预览。

方案 B 核心：所有论文通过 PDF.js 在 pywebview 独立窗口中渲染，
配合自定义 HTML 模板实现与 Flet 主窗口一致的主题外观。

依赖：
- pywebview>=4.0（独立阅读窗口）
- fitz / PyMuPDF（轻量预览，已有）
- PDF.js（CDN 加载，无需本地文件）

open_full_reader 自动获得 PDF 下载 + HTML 全文提取能力，
无需改动本文件。
"""

import base64
import logging
import os
import re
import threading
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# PDF.js 本地缓存（一次下载，终身受用，断网也能渲染）
_PDFJS_DIR = Path.home() / ".paperpilot_pdfjs"
_PDFJS_VERSION = "3.11.174"
_PDFJS_CDN = f"https://cdnjs.cloudflare.com/ajax/libs/pdf.js/{_PDFJS_VERSION}"
_PDFJS_FILES = {
    "pdf.min.js": f"{_PDFJS_CDN}/pdf.min.js",
    "pdf.worker.min.js": f"{_PDFJS_CDN}/pdf.worker.min.js",
}


def _ensure_pdfjs() -> tuple[str, str]:
    """确保 PDF.js 文件已缓存到本地，返回 (主库路径, worker路径)。

    本地文件不存在时从 CDN 下载，下载失败则回退到 CDN URL。
    """
    _PDFJS_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for filename, url in _PDFJS_FILES.items():
        local = _PDFJS_DIR / filename
        if local.exists():
            paths[filename] = f"file:///{local.as_posix()}"
        else:
            try:
                data = urllib.request.urlopen(url, timeout=15).read()
                local.write_bytes(data)
                logger.info("PDF.js 已缓存: %s (%s bytes)", filename, len(data))
                paths[filename] = f"file:///{local.as_posix()}"
            except Exception:
                logger.warning("PDF.js 下载失败，回退 CDN: %s", filename)
                paths[filename] = url
    return paths["pdf.min.js"], paths["pdf.worker.min.js"]

import fitz  # PyMuPDF
import flet as ft

try:
    import webview as _webview
except ImportError:
    _webview = None  # pywebview 未安装时灰显按钮

try:
    from paperpilot.downloader import cache_pdf as _download_pdf
except ImportError:
    _download_pdf = None

try:
    from paperpilot.downloader import fetch_full_text as _fetch_full_text
except ImportError:
    _fetch_full_text = None


# ── 颜色工具 ──

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """#0097A7 → (0, 151, 167)"""
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _blend_with(hex_color: str, factor: float, towards: str = "#000000") -> str:
    """将颜色向 towards 混合，factor 0→原色，1→towards。"""
    r1, g1, b1 = _hex_to_rgb(hex_color)
    r2, g2, b2 = _hex_to_rgb(towards)
    r = int(r1 + (r2 - r1) * factor)
    g = int(g1 + (g2 - g1) * factor)
    b = int(b1 + (b2 - b1) * factor)
    return _rgb_to_hex(max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


# ── PDF.js HTML 模板 ──

_PDFJS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<script src="__PDFJS_SCRIPT__"></script>
<style>
:root {
    --seed: __SEED__;
    --bg: __BG__;
    --text: __TEXT__;
    --bar-bg: __TITLEBAR_BG__;
    --bar-h: 40px;
    --btn-hover: __BTN_HOVER__;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", system-ui, sans-serif;
    overflow: hidden;
}
/* 顶部导航栏 */
.navbar {
    height: var(--bar-h); background: var(--bar-bg);
    display: flex; align-items: center; padding: 0 12px;
    gap: 10px; font-size: 13px;
}
.navbar .title {
    font-weight: 600; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; flex: 1;
}
.navbar button {
    background: var(--btn-hover); border: none; color: var(--text);
    height: 26px; min-width: 28px; border-radius: 4px; cursor: pointer;
    font-size: 13px; padding: 0 8px;
}
.navbar button:hover { background: color-mix(in srgb, var(--seed) 40%, var(--btn-hover)); }
.navbar button:disabled { opacity: 0.35; cursor: default; }
.navbar button:disabled:hover { background: var(--btn-hover); }
.navbar input {
    width: 48px; text-align: center; background: var(--btn-hover); border: none;
    color: var(--text); border-radius: 4px; padding: 3px 4px; font-size: 12px;
}
.navbar .sep { width: 1px; height: 18px; background: var(--btn-hover); }
/* 滚动视口 */
#viewer {
    height: calc(100vh - var(--bar-h));
    overflow-y: auto; display: flex; flex-direction: column;
    align-items: center; padding: 16px; gap: 8px;
}
/* 页面容器 */
.page-container {
    position: relative; box-shadow: 0 2px 16px rgba(0,0,0,0.3);
    border-radius: 2px; line-height: 0; flex-shrink: 0;
}
.page-container canvas { display: block; border-radius: 2px; }
/* 文字层 — scale-factor 由 JS 动态设置，提升标题等大字的对齐精度 */
.textLayer {
    position: absolute; left: 0; top: 0; right: 0; bottom: 0;
    overflow: hidden; line-height: 1.0;
    user-select: text !important; cursor: text; z-index: 1;
}
.textLayer span {
    color: transparent !important; position: absolute; white-space: pre;
    cursor: text; transform-origin: 0% 0%;
}
.textLayer ::selection,
.textLayer ::-moz-selection {
    background: rgba(0, 150, 200, 0.35); color: transparent !important;
}
/* loading */
#status {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    font-size: 15px; opacity: 0.7;
}
</style>
</head>
<body>
<div class="navbar">
    <span class="title" id="title-text">__TITLE__</span>
    <span class="sep"></span>
    <button onclick="scrollToPrevPage()" id="btn-prev" disabled>&#x25B2; 上一页</button>
    <span id="page-info" style="min-width:60px;text-align:center;">-- / --</span>
    <button onclick="scrollToNextPage()" id="btn-next" disabled>&#x25BC; 下一页</button>
    <span class="sep"></span>
    <input type="number" id="goto-input" min="1" max="1" value="1">
    <button onclick="gotoPage()">跳转</button>
</div>
<div id="viewer"></div>
<div id="status">Loading PDF...</div>
<script>
pdfjsLib.GlobalWorkerOptions.workerSrc = '__PDFJS_WORKER__';

var __PDF_DATA__ = '__PDF_BASE64__';
var __PDF_SRC__ = '__PDF_PATH__';

var totalPages = 1;
var pdfDoc = null;
var renderedPages = {};  // {pageNum: container element}
var currentVisible = 1;

// ── 渲染单页（含文字层）──
function buildPage(num) {
    if (renderedPages[num]) return Promise.resolve(renderedPages[num]);
    return pdfDoc.getPage(num).then(function(page) {
        var scale = __SCALE__;  // 渲染精度，由 config.yaml 控制
        var vp = page.getViewport({scale: scale});

        var canvas = document.createElement('canvas');
        canvas.width = vp.width;
        canvas.height = vp.height;
        var ctx = canvas.getContext('2d');

        return page.render({canvasContext: ctx, viewport: vp}).promise.then(function() {
            return page.getTextContent().then(function(textContent) {
                var container = document.createElement('div');
                container.className = 'page-container';
                container.id = 'page-' + num;
                container.style.width = vp.width + 'px';
                container.style.height = vp.height + 'px';
                container.dataset.page = num;
                container.appendChild(canvas);

                var textLayer = document.createElement('div');
                textLayer.className = 'textLayer';
                textLayer.style.setProperty('--scale-factor', vp.scale);
                textLayer.style.width = vp.width + 'px';
                textLayer.style.height = vp.height + 'px';
                container.appendChild(textLayer);

                pdfjsLib.renderTextLayer({
                    textContentSource: textContent,
                    container: textLayer,
                    viewport: vp,
                    textDivs: []
                });

                renderedPages[num] = container;
                return container;
            });
        });
    });
}

// ── 加载 PDF → 渲染全部页面（滚动模式）──
function loadPdf(src) {
    pdfjsLib.getDocument(src).promise.then(function(pdf) {
        pdfDoc = pdf;
        totalPages = pdf.numPages;
        document.getElementById('title-text').textContent += '  (' + totalPages + ' pp)';
        document.getElementById('goto-input').max = totalPages;
        document.getElementById('status').style.display = 'none';

        // 渲染首页，让用户立刻看到内容
        buildPage(1).then(function(container) {
            document.getElementById('viewer').appendChild(container);
            document.getElementById('page-info').textContent = '1 / ' + totalPages;
            document.getElementById('btn-prev').disabled = true;
            document.getElementById('btn-next').disabled = (totalPages <= 1);
            // 继续渲染其余页面
            for (var i = 2; i <= totalPages; i++) renderInOrder(i);
        });
    }).catch(function(e) {
        document.getElementById('status').textContent = 'PDF load error: ' + e.message;
    });
}

// ── 按顺序渲染（保持页码顺序，避免异步打乱）──
var renderQueue = Promise.resolve();
function renderInOrder(num) {
    renderQueue = renderQueue.then(function() {
        return buildPage(num).then(function(container) {
            document.getElementById('viewer').appendChild(container);
        });
    });
}

// ── IntersectionObserver: 滚到哪页就更新页码 ──
var observer = new IntersectionObserver(function(entries) {
    var best = null;
    entries.forEach(function(e) {
        if (e.isIntersecting) {
            var pn = parseInt(e.target.dataset.page);
            // 取可见面积最大的页
            if (!best || e.intersectionRatio > best.ratio) {
                best = {page: pn, ratio: e.intersectionRatio};
            }
        }
    });
    if (best && best.page !== currentVisible) {
        currentVisible = best.page;
        document.getElementById('page-info').textContent = best.page + ' / ' + totalPages;
        document.getElementById('goto-input').value = best.page;
        document.getElementById('btn-prev').disabled = (best.page <= 1);
        document.getElementById('btn-next').disabled = (best.page >= totalPages);
    }
}, {threshold: [0, 0.25, 0.5, 0.75, 1.0]});

// 每页容器插入 DOM 后注册 observer
var origAppend = document.getElementById('viewer').appendChild.bind(document.getElementById('viewer'));
document.getElementById('viewer').appendChild = function(el) {
    origAppend(el);
    if (el.classList && el.classList.contains('page-container')) {
        observer.observe(el);
    }
};

// ── 导航 ──
function scrollToPrevPage() {
    if (currentVisible <= 1) return;
    var el = document.getElementById('page-' + (currentVisible - 1));
    if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
}
function scrollToNextPage() {
    if (currentVisible >= totalPages) return;
    var el = document.getElementById('page-' + (currentVisible + 1));
    if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
}
function gotoPage() {
    var n = parseInt(document.getElementById('goto-input').value);
    if (n >= 1 && n <= totalPages) {
        var el = document.getElementById('page-' + n);
        if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
}

// 键盘：上下箭头/PgUp/PgDn 滚动（利用 browser 原生行为），同时更新页码由 observer 自动完成

// Start
var pdfSrc;
if (__PDF_DATA__) {
    // base64 data → Uint8Array
    var binary = atob(__PDF_DATA__);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    pdfSrc = {data: bytes};
} else if (__PDF_SRC__) {
    pdfSrc = __PDF_SRC__;
}
if (pdfSrc) loadPdf(pdfSrc);
</script>
</body>
</html>
"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PaperPilot</title>
<style>
body { display:flex; align-items:center; justify-content:center;
       height:100vh; margin:0; font-family:"Segoe UI",sans-serif;
       background:__BG__; color:__TEXT__; }
.box { text-align:center; max-width:400px; padding:40px; }
h2 { font-weight:500; } p { opacity:0.7; margin-top:12px; }
</style></head>
<body><div class="box">
<h2>__MESSAGE__</h2><p>__DETAIL__</p></div></body></html>
"""


def _build_reader_html(
    pdf_path: str | None,
    title: str,
    seed_color: str,
    dark_mode: bool,
    scale: float = 2.5,
) -> str:
    """生成 PDF.js 阅读器的完整 HTML 页面。

    Args:
        pdf_path: 本地 PDF 文件路径，None 表示无文件可读
        title: 论文标题
        seed_color: 主题种子色
        dark_mode: 是否夜间模式
        scale: PDF 渲染精度 (2.5 / 3.0 / 4.0)
    """
    if dark_mode:
        bg = _blend_with(seed_color, 0.92, "#000000")
        text = "#e0e0e0"
        titlebar_bg = _blend_with(seed_color, 0.7, "#000000")
        btn_hover = _blend_with(seed_color, 0.4, "#000000")
    else:
        bg = "#ffffff"
        text = "#1a1a1a"
        titlebar_bg = _blend_with(seed_color, 0.3, "#ffffff")
        btn_hover = _blend_with(seed_color, 0.1, "#ffffff")

    # PDF 数据注入：file:/// 引用（HTML 通过 url 模式加载，同源无限制）
    abs_path = os.path.abspath(pdf_path).replace("\\", "/") if pdf_path and os.path.isfile(pdf_path) else ""
    safe_title = title.replace("\\", "\\\\").replace("`", "\\`")

    pdfjs_script, pdfjs_worker = _ensure_pdfjs()
    html = _PDFJS_HTML
    html = html.replace("__TITLE__", safe_title)
    html = html.replace("__SEED__", seed_color)
    html = html.replace("__BG__", bg)
    html = html.replace("__TEXT__", text)
    html = html.replace("__TITLEBAR_BG__", titlebar_bg)
    html = html.replace("__BTN_HOVER__", btn_hover)
    html = html.replace("'__PDF_BASE64__'", "''")
    html = html.replace("'__PDF_PATH__'", f"'file:///{abs_path}'" if abs_path else "''")
    html = html.replace("__PDFJS_SCRIPT__", pdfjs_script)
    html = html.replace("__PDFJS_WORKER__", pdfjs_worker)
    html = html.replace("__SCALE__", str(scale))

    return html


def _build_error_html(message: str, detail: str, dark_mode: bool, seed_color: str) -> str:
    bg = _blend_with(seed_color, 0.92, "#000000") if dark_mode else "#ffffff"
    text = "#e0e0e0" if dark_mode else "#1a1a1a"
    return (
        _ERROR_HTML.replace("__MESSAGE__", message)
        .replace("__DETAIL__", detail)
        .replace("__BG__", bg)
        .replace("__TEXT__", text)
    )


# ── 方案 B：pywebview 独立阅读窗口 ──

def _create_window(create_kwargs: dict) -> bool:
    """在独立子进程中启动 pywebview 窗口。

    关键：不用 html= 参数（WebView2 NavigateToString 有 ~2MB 限制，base64 PDF 会超限）。
    改为把 HTML 写入临时 .html 文件，用 url=file:/// 加载。
    这样 PDF 用 file:/// 引用也是同源，无跨域问题。
    """
    import json
    import subprocess
    import sys
    import tempfile

    html_content = create_kwargs.pop("html", None)
    _html_dir = Path.home() / ".paperpilot_pdf_cache"
    if html_content:
        _html_dir.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8",
            dir=str(_html_dir),
        )
        tmp.write(html_content)
        tmp.close()
        html_path = tmp.name
        create_kwargs["url"] = f"file:///{html_path.replace(chr(92), '/')}"
        # 子进程退出前清理临时文件
        cleanup_script = f"os.unlink({html_path!r})"
    else:
        cleanup_script = "pass"

    # 可选 JS↔Python 桥：js_api_factory 为 "模块路径:工厂函数名"（或 module.func），
    # 子进程内 importlib 构造桥对象后传 webview.create_window(js_api=...)。
    # 两键均从 create_kwargs 剔除（非 pywebview 参数）；缺省时行为与旧版完全一致。
    api_factory = create_kwargs.pop("js_api_factory", None)
    api_args = create_kwargs.pop("js_api_args", None) or []

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
        dir=str(_html_dir) if html_content else None,
    ) as f:
        json.dump(create_kwargs, f, ensure_ascii=False)
        args_path = f.name

    create_kwargs.setdefault("on_top", True)

    script = (
        "import json,webview,os,sys,threading,time\n"
        f"with open({args_path!r},'r',encoding='utf-8') as f:\n"
        "  k=json.load(f)\n"
        f"os.unlink({args_path!r})\n"
        "_jsapi=None\n"
        f"_af={api_factory!r}\n"
        f"_aa=json.loads({json.dumps(json.dumps(api_args))})\n"
        "if _af:\n"
        "  try:\n"
        "    import importlib\n"
        "    _mod,_name=(_af.split(':',1) if ':' in _af else _af.rsplit('.',1))\n"
        "    _jsapi=getattr(importlib.import_module(_mod),_name)(*_aa)\n"
        "  except Exception as _e:\n"
        "    print(f'[pdf_viewer] js_api factory error: {_e}',file=sys.stderr)\n"
        "def _remove_topmost():\n"
        "    if sys.platform!='win32': return\n"
        "    import ctypes\n"
        "    ktitle=k.get('title','')\n"
        "    hwnd=None\n"
        "    for _ in range(20):\n"
        "        time.sleep(0.3)\n"
        "        hwnd=ctypes.windll.user32.FindWindowW(None,ktitle)\n"
        "        if hwnd: break\n"
        "    if not hwnd:\n"
        "        import ctypes.wintypes as wt\n"
        "        found=[]\n"
        "        def _enum(h,_):\n"
        "            buf=wt.create_unicode_buffer(256)\n"
        "            ctypes.windll.user32.GetWindowTextW(h,buf,256)\n"
        "            t=buf.value\n"
        "            if t and t.startswith(ktitle[:30]): found.append(h); return False\n"
        "            return True\n"
        "        ctypes.windll.user32.EnumWindows(wt.WINFUNCTYPE(wt.BOOL,wt.HWND,wt.LPARAM)(_enum),0)\n"
        "        if found: hwnd=found[0]\n"
        "    if hwnd:\n"
        "        try:\n"
        "            fg=ctypes.windll.user32.GetForegroundWindow()\n"
        "            fg_tid=ctypes.windll.user32.GetWindowThreadProcessId(fg,None)\n"
        "            cur_tid=ctypes.windll.kernel32.GetCurrentThreadId()\n"
        "            if fg_tid and fg_tid!=cur_tid:\n"
        "                ctypes.windll.user32.AttachThreadInput(cur_tid,fg_tid,True)\n"
        "            ctypes.windll.user32.ShowWindow(hwnd,9)\n"
        "            ctypes.windll.user32.BringWindowToTop(hwnd)\n"
        "            ctypes.windll.user32.SetForegroundWindow(hwnd)\n"
        "            if fg_tid and fg_tid!=cur_tid:\n"
        "                ctypes.windll.user32.AttachThreadInput(cur_tid,fg_tid,False)\n"
        "        except Exception as _e:\n"
        "            print(f'[pdf_viewer] foreground error: {_e}',file=sys.stderr)\n"
        "        time.sleep(0.5)\n"
        "        SWP_NOMOVE=0x0002;SWP_NOSIZE=0x0001\n"
        "        ctypes.windll.user32.SetWindowPos(hwnd,-2,0,0,0,0,SWP_NOMOVE|SWP_NOSIZE)\n"
        "    else:\n"
        "        print(f'[pdf_viewer] window not found: {ktitle[:50]}',file=sys.stderr)\n"
        "t=threading.Thread(target=_remove_topmost,daemon=True)\n"
        "t.start()\n"
        "webview.create_window(js_api=_jsapi,**k) if _jsapi is not None else webview.create_window(**k)\n"
        "webview.start(gui='edgechromium')\n"
        f"{cleanup_script}\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=None,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(proc.pid)
        except Exception:
            pass
    return True


def open_full_reader(
    paper: dict,
    theme_seed: str = "#0097A7",
    dark_mode: bool = True,
    x: int | None = None,
    y: int | None = None,
) -> bool:
    """打开 pywebview 独立阅读窗口。

    全文来源优先级：
    1. paper["pdf_path"] — 课题仓库 / 缓存 / 原始路径本地文件
    2. repo_manager 缓存 (cache/pdfs/) — 检索页已下载过的 PDF
    3. downloader.cache_pdf(paper) — 直链下载 → 存入 repo 缓存
    4. 都没有 → 返回 False

    Args:
        paper: paper dict 格式（跨模块数据协议）
        theme_seed: 主题种子色，与 Flet 主窗口一致
        dark_mode: 是否夜间模式
        x, y: 窗口位置（屏幕坐标），None 则居中

    Returns:
        True 表示成功打开阅读窗口，False 表示 pywebview 不可用
    """
    if _webview is None:
        return False

    # 从 config 读取 PDF 渲染精度
    try:
        from paperpilot.config import load_config
        cfg = load_config()
        scale = float(cfg.get("ui", {}).get("pdf_render_scale", 2.5))
    except Exception:
        scale = 2.5

    pdf_path = paper.get("pdf_path")
    url = paper.get("url")
    doi = paper.get("doi")
    title = (paper.get("title") or "PaperPilot Reader").strip()

    # 标记：pdf_path 设了但文件不存在（用于最终错误提示）
    missing_local = pdf_path and not os.path.isfile(pdf_path)

    # 1. 本地 PDF 文件（课题仓库 / repo 缓存 / 原始路径）
    if pdf_path and os.path.isfile(pdf_path):
        return _open_pdfjs_window(pdf_path, title, theme_seed, dark_mode, x, y, scale)

    # 1.5. 课题仓库 catalog（DB 记录的路径可能过期，catalog 是权威来源）
    try:
        from paperpilot import repo_manager as _rm_cat
        catalog_results = _rm_cat.find_in_all_catalogs(paper)
        if catalog_results:
            cat_path = catalog_results[0][2]
            if cat_path and os.path.isfile(cat_path):
                paper["pdf_path"] = cat_path
                return _open_pdfjs_window(cat_path, title, theme_seed, dark_mode, x, y, scale)
    except Exception:
        pass

    # 2. repo_manager 缓存（检索页下载后缓存到 cache/pdfs/）
    try:
        from paperpilot import repo_manager as _rm
        cached = _rm.get_cached_pdf(paper)
        if cached and os.path.isfile(cached):
            paper["pdf_path"] = cached
            return _open_pdfjs_window(cached, title, theme_seed, dark_mode, x, y, scale)
    except Exception:
        pass

    # 3. 直链下载（arXiv/Nature/Springer 等）→ 存入 repo 缓存
    if _download_pdf is not None:
        try:
            downloaded = _download_pdf(paper)
            if downloaded and os.path.isfile(downloaded):
                try:
                    from paperpilot import repo_manager as _rm2
                    repo_path = _rm2.cache_pdf(paper, downloaded)
                    if repo_path:
                        downloaded = repo_path
                except Exception:
                    pass
                paper["pdf_path"] = downloaded
                return _open_pdfjs_window(downloaded, title, theme_seed, dark_mode, x, y, scale)
        except Exception:
            pass

    # 4. 本地文件丢失提示
    if missing_local:
        logger.info("本地 PDF 文件丢失: %s", paper.get("pdf_path"))
        return False

    # 5. 无法获取全文
    logger.info("无法获取全文: %s", title)
    return False


def _open_pdfjs_window(
    pdf_path: str,
    title: str,
    theme_seed: str,
    dark_mode: bool,
    x: int | None,
    y: int | None,
    scale: float = 2.5,
) -> bool:
    """生成 PDF.js HTML 并在 pywebview 窗口中打开。"""
    html = _build_reader_html(pdf_path, title, theme_seed, dark_mode, scale)
    return _create_window({
        "title": title,
        "html": html,
        "frameless": False,
        "width": 900,
        "height": 700,
        "x": x,
        "y": y,
        "min_size": (400, 300),
    })


def _open_html_window(
    html_path: str,
    title: str,
    theme_seed: str,
    dark_mode: bool,
    x: int | None,
    y: int | None,
) -> bool:
    """加载缓存 HTML 文件，注入文字选择修复 + 去除外链，pywebview 渲染。"""
    try:
        raw = Path(html_path).read_text(encoding="utf-8")
    except Exception:
        return _open_error_window(title, theme_seed, dark_mode, x, y)

    # ── 1. 注入文字选择修复样式 ──
    fix_css = """<style>
    body, body * { user-select: text !important; -webkit-user-select: text !important; cursor: auto !important; }
    a { cursor: pointer !important; }
    ::selection, ::-moz-selection { background: rgba(0,150,200,0.35) !important; }
</style>"""
    raw = raw.replace("</head>", fix_css + "\n</head>")

    # ── 2. 去除正文链接，保留图片链接 ──
    def _keep_image_hrefs(m: re.Match) -> str:
        full = m.group(0)
        href = m.group(1)
        # 保留 .jpg/.png/.gif/.svg/.webp/.jpeg 链接 + 下载类链接
        img_exts = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".tif", ".tiff")
        href_lower = href.lower()
        if any(href_lower.endswith(ext) for ext in img_exts):
            return full
        if "download" in href_lower or "/picture/" in href_lower or "/image/" in href_lower:
            return full
        # 其他链接：仅保留文字，移除 href
        return full.replace(f'href="{href}"', "").replace(f"href='{href}'", "")

    raw = re.sub(r'<a\s[^>]*href="([^"]*)"[^>]*>', _keep_image_hrefs, raw)
    raw = re.sub(r"<a\s[^>]*href='([^']*)'[^>]*>", _keep_image_hrefs, raw)

    # ── 3. 写入临时文件，通过 url 加载（保持页面完整性）──
    import tempfile
    tmp_dir = Path.home() / ".paperpilot_pdf_cache"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8", dir=str(tmp_dir)
    ) as f:
        f.write(raw)
        tmp_path = f.name

    return _create_window({
        "title": title,
        "url": f"file:///{tmp_path.replace(chr(92), '/')}",
        "frameless": False,
        "width": 960,
        "height": 750,
        "x": x,
        "y": y,
        "min_size": (400, 300),
    })


def _open_text_window(
    text: str,
    title: str,
    theme_seed: str,
    dark_mode: bool,
    x: int | None,
    y: int | None,
) -> bool:
    """将纯文本全文包装为阅读页面，在 pywebview 中展示（ScienceDirect 等无 PDF 时用）。"""
    # 合并连续非空行为段落（空行作为段落分隔符）
    paragraphs = []
    buf = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            buf.append(stripped)
        elif buf:
            paragraphs.append(" ".join(buf))
            buf = []
    if buf:
        paragraphs.append(" ".join(buf))

    body = "\n".join(f"<p>{_escape_html(p)}</p>" for p in paragraphs)

    bg = "#1e1e1e" if dark_mode else "#fafafa"
    fg = "#d4d4d4" if dark_mode else "#333333"
    accent = theme_seed

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;
    background: {bg}; color: {fg};
    line-height: 1.9; font-size: 15px;
    padding: 40px 48px;
    max-width: 820px; margin: 0 auto;
  }}
  h1 {{
    font-size: 22px; font-weight: 700; color: {accent};
    margin-bottom: 32px; padding-bottom: 16px;
    border-bottom: 2px solid {accent}44;
  }}
  p {{ margin-bottom: 16px; text-align: justify; }}
  ::-webkit-scrollbar {{ width: 6px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{ background: {accent}44; border-radius: 3px; }}
</style>
<title>{_escape_html(title)}</title>
</head>
<body>
<h1>{_escape_html(title)}</h1>
{body}
</body>
</html>"""
    return _create_window({
        "title": title,
        "html": html,
        "frameless": False,
        "width": 900,
        "height": 700,
        "x": x,
        "y": y,
        "min_size": (400, 300),
    })


def _escape_html(text: str) -> str:
    """转义 HTML 特殊字符。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _open_error_window(
    title: str,
    theme_seed: str,
    dark_mode: bool,
    x: int | None,
    y: int | None,
    message: str = "无法获取全文",
    detail: str = "该论文没有可用的本地 PDF 文件或远程链接。",
) -> bool:
    """显示错误提示窗口。"""
    html = _build_error_html(
        message=message,
        detail=detail,
        dark_mode=dark_mode,
        seed_color=theme_seed,
    )
    return _create_window({
        "title": title,
        "html": html,
        "frameless": False,
        "width": 500,
        "height": 300,
        "x": x,
        "y": y,
    })


# ── 方案 A：Flet 内嵌 PyMuPDF 轻量预览 ──

def render_preview(pdf_path: str, max_pages: int = 5) -> list[ft.Image]:
    """将 PDF 前 N 页转为 Flet Image 控件列表，用于内嵌快速预览。

    Args:
        pdf_path: 本地 PDF 文件路径
        max_pages: 最多渲染页数

    Returns:
        ft.Image 列表，可直接添加到 Flet Column 中展示
    """
    if not pdf_path or not os.path.isfile(pdf_path):
        return [ft.Text("PDF 文件不存在", italic=True, color=ft.Colors.OUTLINE)]

    images = []
    try:
        doc = fitz.open(pdf_path)
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            pix = page.get_pixmap(dpi=120)
            images.append(
                ft.Image(
                    src=f"data:image/png;base64,{base64.b64encode(pix.tobytes('png')).decode()}",
                    fit="contain",
                )
            )
        doc.close()
    except Exception as e:
        return [ft.Text(f"PDF 预览失败: {e}", italic=True, color=ft.Colors.ERROR)]

    return images or [ft.Text("PDF 无内容", italic=True, color=ft.Colors.OUTLINE)]


# ── 可用性检查 ──

def is_full_reader_available() -> bool:
    """检查 pywebview 是否可用（用于 UI 中按钮的 disabled 状态）。"""
    return _webview is not None


def is_downloader_available() -> bool:
    """检查 downloader 是否可用。"""
    return _download_pdf is not None
