"""文献库 CRUD 接口。

提供课题管理、论文收藏、阅读状态追踪功能，复用 models.py 现有五张表。
"""

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from paperpilot.models import Base, Project, Paper, ProjectPaper, Feedback

if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent.parent
_DB_PATH = str(_BASE_DIR / "paperpilot.db")
_engine = None
_SessionLocal = None


def _migrate_schema(engine):
    """检测并添加缺失的列，向前兼容旧数据库。"""
    import sqlite3
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        cursor = conn.cursor()

        # 获取已有列
        existing = {r[1] for r in cursor.execute("PRAGMA table_info(papers)")}
        pp_existing = {r[1] for r in cursor.execute("PRAGMA table_info(project_papers)")}

        # 需要在 base 模型中声明但可能缺失的列
        needed = {
            "pdf_path": "TEXT",
        }

        for col_name, col_type in needed.items():
            if col_name not in existing:
                cursor.execute(f"ALTER TABLE papers ADD COLUMN {col_name} {col_type}")
                print(f"[Library] 数据库迁移: papers 表新增列 {col_name}")

        pp_needed = {
            "ai_score": "FLOAT",
            "ai_reason": "TEXT",
        }

        for col_name, col_type in pp_needed.items():
            if col_name not in pp_existing:
                cursor.execute(f"ALTER TABLE project_papers ADD COLUMN {col_name} {col_type}")
                print(f"[Library] 数据库迁移: project_papers 表新增列 {col_name}")

        conn.commit()
    finally:
        conn.close()


def _get_session() -> Session:
    """获取数据库会话（单引擎，复用 session factory）。"""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(f"sqlite:///{_DB_PATH}", echo=False)
        Base.metadata.create_all(_engine)
        _migrate_schema(_engine)
        _SessionLocal = sessionmaker(bind=_engine)
    return _SessionLocal()


# ── 课题管理 ──

