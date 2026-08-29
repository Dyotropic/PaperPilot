"""
统一论文 PDF 下载 — 直链 HTTP。

支持的出版商:
  - arXiv, Nature, Springer (直链)
  - ACS, Wiley, IEEE, IOP, RSC, Science (guessed URL)
  - 其他: citation_pdf_url meta 标签

用法:
    from paperpilot.downloader import cache_pdf

    path = cache_pdf(paper)       # 下载 + 缓存, 返回本地路径

依赖:
    pip install curl_cffi PyMuPDF beautifulsoup4 lxml
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

import fitz
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────

_ARXIV_PDF = "https://arxiv.org/pdf/{}.pdf"
_ARXIV_HTML = "https://arxiv.org/html/{}"
_NATURE_PDF = "https://www.nature.com/articles/{}.pdf"
_SPRINGER_PDF = "https://link.springer.com/content/pdf/{}.pdf"
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")

DEFAULT_CACHE_DIR = Path.home() / ".paperpilot_pdf_cache"
MAX_CACHE_MB = 512

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ══════════════════════════════════════════════════════════════════
# 出版商检测
# ══════════════════════════════════════════════════════════════════

_PUBLISHERS = [
    ("sciencedirect", lambda d, u: "sciencedirect.com" in u or "elsevier" in u or "10.1016/" in d),
    ("nejm",          lambda d, u: "nejm.org" in u or "10.1056/" in d),
    ("arxiv",         lambda d, u: "arxiv.org" in u or "10.48550/" in d),
    ("nature",        lambda d, u: "nature.com" in u or "10.1038/" in d),
    ("springer",      lambda d, u: "link.springer.com" in u),
    ("acs",           lambda d, u: "pubs.acs.org" in u or "10.1021/" in d),
    ("wiley",         lambda d, u: "onlinelibrary.wiley.com" in u or "10.1002/" in d),
    ("science",       lambda d, u: "science.org" in u or "10.1126/" in d),
    ("ieee",          lambda d, u: "ieeexplore.ieee.org" in u or "10.1109/" in d),
    ("iop",           lambda d, u: "iop.org" in u or "10.1088/" in d),
    ("rsc",           lambda d, u: "rsc.org" in u or "10.1039/" in d),
]


def detect_publisher(paper: dict) -> str:
    """根据 paper 的 doi/url 检测出版商。"""
    doi = paper.get("doi", "") or ""
    url = (paper.get("url", "") or "").lower()
    for name, check in _PUBLISHERS:
        if check(doi, url):
            return name
    return "unknown"


# ══════════════════════════════════════════════════════════════════
# 直链下载
# ══════════════════════════════════════════════════════════════════

def _direct_download(paper: dict) -> bytes | None:
    """下载 PDF（浏览器指纹伪装 + 落地页预热会话）。

    链路: arXiv → Nature → Springer → Guessed URL（先预热落地页）→ citation_pdf_url meta

    反爬现状（2026-08 实测）：Wiley/ACS/RSC/Science 等对自动化客户端在
    落地页阶段就返回 403 挑战页（浏览器可静默通过 JS 挑战，脚本不能），
    因此直链下载仅对开放获取/轻防护出版商稳定；其余依赖浏览器兜底。

    Returns:
        PDF bytes 或 None
    """
    session, impersonate = _new_session()
    try:
        return _direct_download_with(paper, session, impersonate)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _new_session():
    """构建带浏览器指纹的下载会话；不支持高档位时逐级回退。

    Returns:
        (session, impersonate) — curl_cffi 不可用时返回 (None, "")
    """
    try:
        from curl_cffi.requests import Session
    except ImportError:
        return None, ""

    for target in ("chrome136", "chrome133a", "chrome124", "chrome120"):
        try:
            return Session(impersonate=target, timeout=20), target
        except Exception:
            continue
    return None, ""


def _direct_download_with(paper: dict, session, impersonate: str) -> bytes | None:
    """_direct_download 的会话化实现（session 为 None 时回退纯 requests）。"""
    if session is not None:
        def _get(url, timeout=20, allow_redirects=True):
            kw = {"timeout": timeout, "impersonate": impersonate}
            if allow_redirects is not True:
                kw["allow_redirects"] = allow_redirects
            return session.get(url, headers=headers, **kw)
    else:
        import requests as _http

        def _get(url, timeout=20, allow_redirects=True):
            kw = {"timeout": timeout}
            if allow_redirects is not True:
                kw["allow_redirects"] = allow_redirects
            return _http.get(url, headers=headers, **kw)

    doi = paper.get("doi") or ""
    url = paper.get("url") or ""

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    }

    # ① arXiv（服务端渲染，无需预热）
    arxiv_id = None
    m = _ARXIV_ID_RE.search(doi or url or "")
    if m:
        arxiv_id = m.group(1)
    if arxiv_id:
        try:
            resp = _get(_ARXIV_PDF.format(arxiv_id), timeout=15)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: arXiv 直链")
                return resp.content
        except Exception:
            pass

    # ② 落地页预热：建立 cookie 会话，供后续出版商直链使用
    referer = _warm_landing(_get, doi)

    # ③ Nature（paper.url 与预热落地页都参与匹配：DOI 直链场景落地页才含 nature.com）
    nature_id = None
    m = re.search(r"nature\.com/articles/([^/?\s]+)", f"{url} {referer}")
    if m:
        nature_id = m.group(1)
    if nature_id:
        try:
            resp = _get(_NATURE_PDF.format(nature_id), timeout=15)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: Nature 直链")
                return resp.content
        except Exception:
            pass

    # ④ Springer
    if doi:
        try:
            resp = _get(_SPRINGER_PDF.format(doi), timeout=15)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: Springer 直链")
                return resp.content
        except Exception:
            pass

    # ⑤ Guessed PDF URL (ACS, Science, Wiley, IOP, RSC…)，带落地页 Referer
    guessed = _guess_pdf_url(paper)
    if guessed:
        try:
            warm_headers = dict(headers)
            if referer:
                warm_headers["Referer"] = referer
            if session is not None:
                resp = session.get(guessed, headers=warm_headers,
                                   timeout=20, impersonate=impersonate)
            else:
                import requests as _http
                resp = _http.get(guessed, headers=warm_headers, timeout=20)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: %s", guessed[:80])
                return resp.content
        except Exception:
            pass

    # ⑥ citation_pdf_url meta tag
    if doi:
        try:
            resp = _get(f"https://doi.org/{doi}", timeout=15)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
                if meta and meta.get("content"):
                    pdf_url = meta["content"]
                    pdf_resp = _get(pdf_url, timeout=15)
                    if pdf_resp.status_code == 200 and pdf_resp.content[:5] == b"%PDF-":
                        logger.info("PDF 来源: citation_pdf_url")
                        return pdf_resp.content
        except Exception:
            pass

    return None


def _warm_landing(_get, doi: str) -> str:
    """预热：访问 DOI 落地页建立 cookie 会话，返回最终落地 URL（作 Referer）。

    纯增进性步骤：失败静默返回空串，不影响主链路。
    """
    if not doi:
        return ""
    try:
        resp = _get(f"https://doi.org/{doi}", timeout=15)
        if resp.status_code == 200:
            return str(resp.url)
    except Exception:
        pass
    return ""


def _guess_pdf_url(paper: dict) -> str | None:
    """根据 DOI/URL 构造常见出版商 PDF 直链。"""
    doi = paper.get("doi") or ""
    url = (paper.get("url") or "").lower()

    if "10.1021/" in doi or "pubs.acs.org" in url:
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    if "10.1126/" in doi or "science.org" in url:
        return f"https://www.science.org/doi/pdf/{doi}?download=true"
    if "10.1002/" in doi or "wiley.com" in url:
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    if "10.1109/" in doi or "ieeexplore.ieee.org" in url:
        return f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={doi}"
    if "10.1088/" in doi or "iop.org" in url:
        return f"https://iopscience.iop.org/article/{doi}/pdf"
    if "10.1039/" in doi or "rsc.org" in url:
        return f"https://pubs.rsc.org/en/content/articlepdf/{doi.split('/')[-1]}"
    if "10.1056/" in doi or "nejm.org" in url:
        return f"https://www.nejm.org/doi/pdf/{doi}"

    return None


# ══════════════════════════════════════════════════════════════════
# 公开 API
# ══════════════════════════════════════════════════════════════════

def pdf_direct_url(paper: dict) -> str | None:
    """返回论文的猜测 PDF 直链（供"用浏览器下载"兜底按钮使用）。

    浏览器可通过 Cloudflare 的 JS 挑战（脚本不能），对被反爬拦截的
    出版商（Wiley/ACS/RSC/Science 等）是最可靠的获取途径。

    Returns:
        PDF 直链 URL；无可用猜测链时返回 None（调用方回退 DOI 落地页）
    """
    return _guess_pdf_url(paper)


def download_pdf(paper: dict) -> bytes | None:
    """下载论文 PDF（直链 HTTP）。

    支持的出版商: arXiv, Nature, Springer, ACS, Wiley, IEEE, IOP, RSC, Science

    Returns:
        PDF bytes, 或 None（直链不可用）
    """
    return _direct_download(paper)


def cache_pdf(
    paper: dict,
    cache_dir: str | Path | None = None,
) -> str | None:
    """下载 PDF 并缓存到本地, 返回文件路径。

    缓存命中时直接返回路径, 不重复下载。
    文件以 DOI 命名, 超出 512MB 自动清理最旧文件。

    Args:
        paper: paper dict
        cache_dir: 缓存目录, 默认 ~/.paperpilot_pdf_cache/

    Returns:
        PDF 文件路径, 或 None
    """
    target = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    doi = paper.get("doi") or ""
    if doi:
        safe = doi.replace("/", "_").replace("\\", "_")
        filename = f"{safe}.pdf"
    else:
        title = paper.get("title", "") or paper.get("url", "") or str(id(paper))
        h = hashlib.sha256(title.encode()).hexdigest()[:16]
        filename = f"{h}.pdf"

    filepath = target / filename

    # 缓存命中
    if filepath.exists() and filepath.stat().st_size > 0:
        logger.info("PDF 缓存命中: %s", filepath)
        return str(filepath)

    # 下载
    pdf_bytes = download_pdf(paper)
    if not pdf_bytes:
        return None

    target.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(pdf_bytes)
    logger.info("PDF 已缓存: %s (%d bytes)", filepath, len(pdf_bytes))

    _cleanup_cache(target)
    return str(filepath)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """从 PDF 字节流提取纯文本 (PyMuPDF)。"""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts = []
        total = 0
        for page in doc:
            t = page.get_text()
            parts.append(t)
            total += len(t)
            if total >= 150000:
                break
        doc.close()
        return "".join(parts)
    except Exception:
        return ""


def _cleanup_cache(cache_dir: Path, max_mb: int = MAX_CACHE_MB) -> None:
    """清理缓存目录，超出上限时删除最旧文件。"""
    files = sorted(cache_dir.glob("*.pdf"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files) / (1024 * 1024)
    while total > max_mb and len(files) > 1:
        oldest = files.pop(0)
        total -= oldest.stat().st_size / (1024 * 1024)
        oldest.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════
# HTML 全文提取
# ══════════════════════════════════════════════════════════════════

HTML_CACHE_DIR = Path.home() / ".paperpilot_html_cache"


def _fetch_arxiv_html(paper: dict) -> str | None:
    """arXiv HTTP 快速路径：无需 CDP 浏览器，直接 requests 抓取。

    arXiv 的 HTML 版本（arxiv.org/html/{id}）是服务端渲染的纯 HTML，
    不需要 JS 执行，所以用简单的 HTTP GET 即可获取全文。

    Returns:
        缓存 HTML 文件路径，或 None
    """
    doi = paper.get("doi", "") or ""
    url = paper.get("url", "") or ""
    arxiv_id = None
    m = _ARXIV_ID_RE.search(doi or url)
    if m:
        arxiv_id = m.group(1)
    if not arxiv_id:
        return None

    cache_path = _html_cache_path(paper)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return str(cache_path)

    import urllib.request
    import urllib.error
    html_url = _ARXIV_HTML.format(arxiv_id)
    logger.info("arXiv HTTP 快速路径: %s", html_url)

    try:
        req = urllib.request.Request(
            html_url,
            headers={"User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.warning("arXiv HTTP 抓取失败: %s", e)
        return None

    soup = BeautifulSoup(raw, "lxml")
    # arXiv LaTeXML 转换后的正文容器
    body = soup.select_one("div.ltx_page_main, article.ltx_document, main")
    if not body:
        # 回退：移除 nav/footer 后取 body
        for tag in soup.select("nav, footer, script, style, .ltx_navbar"):
            tag.decompose()
        body = soup.find("body") or soup

    # 移除导航/页脚等噪音
    for tag in body.select("nav, footer, .ltx_navbar, .ltx_bibblock, .ltx_note_mark"):
        tag.decompose()

    body_html = str(body)
    if len(body_html) < 500:
        logger.warning("arXiv HTML 正文过短 (%d chars)，可能是仅摘要页面", len(body_html))
        return None

    title = paper.get("title", "") or (
        (soup.title.string if soup.title else None) or arxiv_id
    )
    full_html = _build_article_page(title, body_html)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(full_html, encoding="utf-8")
    logger.info("arXiv HTML 已缓存: %s (%d chars)", cache_path, len(full_html))
    return str(cache_path)


def fetch_full_text(paper: dict) -> str | None:
    """获取论文全文 HTML 缓存文件（仅 arXiv HTTP 快速路径）。

    其他出版商（SD/NEJM/Nature 等）的 HTML 提取暂不支持，
    请通过 PDF 下载获取全文。

    Args:
        paper: paper dict，需含 url / doi

    Returns:
        缓存 HTML 文件路径，或 None
    """
    doi = paper.get("doi", "")
    url = paper.get("url", "") or (f"https://doi.org/{doi}" if doi else "")
    if not url:
        return None

    # 缓存命中
    cache_path = _html_cache_path(paper)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info("HTML 缓存命中: %s", cache_path)
        return str(cache_path)

    # arXiv HTTP 快速路径（服务端渲染，无需浏览器）
    if _ARXIV_ID_RE.search(doi or url):
        return _fetch_arxiv_html(paper)

    # 其他来源暂不支持 HTML 提取
    logger.info("HTML 提取: 非 arXiv 来源，跳过 (%s)", url[:80])
    return None


def _html_cache_path(paper: dict) -> Path:
    doi = paper.get("doi", "")
    if doi:
        safe = doi.replace("/", "_").replace("\\", "_")
        return HTML_CACHE_DIR / f"{safe}.html"
    title = paper.get("title", "") or paper.get("url", "") or str(id(paper))
    h = hashlib.sha256(title.encode()).hexdigest()[:16]
    return HTML_CACHE_DIR / f"{h}.html"
def _build_article_page(title: str, body_html: str) -> str:
    """组装完整的阅读页面。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape_html(title)}</title>
