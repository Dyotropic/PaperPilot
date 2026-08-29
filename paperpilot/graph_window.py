"""知识图谱窗口（pywebview 独立子进程 + ECharts graph）。

复用 pdf_viewer._create_window 的"子进程 + HTML 临时文件 + file:/// 加载"方案，
经其新增的 js_api_factory 键注入 JS↔Python 桥（节点详情纯前端渲染，
仅"打开 PDF"回调 Python → 复用 pdf_viewer.open_full_reader）。

数据注入：graph_service 产出的 JSON 以占位符方式整体嵌入 HTML（单文件自包含）；
ECharts 引擎缓存于 ~/.paperpilot_echarts/，三级下载源失败时回退 CDN 直引。
"""

import json
import time
import urllib.request
from pathlib import Path

from paperpilot.pdf_viewer import _blend_with, _create_window, is_full_reader_available

try:
    import webview as _webview
except ImportError:
    _webview = None

# ── ECharts 引擎缓存（仿 pdf_viewer 的 PDF.js 缓存模式）──
_ECHARTS_DIR = Path.home() / ".paperpilot_echarts"
_ECHARTS_VERSION = "5.5.1"
_ECHARTS_FILE = "echarts.min.js"
# 国内可达性优先：npmmirror → jsdelivr → cdnjs
_ECHARTS_URLS = [
    f"https://registry.npmmirror.com/echarts/{_ECHARTS_VERSION}/files/dist/{_ECHARTS_FILE}",
    f"https://cdn.jsdelivr.net/npm/echarts@{_ECHARTS_VERSION}/dist/{_ECHARTS_FILE}",
    f"https://cdnjs.cloudflare.com/ajax/libs/echarts/{_ECHARTS_VERSION}/{_ECHARTS_FILE}",
]
_ECHARTS_MIN_SIZE = 300_000

_DATA_DIR = Path.home() / ".paperpilot_graph"


def is_graph_window_available() -> bool:
    """图谱窗口可用性（依赖 pywebview，与 PDF 阅读器同源）。"""
    return _webview is not None


def _ensure_echarts() -> str:
    """确保本地有 echarts.min.js，返回 file:/// 路径；全败返回空串（HTML 回退 CDN）。

    已缓存且体积合理 → 秒回；否则逐源下载（UA 头 + 20s 超时），
    内容以 '<' 开头视为错误页不落盘。
    """
    _ECHARTS_DIR.mkdir(parents=True, exist_ok=True)
    local = _ECHARTS_DIR / _ECHARTS_FILE
    if local.is_file() and local.stat().st_size >= _ECHARTS_MIN_SIZE:
        return local.as_uri()
    for url in _ECHARTS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PaperPilot"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            if len(data) < _ECHARTS_MIN_SIZE or data[:1] == b"<":
                continue
            tmp = local.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(local)
            return local.as_uri()
        except Exception:
            continue
    return ""


# ── JS ↔ Python 桥 ──

class _GraphBridge:
    """图窗口内 JS 回调 Python 的桥（在 pywebview 子进程内实例化）。

    构造时读取节点数据文件并立即删除（窗口关闭后的临时文件清理）。
    """

    def __init__(self, data_path: str, theme_seed: str = "#0097A7",
                 dark_mode: bool = False):
        self._papers: dict = {}
        self._theme_seed = theme_seed
        self._dark = dark_mode
        try:
            with open(data_path, encoding="utf-8") as f:
                data = json.load(f)
            for n in data.get("nodes", []):
                self._papers[n.get("id")] = n
            import os
            os.unlink(data_path)
        except Exception:
            pass

    def open_paper(self, paper_id):
        """打开论文 PDF 阅读器。返回 str：'ok' 或错误信息（JS 侧 toast 展示）。"""
        try:
            paper_id = int(paper_id)
        except (TypeError, ValueError):
            return "无效的论文 id"
        node = self._papers.get(paper_id)
        if not node:
            return "未找到论文数据"
        try:
            from paperpilot import pdf_viewer
            if not pdf_viewer.is_full_reader_available():
                return "PDF 阅读器组件不可用"
            ok = pdf_viewer.open_full_reader(
                dict(node), theme_seed=self._theme_seed, dark_mode=self._dark)
            return "ok" if ok else "未找到可用的全文来源"
        except Exception as e:  # 桥内异常必须转字符串，不能打断窗口
            return f"打开失败：{e}"


def _make_bridge(data_path: str, theme_seed: str = "#0097A7",
                 dark_mode: bool = False) -> _GraphBridge:
    """js_api_factory 点分路径的工厂（在子进程内被 importlib 调用）。"""
    return _GraphBridge(data_path, theme_seed, dark_mode)


