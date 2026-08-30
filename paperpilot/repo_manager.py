"""文献仓库管理 — 项目级 PDF 组织、论文目录、检索缓存。

目录结构：
    repository/
      课题A/
        catalog.json   # {papers: {doi_or_key: {title, authors, year, journal, pdf_name, imported_at}}}
        pdfs/
          Wang_2016_Stability_of_Perovskite_SolEnergy.pdf
      课题B/
        catalog.json
        pdfs/
    cache/
      cache_index.json  # {files: {doi_or_key: {file, size, last_access}}}
      pdfs/
"""

import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from datetime import datetime


def _get_app_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


_REPO_ROOT = _get_app_dir() / "repository"
_CACHE_DIR = _get_app_dir() / "cache"
_CACHE_PDFS = _CACHE_DIR / "pdfs"
_CACHE_INDEX = _CACHE_DIR / "cache_index.json"
_CACHE_MAX = 500 * 1024 * 1024  # 500MB


# ── 文件命名 ──

_PLACEHOLDER_AUTHORS = {
    "unknown", "anonymous", "anon", "佚名", "匿名", "未知",
    "no author", "n a", "na", "none",
}


def make_pdf_name(paper: dict) -> str:
    """生成规范文件名：{作者}_{年份}_{标题}_{期刊}.pdf"""
    authors = (paper.get("authors") or "Unknown").strip()
    first = authors.split(",")[0].strip()
    author_parts = first.split()
    surname = author_parts[-1] if author_parts else "Unknown"
    surname = re.sub(r"[^\w]", "", surname)[:30]
    # 回退：空字符串 / 占位作者名 / 含乱码字符
    if not surname or re.search(r"[^\x00-\x7f一-鿿㐀-䶿]", surname):
        surname = "Unknown"
    if surname.lower() in _PLACEHOLDER_AUTHORS:
        surname = "Unknown"

    # 无年份用 "Unknown" 占位：不能用 "????"，`?` 在 Windows 文件名中非法
    year = str(paper.get("year") or "Unknown")
    title = (paper.get("title") or "untitled").strip()
    title_slug = re.sub(r"[^\w\s-]", "", title[:60])
    title_slug = re.sub(r"[-\s]+", "_", title_slug).strip("_") or "untitled"

    journal = (paper.get("journal") or "").strip()
    journal_slug = re.sub(r"[^\w]", "", journal)[:20] if journal else ""

    parts = [surname, year, title_slug]
    if journal_slug:
        parts.append(journal_slug)
    name = "_".join(parts)[:200]
    # 最终防线：清除 Windows 保留非法字符（<>:"/\|?*），防止未来字段变动引入
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name + ".pdf"


# ── Catalog 管理 ──

def _catalog_path(project_name: str) -> Path:
    safe = re.sub(r"[^\w\s\-]", "", project_name)[:80]
    return _REPO_ROOT / safe / "catalog.json"


def _pdf_dir(project_name: str) -> Path:
    safe = re.sub(r"[^\w\s\-]", "", project_name)[:80]
    return _REPO_ROOT / safe / "pdfs"


def rename_project(old_name: str, new_name: str) -> bool:
    """重命名课题仓库文件夹（含 catalog + conversation + pdfs）。"""
    old_safe = re.sub(r"[^\w\s\-]", "", old_name)[:80]
    new_safe = re.sub(r"[^\w\s\-]", "", new_name)[:80]
    old_dir = _REPO_ROOT / old_safe
    new_dir = _REPO_ROOT / new_safe
    if old_dir.is_dir() and not new_dir.exists() and old_safe != new_safe:
        old_dir.rename(new_dir)
        return True
    return False


def load_catalog(project_name: str) -> dict:
    """读取课题的 catalog.json，不存在则返回空结构。"""
    path = _catalog_path(project_name)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"papers": {}}