<style>
  :root {{
    --bg: #1a1a2e;
    --text: #d4d4d8;
    --heading: #e4e4e7;
    --link: #60a5fa;
    --border: #27272a;
    --caption: #71717a;
    --code-bg: #27272a;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: Georgia, 'Noto Serif SC', 'Source Han Serif SC', serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.8;
    padding: 40px 24px;
    max-width: 860px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 1.6em; color: var(--heading); margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }}
  h2 {{ font-size: 1.25em; color: var(--heading); margin: 28px 0 12px; }}
  h3 {{ font-size: 1.1em; color: var(--heading); margin: 20px 0 8px; }}
  h4, h5, h6 {{ color: var(--heading); margin: 16px 0 6px; }}
  p {{ margin: 10px 0; }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  img {{ max-width: 100%; height: auto; display: block; margin: 16px auto; border-radius: 4px; }}
  figure {{ margin: 20px 0; text-align: center; }}
  figcaption {{ font-size: 0.85em; color: var(--caption); margin-top: 6px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; overflow-x: auto; display: block; }}
  th, td {{ border: 1px solid var(--border); padding: 8px 12px; text-align: left; }}
  th {{ background: var(--code-bg); }}
  code {{ background: var(--code-bg); padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: var(--code-bg); padding: 16px; border-radius: 6px; overflow-x: auto; margin: 12px 0; }}
  blockquote {{ border-left: 3px solid var(--border); padding-left: 16px; color: var(--caption); margin: 12px 0; }}
  ul, ol {{ margin: 10px 0; padding-left: 24px; }}
  li {{ margin: 4px 0; }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
  section {{ margin: 16px 0; }}
  .abstract, .keywords {{ background: var(--code-bg); padding: 12px 16px; border-radius: 6px; margin: 12px 0; }}
  @media (max-width: 640px) {{
    body {{ padding: 16px 12px; font-size: 0.95em; }}
  }}
</style>
</head>
<body>
<h1>{_escape_html(title)}</h1>
{body_html}
</body>
</html>"""


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