# ── 主题配色 ──

def _theme_colors(seed: str, dark: bool) -> dict:
    """从主题种子色推导图谱窗口配色。

    画布保持中性高对比（浅色≈白、深色≈炭黑），主题色仅轻微点缀——
    分析用图可读性优先，像 dusk 这类高饱和主题不能把背景染紫。
    """
    if dark:
        return {
            "bg": _blend_with(seed, 0.90, "#10141d"),
            "panel": _blend_with(seed, 0.85, "#171d29"),
            "panel2": _blend_with(seed, 0.80, "#1d2432"),
            "text": "#e8ecf4",
            "muted": "#9aa4bb",
            "border": _blend_with(seed, 0.60, "#2a3247"),
            "hover": _blend_with(seed, 0.72, "#242e44"),
        }
    return {
        # factor 是"向目标色混合的比例"：浅色画布要 9 成以上的中性色，只留一点主题色调
        "bg": _blend_with(seed, 0.94, "#f5f6f8"),
        "panel": "#ffffff",
        "panel2": _blend_with(seed, 0.92, "#eef0f4"),
        "text": "#1f2637",
        "muted": "#5b6478",
        "border": _blend_with(seed, 0.80, "#dfe3ec"),
        "hover": _blend_with(seed, 0.88, "#edf0f5"),
    }


# ── HTML 模板 ──

_GRAPH_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<script src="__ECHARTS_SRC__"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:100%; height:100%; overflow:hidden; }
body { display:flex; flex-direction:column; background:__BG__; color:__TEXT__;
       font-family:"Segoe UI","Microsoft YaHei",sans-serif; font-size:14px; }
#header { display:flex; align-items:center; gap:12px; padding:10px 16px;
          background:__PANEL__; border-bottom:1px solid __BORDER__; }
#header .title { font-size:15px; font-weight:600; white-space:nowrap; }
#header .stats { font-size:12px; color:__MUTED__; white-space:nowrap; }
#tabs { display:flex; gap:6px; margin-left:auto; }
.tab { padding:5px 14px; border-radius:16px; cursor:pointer; font-size:13px;
       border:1px solid __BORDER__; background:transparent; color:__MUTED__;
       user-select:none; }
.tab.active { background:__SEED__; border-color:__SEED__; color:#fff; }
#relayout { padding:5px 12px; border-radius:16px; cursor:pointer; font-size:13px;
            border:1px solid __BORDER__; background:transparent; color:__MUTED__; }
#relayout:hover { background:__HOVER__; }
#warn { display:none; padding:6px 16px; font-size:12.5px; color:#b45309;
        background:#fef3c7; border-bottom:1px solid #fde68a; }
body.dark #warn { color:#fcd34d; background:#3a2e12; border-color:#57431a; }
#main { flex:1; display:flex; min-height:0; }
#chart { flex:1; min-width:0; }
#detail { display:none; width:330px; background:__PANEL__; border-left:1px solid __BORDER__;
          padding:16px; overflow-y:auto; }
#detail h2 { font-size:15px; line-height:1.45; margin-bottom:8px; }
#detail .meta { font-size:12.5px; color:__MUTED__; line-height:1.7; margin-bottom:8px; }
#detail .chiprow { display:flex; flex-wrap:wrap; gap:5px; margin:8px 0; }
.chip { font-size:11.5px; padding:2px 9px; border-radius:10px;
        background:__PANEL2__; border:1px solid __BORDER__; color:__MUTED__; }
.chip.score { color:__SEED__; border-color:__SEED__; font-weight:600; }
#detail .abstract { font-size:12.5px; line-height:1.65; color:__TEXT__;
                    background:__PANEL2__; border-radius:8px; padding:10px;
                    max-height:180px; overflow-y:auto; white-space:pre-wrap; }
#detail .counts { font-size:12px; color:__MUTED__; margin:8px 0; }
#detail .btns { display:flex; gap:8px; margin-top:12px; }
.btn { flex:1; padding:8px 0; border-radius:8px; border:none; cursor:pointer;
       font-size:13px; background:__SEED__; color:#fff; }
.btn.ghost { background:transparent; border:1px solid __BORDER__; color:__MUTED__; }
#toast { display:none; position:fixed; left:50%; bottom:36px; transform:translateX(-50%);
         background:__PANEL2__; color:__TEXT__; border:1px solid __BORDER__;
         padding:8px 18px; border-radius:8px; font-size:13px; z-index:99; }
#fatal { display:none; position:absolute; inset:0; background:__BG__; color:__MUTED__;
         align-items:center; justify-content:center; font-size:14px; }