def save_catalog(project_name: str, catalog: dict) -> None:
    """写入课题目录。"""
    path = _catalog_path(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_key(paper: dict) -> str | None:
    """论文唯一标识：DOI 优先，否则标题 hash。"""
    doi = (paper.get("doi") or "").strip()
    if doi:
        return doi
    title = (paper.get("title") or "").strip()
    if title:
        return "title:" + hashlib.md5(title.lower().encode()).hexdigest()[:12]
    return None


def add_to_catalog(project_name: str, paper: dict, pdf_name: str) -> None:
    """将一篇论文添加到课题目录。"""
    catalog = load_catalog(project_name)
    key = _make_key(paper)
    if not key:
        return
    catalog["papers"][key] = {
        "title": (paper.get("title") or "").strip(),
        "authors": (paper.get("authors") or "").strip(),
        "year": paper.get("year"),
        "journal": (paper.get("journal") or paper.get("source") or "").strip(),
        "doi": (paper.get("doi") or "").strip(),
        "pdf_name": pdf_name,
        "imported_at": datetime.now().isoformat(),
    }
    save_catalog(project_name, catalog)


def find_in_all_catalogs(paper: dict) -> list[tuple[str, str, str]]:
    """在所有课题 catalog 中查找论文 → [(project_name, pdf_name, pdf_abs_path), ...]"""
    key = _make_key(paper)
    if not key or not _REPO_ROOT.is_dir():
        return []

    # 构建候选 key 列表：DOI / 标题 hash 都可能匹配
    keys = [key]
    doi = (paper.get("doi") or "").strip()
    title = (paper.get("title") or "").strip()
    if doi and title:
        title_key = "title:" + hashlib.md5(title.lower().encode()).hexdigest()[:12]
        if title_key != key:
            keys.append(title_key)

    results = []
    for proj_dir in sorted(_REPO_ROOT.iterdir()):
        if not proj_dir.is_dir():
            continue
        catalog = load_catalog(proj_dir.name)
        papers = catalog.get("papers", {})
        for k in keys:
            if k in papers:
                entry = papers[k]
                pname = entry.get("pdf_name", "")
                ppath = str(_pdf_dir(proj_dir.name) / pname) if pname else ""
                results.append((proj_dir.name, pname, ppath))
                break
    return results


# ── PDF 导入 ──

def import_pdf(paper: dict, project_name: str) -> str | None:
    """导入一篇论文的 PDF。

    1. 检查 catalog 是否已有该论文的 PDF → 已有且文件存在则直接返回
    2. 生成规范文件名，拷贝到 project/pdfs/
    3. 更新 catalog.json
    4. 同步到其他已有该论文的课题
    """
    pdf_dir = _pdf_dir(project_name)
    key = _make_key(paper)

    # ① 已有 PDF 且文件存在 → 直接返回，避免重复拷贝
    if key:
        catalog = load_catalog(project_name)
        existing = catalog.get("papers", {}).get(key, {})
        existing_name = existing.get("pdf_name", "")
        if existing_name:
            existing_path = pdf_dir / existing_name
            if existing_path.is_file():
                return str(existing_path)

    # ② 需要导入：源文件必须有效
    src = paper.get("pdf_path")
    if not src or not os.path.isfile(src):
        return None

    pdf_dir.mkdir(parents=True, exist_ok=True)

    pdf_name = make_pdf_name(paper)
    dst = str(pdf_dir / pdf_name)

    # ③ 同名文件：大小相同 → 跳过拷贝；大小不同 → 同 key 在 ① 已命中，
    #    到这说明是同名不同论文，加 hash 后缀避免覆盖
    if os.path.isfile(dst):
        if os.path.getsize(dst) == os.path.getsize(src):
            add_to_catalog(project_name, paper, pdf_name)
            if dst != src:
                _sync_to_other_projects(paper, dst, project_name)
            return dst
        base = os.path.splitext(pdf_name)[0][:190]
        suffix = (key or hashlib.md5(str(src).encode()).hexdigest()[:8])
        suffix = suffix.replace("/", "_").replace(":", "_")[:12]
        pdf_name = f"{base}_{suffix}.pdf"
        dst = str(pdf_dir / pdf_name)

    # ④ 拷贝
    copy_failed = False
    if not os.path.isfile(dst):
        try:
            shutil.copy2(src, dst)
        except OSError:
            # 拷贝失败时不得把 catalog 指向不存在的 pdfs/{pdf_name}——
            # 登记源文件原名，保证 catalog 条目真实可解析
            dst = src
            copy_failed = True

    # ⑤ 更新 catalog
    add_to_catalog(project_name, paper,
                   os.path.basename(dst) if copy_failed else pdf_name)

    # ⑥ 同步到其他已有该论文的课题
    if dst != src:
        _sync_to_other_projects(paper, dst, project_name)

    return dst


def _sync_to_other_projects(paper: dict, src_pdf: str, skip_project: str) -> None:
    """将新导入的 PDF 同步到其他已有该论文的课题（优先硬链接）。"""
    existing = find_in_all_catalogs(paper)
    for proj_name, _, existing_path in existing:
        if proj_name == skip_project:
            continue
        if existing_path and os.path.isfile(existing_path):
            continue  # 已有文件，不覆盖

        pdf_dir = _pdf_dir(proj_name)
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_name = make_pdf_name(paper)
        dst = str(pdf_dir / pdf_name)

        if os.path.isfile(dst):
            continue

        try:
            os.link(src_pdf, dst)  # 硬链接，不占额外空间
        except OSError:
            try:
                shutil.copy2(src_pdf, dst)  # 回退到拷贝
            except OSError:
                continue

        add_to_catalog(proj_name, paper, pdf_name)


# ── 检索页缓存 ──

def _load_cache_index() -> dict:
    if _CACHE_INDEX.is_file():
        try:
            return json.loads(_CACHE_INDEX.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"files": {}, "total_size": 0}


def _save_cache_index(idx: dict) -> None:
    _CACHE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_pdf(paper: dict, src_path: str) -> str | None:
    """将检索页下载的 PDF 放入缓存，LRU 管理。"""
    if not os.path.isfile(src_path):
        return None

    key = _make_key(paper)
    if not key:
        return None

    _CACHE_PDFS.mkdir(parents=True, exist_ok=True)

    file_size = os.path.getsize(src_path)

    # 已有缓存且文件存在 → 只更新访问时间
    idx = _load_cache_index()
    if key in idx["files"]:
        existing_path = _CACHE_PDFS / idx["files"][key]["file"]
        if existing_path.is_file():
            idx["files"][key]["last_access"] = datetime.now().isoformat()
            _save_cache_index(idx)
            return str(existing_path)
        # 文件丢了，清理索引
        del idx["files"][key]
        idx["total_size"] = sum(f["size"] for f in idx["files"].values())

    # 新缓存：用规范文件名
    cache_name = make_pdf_name(paper)
    dst = str(_CACHE_PDFS / cache_name)

    # 重名去重
    if os.path.isfile(dst):
        existing_size = os.path.getsize(dst)
        if existing_size != file_size:
            base = os.path.splitext(cache_name)[0][:190]
            suffix = key.replace("/", "_").replace(":", "_")[:12]
            cache_name = f"{base}_{suffix}.pdf"
            dst = str(_CACHE_PDFS / cache_name)

    # LRU 清理
    while idx["total_size"] + file_size > _CACHE_MAX and idx["files"]:
        _evict_one(idx)
    try:
        shutil.copy2(src_path, dst)
    except OSError:
        return None
    idx["files"][key] = {
        "file": cache_name,
        "size": file_size,
        "last_access": datetime.now().isoformat(),
    }
    idx["total_size"] += file_size
    _save_cache_index(idx)

    return dst


def get_cached_pdf(paper: dict) -> str | None:
    """查找缓存中的 PDF。"""
    key = _make_key(paper)
    if not key:
        return None
    idx = _load_cache_index()
    if key not in idx["files"]:
        return None
    cache_name = idx["files"][key]["file"]
    path = str(_CACHE_PDFS / cache_name)
    if os.path.isfile(path):
        idx["files"][key]["last_access"] = datetime.now().isoformat()
        _save_cache_index(idx)
        return path
    # 文件丢了，清理索引
    del idx["files"][key]
    idx["total_size"] = sum(f["size"] for f in idx["files"].values())
    _save_cache_index(idx)
    return None


def check_cache_for_import(paper: dict) -> str | None:
    """检查缓存中是否有该论文 PDF，有则返回路径（用于直接导入课题）。"""
    return get_cached_pdf(paper)


def _evict_one(idx: dict) -> None:
    """淘汰最久未访问的缓存文件。"""
    if not idx["files"]:
        return
    oldest_key = min(idx["files"], key=lambda k: idx["files"][k].get("last_access", ""))
    info = idx["files"].pop(oldest_key)
    file_path = _CACHE_PDFS / info["file"]
    try:
        os.unlink(file_path)
    except OSError:
        pass
    idx["total_size"] = max(0, idx["total_size"] - info.get("size", 0))


# ── 回收站 ──

_RECYCLE_DIR = _get_app_dir() / "repository" / ".recycle"
_RECYCLE_RETENTION_DAYS = 7


def move_project_to_recycle(project_name: str) -> bool:
    """将课题目录整个移到回收站，7 天后自动清理。

    Returns:
        True 表示移动成功，False 表示课题目录不存在
    """
    safe = re.sub(r"[^\w\s\-]", "", project_name)[:80]
    proj_dir = _REPO_ROOT / safe
    if not proj_dir.is_dir():
        return False

    _RECYCLE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = _RECYCLE_DIR / f"{safe}_{ts}"
    try:
        shutil.move(str(proj_dir), str(dest))
        return True
    except OSError:
        return False


def remove_paper_from_catalog(project_name: str, paper: dict) -> bool:
    """从课题目录中移除一篇论文：删 catalog 条目 + 移 PDF 到回收站。

    Args:
        project_name: 课题名
        paper: paper dict（含 doi / title 用于匹配）

    Returns:
        True 表示有变更，False 表示未找到匹配条目
    """
    key = _make_key(paper)
    if not key:
        return False
    catalog = load_catalog(project_name)
    entry = catalog.get("papers", {}).get(key)
    if not entry:
        return False

    pdf_name = entry.get("pdf_name", "")
    del catalog["papers"][key]
    save_catalog(project_name, catalog)

    # 移 PDF 到回收站
    if pdf_name:
        src = _pdf_dir(project_name) / pdf_name
        if src.is_file():
            recycle_pdf_dir = _RECYCLE_DIR / re.sub(r"[^\w\s\-]", "", project_name)[:80]
            recycle_pdf_dir.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(recycle_pdf_dir / pdf_name))
            except OSError:
                pass

    return True


def clean_recycle(max_age_days: int = _RECYCLE_RETENTION_DAYS) -> int:
    """清理超过 max_age_days 天的回收站项目。

    Returns:
        清理的项目数
    """
    if not _RECYCLE_DIR.is_dir():
        return 0

    cutoff = time.time() - max_age_days * 86400
    cleaned = 0

    for item in list(_RECYCLE_DIR.iterdir()):
        try:
            mtime = item.stat().st_mtime
            if mtime < cutoff:
                if item.is_dir():
                    shutil.rmtree(str(item), ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                cleaned += 1
        except OSError:
            pass

    return cleaned