def create_project(name: str, description: str, push_interval_days: int = 7) -> Project:
    """创建新课题。

    Raises:
        ValueError: 课题名称已存在
    """
    session = _get_session()
    try:
        existing = session.query(Project).filter(Project.name == name).first()
        if existing:
            raise ValueError(f"课题「{name}」已存在")

        project = Project(
            name=name,
            description=description,
            push_interval_days=push_interval_days,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        return project
    finally:
        session.close()


def get_all_projects() -> list[Project]:
    """获取所有课题列表，按创建时间倒序。"""
    session = _get_session()
    try:
        return session.query(Project).order_by(Project.created_at.desc()).all()
    finally:
        session.close()


def get_project(project_id: int) -> Project | None:
    """按 ID 获取单个课题。"""
    session = _get_session()
    try:
        return session.query(Project).filter(Project.id == project_id).first()
    finally:
        session.close()


def update_project(project_id: int, name: str | None = None,
                   description: str | None = None,
                   push_interval_days: int | None = None) -> bool:
    """更新课题信息（名称/描述/推送周期）。"""
    session = _get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if not project:
            return False
        if name is not None and name.strip():
            project.name = name.strip()
        if description is not None:
            project.description = description.strip()
        if push_interval_days is not None:
            project.push_interval_days = push_interval_days
        session.commit()
        return True
    finally:
        session.close()


def delete_project(project_id: int) -> bool:
    """删除课题及其关联数据（级联删除 papers/keywords/feedback）。"""
    session = _get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if not project:
            return False
        session.delete(project)
        session.commit()
        return True
    finally:
        session.close()


# ── 论文管理 ──

def _patch_existing_paper(existing: Paper, paper_dict: dict) -> None:
    """用新数据补填已有论文的缺失字段（不覆盖已有内容）。"""
    new_abstract = (paper_dict.get("abstract") or "").strip()
    old_abstract = (existing.abstract or "").strip()
    if new_abstract and len(new_abstract) > len(old_abstract):
        existing.abstract = new_abstract
    new_authors = (paper_dict.get("authors") or "").strip()
    old_authors = (existing.authors or "").strip()
    if new_authors and len(new_authors) > len(old_authors):
        existing.authors = new_authors
    new_source = paper_dict.get("source") or ""
    if new_source and not (existing.source or "").strip():
        existing.source = new_source
    new_url = paper_dict.get("url") or ""
    if new_url and not (existing.url or "").strip():
        existing.url = new_url


def _find_or_create_paper(session: Session, paper_dict: dict) -> tuple[Paper, bool]:
    """在 session 内查找或创建 Paper 记录。

    去重策略：DOI 优先 → 标题 + 年份。
    如果找到已有记录且 paper_dict 有 pdf_path，自动补填 pdf_path。

    Returns:
        (Paper, pdf_updated) — pdf_updated 表示是否对已有记录补填了 pdf_path
    """
    pdf_path = paper_dict.get("pdf_path")

    # 1. DOI 匹配
    doi = paper_dict.get("doi")
    if doi:
        existing = session.query(Paper).filter(Paper.doi == doi).first()
        if existing:
            _patch_existing_paper(existing, paper_dict)
            if pdf_path and os.path.isfile(pdf_path):
                old_valid = existing.pdf_path and os.path.isfile(existing.pdf_path)
                if not old_valid:
                    existing.pdf_path = pdf_path
                    return existing, True
            return existing, False

    # 2. 标题 + 年份匹配
    title = (paper_dict.get("title") or "").strip()
    year = paper_dict.get("year")
    if title:
        q = session.query(Paper).filter(Paper.title == title)
        if year is not None:
            q = q.filter(Paper.year == year)
        existing = q.first()
        # 年份不匹配时退回到纯标题匹配（PDF 年份提取可能不准）
        if not existing and year is not None:
            existing = session.query(Paper).filter(Paper.title == title).first()
        if existing:
            _patch_existing_paper(existing, paper_dict)
            if pdf_path and os.path.isfile(pdf_path):
                old_valid = existing.pdf_path and os.path.isfile(existing.pdf_path)
                if not old_valid:
                    existing.pdf_path = pdf_path
                    return existing, True
            return existing, False

    # 3. 新建
    paper = Paper(
        title=title,
        authors=(paper_dict.get("authors") or "")[:500],
        abstract=(paper_dict.get("abstract") or ""),
        year=year,
        source=paper_dict.get("source", "unknown"),
        url=paper_dict.get("url"),
        doi=doi,
        pdf_path=paper_dict.get("pdf_path"),
    )
    session.add(paper)
    session.flush()
    return paper, False


def save_papers_to_project(
    project_id: int,
    papers: list[dict],
    scores: list[tuple[dict, float]] | None = None,
) -> tuple[int, int]:
    """将检索结果保存到课题。

    已存在于课题中的论文不重复添加（通过 ProjectPaper 去重），
    但会补填缺失的 pdf_path。

    Args:
        project_id: 目标课题 ID
        papers: paper dict 列表
        scores: rank_papers 返回的 [(paper, score), ...]，可选

    Returns:
        (added, pdf_updated) — added 为新关联数，pdf_updated 为补填 PDF 路径数
    """
    import json as _json

    # 构建 paper → score 映射
    score_map: dict[str, float] = {}
    if scores:
        for paper_dict, score in scores:
            key = (paper_dict.get("doi") or paper_dict.get("title", "")).strip().lower()
            if key:
                score_map[key] = float(score)

    session = _get_session()
    added = 0
    pdf_updated = 0
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if not project:
            return 0, 0

        for paper_dict in papers:
            paper, was_pdf_updated = _find_or_create_paper(session, paper_dict)
            paper_id = paper.id

            if was_pdf_updated:
                pdf_updated += 1

            # 检查是否已关联到该课题
            existing = (
                session.query(ProjectPaper)
                .filter(
                    ProjectPaper.project_id == project_id,
                    ProjectPaper.paper_id == paper_id,
                )
                .first()
            )
            # 获取分数
            key = (paper_dict.get("doi") or paper_dict.get("title", "")).strip().lower()
            total_score = score_map.get(key, 0.0)
            ai_score = paper_dict.get("ai_score")
            ai_reason = paper_dict.get("ai_reason")

            if existing:
                # 已有关联：补充 AI 分数（如果 paper 带了且 ProjectPaper 没有）
                updated = False
                if ai_score is not None and existing.ai_score is None:
                    existing.ai_score = int(ai_score)
                    updated = True
                if ai_reason and not existing.ai_reason:
                    existing.ai_reason = _json.dumps(ai_reason, ensure_ascii=False) if isinstance(ai_reason, dict) else str(ai_reason)
                    updated = True
                if updated:
                    pdf_updated += 1  # 复用计数
                continue

            pp = ProjectPaper(
                project_id=project_id,
                paper_id=paper_id,
                total_score=total_score,
                score_similarity=total_score,
                status="unread",
                ai_score=int(ai_score) if ai_score is not None else None,
                ai_reason=_json.dumps(ai_reason, ensure_ascii=False) if isinstance(ai_reason, dict) else (str(ai_reason) if ai_reason else None),
            )
            session.add(pp)
            added += 1

        session.commit()
    finally:
        session.close()

    return added, pdf_updated


def get_project_papers(
    project_id: int,
    status_filter: str | None = None,
) -> list[dict]:
    """获取课题下的论文列表。

    Args:
        project_id: 课题 ID
        status_filter: 可选的状态筛选 ("unread" / "skimmed" / "deep_read")

    Returns:
        dict 列表，包含论文字段 + project_paper 关联字段
    """
    session = _get_session()
    try:
        q = (
            session.query(ProjectPaper, Paper)
            .join(Paper, ProjectPaper.paper_id == Paper.id)
            .filter(ProjectPaper.project_id == project_id)
        )
        if status_filter:
            q = q.filter(ProjectPaper.status == status_filter)
        q = q.order_by(ProjectPaper.total_score.desc())

        results = []
        for pp, paper in q.all():
            results.append({
                "id": paper.id,
                "project_paper_id": pp.id,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "year": paper.year,
                "source": paper.source,
                "url": paper.url,
                "doi": paper.doi,
                "pdf_path": paper.pdf_path,
                "total_score": pp.total_score,
                "ai_score": pp.ai_score,
                "ai_reason": pp.ai_reason,
                "status": pp.status,
                "ai_notes": pp.ai_notes,
                "user_notes": pp.user_notes,
                "added_at": str(pp.added_at) if pp.added_at else None,
            })
        return results
    finally:
        session.close()


def set_paper_pdf_path(doi: str, pdf_path: str) -> bool:
    """根据 DOI 设置论文的本地 PDF 路径。

    Args:
        doi: 论文 DOI
        pdf_path: PDF 文件绝对路径

    Returns:
        是否成功更新
    """
    session = _get_session()
    try:
        paper = session.query(Paper).filter(Paper.doi == doi).first()
        if not paper:
            return False
        paper.pdf_path = pdf_path
        session.commit()
        return True
    finally:
        session.close()


def set_paper_pdf_path_by_title(title: str, pdf_path: str,
                                year: int | None = None) -> bool:
    """根据标题（+ 年份）设置论文 PDF 路径，DOI 匹配失败时的回退。

    Args:
        title: 论文标题
        pdf_path: PDF 文件绝对路径
        year: 可选，论文年份（提高匹配精度）

    Returns:
        是否成功更新
    """
    session = _get_session()
    try:
        q = session.query(Paper).filter(Paper.title == title)
        if year is not None:
            q = q.filter(Paper.year == year)
        paper = q.first()
        if not paper:
            return False
        paper.pdf_path = pdf_path
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def update_paper_status(project_paper_id: int, status: str) -> bool:
    """更新论文阅读状态。

    Args:
        project_paper_id: ProjectPaper 的 ID（非 Paper ID）
        status: "unread" / "skimmed" / "deep_read"

    Returns:
        True 表示更新成功，False 表示记录不存在
    """
    valid_statuses = {"unread", "skimmed", "deep_read"}
    if status not in valid_statuses:
        raise ValueError(f"无效状态: {status}，有效值: {valid_statuses}")

    session = _get_session()
    try:
        pp = session.query(ProjectPaper).filter(ProjectPaper.id == project_paper_id).first()
        if not pp:
            return False
        pp.status = status
        session.commit()
        return True
    finally:
        session.close()


def save_deep_read_notes(project_paper_id: int, notes: str, status: str = "deep_read") -> bool:
    """保存 AI 精读笔记到论文记录，同时更新阅读状态。

    Args:
        project_paper_id: ProjectPaper 的 ID
        notes: AI 精读结果（JSON 字符串或纯文本）
        status: 同步更新的阅读状态，默认 "deep_read"

    Returns:
        True 表示更新成功
    """
    session = _get_session()
    try:
        pp = session.query(ProjectPaper).filter(ProjectPaper.id == project_paper_id).first()
        if not pp:
            return False
        pp.ai_notes = notes
        pp.status = status
        session.commit()
        return True
    finally:
        session.close()


def update_paper_scores(project_id: int, scored: list[tuple[dict, float]]) -> int:
    """用 rank_papers 结果批量更新 ProjectPaper 分数。

    通过 DOI 或标题匹配论文，更新 total_score 和 score_similarity。

    Args:
        project_id: 课题 ID
        scored: [(paper_dict, score), ...] — rank_papers 输出

    Returns:
        更新的 ProjectPaper 记录数
    """
    session = _get_session()
    updated = 0
    try:
        for paper_dict, score in scored:
            doi = paper_dict.get("doi")
            title = (paper_dict.get("title") or "").strip()

            if not doi and not title:
                continue

            # 匹配 ProjectPaper
            q = (
                session.query(ProjectPaper)
                .join(Paper, ProjectPaper.paper_id == Paper.id)
                .filter(ProjectPaper.project_id == project_id)
            )
            if doi:
                q = q.filter(Paper.doi == doi)
            elif title:
                q = q.filter(Paper.title == title)

            pp = q.first()
            if pp:
                pp.total_score = float(score)
                pp.score_similarity = float(score)
                updated += 1

        session.commit()
    finally:
        session.close()

    return updated


def update_paper_ai_scores(project_id: int, ai_results: list[dict],
                         paper_dicts: list[dict] | None = None) -> int:
    """将 AI 精排结果写入 ProjectPaper。

    优先通过 paper_dicts[index] 中的 project_paper_id 匹配；
    否则按 ProjectPaper 在数据库中的顺序匹配（仅当 paper_dicts 顺序
    与数据库顺序一致时有效）。

    Args:
        project_id: 课题 ID
        ai_results: score_papers 返回的 [{index, ai_score, tier, ai_reason}, ...]
        paper_dicts: 传入 score_papers 的论文列表（含 project_paper_id）

    Returns:
        更新的 ProjectPaper 记录数
    """
    import json
    session = _get_session()
    updated = 0
    try:
        for item in ai_results:
            idx = item.get("index", -1)
            pp_id = None
            if paper_dicts and 0 <= idx < len(paper_dicts):
                pp_id = paper_dicts[idx].get("project_paper_id")

            if pp_id is not None:
                pp = session.query(ProjectPaper).filter(ProjectPaper.id == pp_id).first()
            else:
                # 回退：按数据库顺序匹配
                pps = (
                    session.query(ProjectPaper)
                    .filter(ProjectPaper.project_id == project_id)
                    .order_by(ProjectPaper.id)
                    .all()
                )
                pp = pps[idx] if 0 <= idx < len(pps) else None

            if pp:
                pp.ai_score = float(item.get("ai_score", 0))
                reason = dict(item.get("ai_reason", {}))
                reason["tier"] = item.get("tier", "")
                pp.ai_reason = json.dumps(reason, ensure_ascii=False)
                updated += 1

        session.commit()
    finally:
        session.close()

    return updated


def remove_paper_from_project(project_paper_id: int) -> bool:
    """从课题中删除单篇论文（仅删 ProjectPaper 关联，不删 Paper）。

    Args:
        project_paper_id: ProjectPaper 的 ID

    Returns:
        True 表示删除成功，False 表示记录不存在
    """
    session = _get_session()
    try:
        pp = session.query(ProjectPaper).filter(ProjectPaper.id == project_paper_id).first()
        if not pp:
            return False
        # 清理关联的反馈记录
        session.query(Feedback).filter(
            Feedback.project_paper_id == project_paper_id
        ).delete()
        session.delete(pp)
        session.commit()
        return True
    finally:
        session.close()


def remove_papers_from_project(project_paper_ids: list[int]) -> int:
    """批量从课题中删除论文。

    Args:
        project_paper_ids: ProjectPaper ID 列表

    Returns:
        成功删除的条数
    """
    count = 0
    for pp_id in project_paper_ids:
        if remove_paper_from_project(pp_id):
            count += 1
    return count