</style>
</head>
<body>
<div id="header">
  <span class="title">__TITLE__</span>
  <span class="stats" id="stats"></span>
  <div id="tabs">
    <div class="tab active" data-view="cites">引用关系</div>
    <div class="tab" data-view="cooccur">关键词共现</div>
    <div class="tab" data-view="timeline">时间线</div>
  </div>
  <button id="relayout">重新布局</button>
</div>
<div id="warn"></div>
<div id="main">
  <div id="chart"></div>
  <div id="detail"></div>
</div>
<div id="toast"></div>
<div id="fatal">ECharts 引擎加载失败（本地缓存与在线源均不可达），请联网后重试。</div>
<script>
"use strict";
window.onerror = function(msg, src, line){
  var f = document.getElementById("fatal");
  f.style.display = "flex";
  f.textContent = "JS 错误: " + msg + " @line " + line;
};
var DATA = __GRAPH_DATA__;
var SEED = "__SEED__";
var IS_DARK = __IS_DARK__;
var VIEW = "cites";
var chart = null;

function $(id){ return document.getElementById(id); }
function toast(msg){
  var t = $("toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(function(){ t.style.display = "none"; }, 2600);
}

var SRC_META = [
  { key:"arxiv",     name:"arXiv",      color:"#E76F51" },
  { key:"openalex",  name:"OpenAlex",   color:"#3B82F6" },
  { key:"europepmc", name:"Europe PMC", color:"#10B981" },
  { key:"local_pdf", name:"本地 PDF",   color:"#8B5CF6" },
  { key:"unknown",   name:"未知来源",   color:"#94A3B8" }
];
var STATUS_TEXT = { unread:"未读", skimmed:"略读", deep_read:"精读" };

function srcMeta(key){
  for (var i = 0; i < SRC_META.length; i++)
    if (SRC_META[i].key === key) return SRC_META[i];
  return SRC_META[SRC_META.length - 1];
}

function init(){
  if (!window.echarts || !DATA.nodes.length) {
    $("stats").textContent = DATA.nodes.length ? "" : "课题内暂无论文";
    if (!DATA.nodes.length) return;
  }
  if (DATA.warnings && DATA.warnings.length) {
    var w = $("warn"); w.style.display = "block";
    w.textContent = "⚠ " + DATA.warnings.join("；");
  }
  try {
    chart = echarts.init($("chart"));
    window.addEventListener("resize", function(){ chart.resize(); });
    chart.on("click", onNodeClick);
    chart.on("dblclick", onNodeDblClick);
    buildTabs(); render();
  } catch (err) {
    var f = $("fatal"); f.style.display = "flex";
    f.textContent = "图谱初始化失败: " + (err && err.message || err);
  }
}

function nodeDegree(n){ return n.cite_in * 2 + n.cite_out + n.cooccur; }

function buildNodes(view, catIndex){
  return DATA.nodes.map(function(n){
    var m = srcMeta(n.source);
    var size = view === "timeline" ? 14 : 14 + Math.min(30, nodeDegree(n) * 2.2);
    return {
      id: String(n.id), name: n.label || n.title,
      symbolSize: size,
      // 力导向视图不喂时间线坐标做初值（会把节点压成一条横带），交给随机初始化
      x: view === "timeline" ? n.tx : undefined,
      y: view === "timeline" ? n.ty : undefined,
      category: catIndex[n.source] !== undefined ? catIndex[n.source] : 0,
      itemStyle: { color: m.color,
                   borderColor: IS_DARK ? "#10141d" : "#ffffff",
                   borderWidth: 1.2 },
      label: { show: DATA.nodes.length <= 80, position: "right",
               fontSize: 12, fontWeight: 500,
               color: IS_DARK ? "#dbe2ef" : "#2a3346",
               width: 170, overflow: "truncate" },
      paper: n
    };
  });
}

function buildEdges(view){
  var kind = view === "cooccur" ? "cooccur" : "cites";
  var list = DATA.edges.filter(function(e){ return e.kind === kind; });
  // 共现边常近全连接（毛球）：自适应只画强关联（阈值 ≤4，随数据封顶）
  if (kind === "cooccur" && list.length) {
    var maxW = 0;
    list.forEach(function(e){ if (e.weight > maxW) maxW = e.weight; });
    var th = Math.min(4, maxW);
    if (th > 2) list = list.filter(function(e){ return e.weight >= th; });
  }
  return list.map(function(e){
    return {
      source: String(e.source), target: String(e.target),
      shared: e.shared, kind: e.kind, weight: e.weight,
      lineStyle: e.kind === "cites"
        ? { color: IS_DARK ? "rgba(150,160,185,0.4)" : "rgba(70,80,110,0.5)",
            width: 1.4, curveness: 0.18,
            type: "solid" }
        : { color: IS_DARK ? "rgba(120,200,170,0.22)" : "rgba(16,140,100,0.28)",
            width: 0.7 + Math.min(4, e.weight * 0.7), curveness: 0.12,
            type: "dashed" },
      emphasis: { lineStyle: { width: e.kind === "cites" ? 2.2 : 1.2 + Math.min(4, e.weight * 0.7) } }
    };
  });
}

function currentOption(){
  var usedSrc = {};
  DATA.nodes.forEach(function(n){ usedSrc[n.source] = 1; });
  var categories = SRC_META.filter(function(m){ return usedSrc[m.key]; });
  var catIndex = {};
  categories.forEach(function(c, i){ catIndex[c.key] = i; });
  var view = VIEW;
  return {
    backgroundColor: "transparent",
    tooltip: {
      confine: true, enterable: false,
      formatter: function(p){
        if (p.dataType === "edge") {
          var e = p.data;
          if (e.kind === "cooccur" && e.shared && e.shared.length)
            return "共同关键词(" + e.weight + ")：" + e.shared.join("、");
          return "前者引用后者";
        }
        var n = p.data.paper;
        var lines = ["<b>" + esc(n.title) + "</b>",
          (n.authors || "").slice(0, 80), n.year || "年份未知"];
        return lines.join("<br>");
      }
    },
    legend: view === "timeline" ? [] : [{
      data: categories.map(function(c){ return c.name; }),
      textStyle: { color: IS_DARK ? "#c8d0e0" : "#475069", fontSize: 11 },
      itemWidth: 12, itemHeight: 8, top: 8, left: 10,
      selected: categories.reduce(function(o, c){ o[c.name] = true; return o; }, {})
    }],
    series: [{
      type: "graph", layout: view === "timeline" ? "none" : "force",
      roam: true, draggable: true,
      // 斥力/边长随节点数缩放；共现图更稀疏（其边天然偏多）
      force: view === "cooccur"
        ? { repulsion: Math.max(3200, DATA.nodes.length * 70),
            edgeLength: [110, 320], gravity: 0.06, friction: 0.2 }
        : { repulsion: Math.max(1800, DATA.nodes.length * 45),
            edgeLength: [90, 260], gravity: 0.08, friction: 0.2 },
      categories: categories,
      data: buildNodes(view, catIndex),
      links: buildEdges(view),
      emphasis: { focus: "adjacency", itemStyle: { borderColor: SEED, borderWidth: 2 } },
      lineStyle: { opacity: 1 },
      labelLayout: { hideOverlap: true }
    }]
  };
}

function esc(s){
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function render(){
  chart.setOption(currentOption(), true);
  var cites = 0, co = 0;
  DATA.edges.forEach(function(e){ e.kind === "cites" ? cites++ : co++; });
  $("stats").textContent =
    DATA.nodes.length + " 篇论文 · 引用边 " + cites + " · 共现边 " + co;
}

function buildTabs(){
  var tabs = document.querySelectorAll(".tab");
  tabs.forEach(function(t){
    t.onclick = function(){
      tabs.forEach(function(x){ x.classList.remove("active"); });
      t.classList.add("active");
      VIEW = t.getAttribute("data-view");
      render();
    };
  });
  $("relayout").onclick = function(){ render(); };
}

var _selected = null;
chart; // noop guard
function onNodeClick(params){
  if (params.dataType !== "node") return;
  showDetail(params.data.paper);
}
function onNodeDblClick(params){
  if (params.dataType !== "node") return;
  openPaper(params.data.paper.id);
}

function showDetail(n){
  _selected = n;
  var m = srcMeta(n.source);
  var d = $("detail"); d.style.display = "block";
  var chips = "";
  if (n.ai_score != null) chips += '<span class="chip score">AI ' + Math.round(n.ai_score) + "</span>";
  if (n.total_score != null) chips += '<span class="chip score">CE ' + Math.round(n.total_score) + "</span>";
  chips += '<span class="chip">' + (STATUS_TEXT[n.status] || n.status) + "</span>";
  chips += '<span class="chip">' + m.name + "</span>";
  if (n.year) chips += '<span class="chip">' + n.year + "</span>";
  var kws = (n.keywords || []).map(function(k){ return '<span class="chip">' + esc(k) + "</span>"; }).join("");
  d.innerHTML =
    "<h2>" + esc(n.title) + "</h2>" +
    '<div class="meta">' + esc(n.authors || "作者未知") + "</div>" +
    '<div class="chiprow">' + chips + "</div>" +
    (kws ? '<div class="chiprow">' + kws + "</div>" : "") +
    '<div class="counts">被引(图内) ' + n.cite_in + " · 引用 " + n.cite_out +
    " · 共现 " + n.cooccur + "</div>" +
    (n.abstract ? '<div class="abstract">' + esc(n.abstract.slice(0, 1200)) +
      (n.abstract.length > 1200 ? " …" : "") + "</div>" : "") +
    '<div class="btns"><button class="btn" id="btn-open">打开 PDF</button>' +
    '<button class="btn ghost" id="btn-close">关闭</button></div>';
  $("btn-open").onclick = function(){ openPaper(n.id); };
  $("btn-close").onclick = function(){ d.style.display = "none"; _selected = null; };
}

function openPaper(paperId){
  if (!window.pywebview || !window.pywebview.api) {
    toast("本地桥接未就绪，请稍后重试"); return;
  }
  toast("正在打开…");
  window.pywebview.api.open_paper(paperId).then(function(r){
    if (r !== "ok") toast(r || "打开失败");
  }).catch(function(e){ toast("打开失败：" + e); });
}

// ── ECharts 加载兜底：script 标签失败时逐源重试 ──
// 必须放在全部定义之后：本地缓存命中时 echarts 同步可用，init 会立刻执行，
// 提前调用会撞上尚未赋值的 var（SRC_META 等）导致 undefined.filter。
var FALLBACK_SOURCES = __ECHARTS_FALLBACKS__;
(function ensureEcharts(i){
  if (window.echarts) return init();
  if (i >= FALLBACK_SOURCES.length) { $("fatal").style.display = "flex"; return; }
  var s = document.createElement("script");
  s.src = FALLBACK_SOURCES[i];
  s.onload = function(){ init(); };
  s.onerror = function(){ ensureEcharts(i + 1); };
  document.head.appendChild(s);
})(0);
</script>
</body>
</html>
"""


def open_graph_window(project_name: str, graph_data: dict, *,
                      theme_seed: str = "#0097A7",
                      dark_mode: bool = False) -> bool:
    """打开知识图谱独立窗口。

    Args:
        project_name: 课题名（窗口标题展示）
        graph_data: graph_service.build_graph_data() 的返回值
        theme_seed: 主题种子色（与主窗口一致）
        dark_mode: 是否夜间模式

    Returns:
        True=窗口已启动；False=pywebview 不可用
    """
    if not is_full_reader_available():
        return False

    # 节点数据落盘供桥读取（桥加载后自删）；顺带清理超过 1 天的残留
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in _DATA_DIR.glob("graph_*.json"):
        try:
            if now - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass
    slim_nodes = [
        {k: n.get(k) for k in
         ("id", "title", "authors", "doi", "url", "pdf_path", "abstract")}
        for n in graph_data.get("nodes", [])
    ]
    data_path = _DATA_DIR / f"graph_{int(now * 1000)}.json"
    data_path.write_text(
        json.dumps({"nodes": slim_nodes}, ensure_ascii=False), encoding="utf-8")

    colors = _theme_colors(theme_seed, dark_mode)
    local_js = _ensure_echarts()
    primary_js = local_js or _ECHARTS_URLS[0]
    fallbacks = _ECHARTS_URLS if local_js else _ECHARTS_URLS[1:]
    html = (
        _GRAPH_HTML
        .replace("__TITLE__", f"知识图谱 · {project_name}")
        .replace("__GRAPH_DATA__", json.dumps(
            {k: graph_data.get(k, []) for k in ("nodes", "edges", "warnings")},
            ensure_ascii=False).replace("</", "<\\/"))
        .replace("__ECHARTS_SRC__", primary_js)
        .replace("__ECHARTS_FALLBACKS__", json.dumps(fallbacks))
        .replace("__IS_DARK__", "true" if dark_mode else "false")
        .replace("__SEED__", theme_seed)
        .replace("__BG__", colors["bg"])
        .replace("__PANEL__", colors["panel"])
        .replace("__PANEL2__", colors["panel2"])
        .replace("__TEXT__", colors["text"])
        .replace("__MUTED__", colors["muted"])
        .replace("__BORDER__", colors["border"])
        .replace("__HOVER__", colors["hover"])
    )
    return _create_window({
        "title": f"知识图谱 · {project_name}",
        "html": html,
        "width": 1280,
        "height": 800,
        "min_size": (900, 600),
        "js_api_factory": "paperpilot.graph_window:_make_bridge",
        "js_api_args": [str(data_path), theme_seed, dark_mode],
    })
