"""PaperPilot 数据库模型 —— 两人共用的宪法文件。

所有表定义集中在此，任何字段变更必须先发 PR 合并。
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Text, DateTime, ForeignKey,
    create_engine
)
from sqlalchemy.orm import declarative_base, relationship, Session

Base = declarative_base()


class Project(Base):
    """课题"""
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), unique=True, nullable=False, comment="课题名称")
    description = Column(Text, nullable=False, comment="课题描述，用于向量检索")
    push_interval_days = Column(Integer, default=7, comment="推送周期(天)")
    created_at = Column(DateTime, default=datetime.now)

    papers = relationship("ProjectPaper", back_populates="project", cascade="all, delete-orphan")
    keywords = relationship("Keyword", back_populates="project", cascade="all, delete-orphan")


class Paper(Base):
    """论文"""
    __tablename__ = "papers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False, comment="标题")
    authors = Column(String(500), comment="作者，逗号分隔")
    abstract = Column(Text, comment="摘要")
    year = Column(Integer, nullable=True, comment="发表年份")
    source = Column(String(50), nullable=False, comment="来源: arxiv / openalex / europepmc / local_pdf")
    url = Column(String(500), nullable=True, comment="论文链接")
    doi = Column(String(200), nullable=True, comment="DOI")
    embedding_id = Column(Integer, nullable=True, comment="FAISS索引中的位置，-1表示未向量化")
    pdf_path = Column(String(500), nullable=True, comment="本地 PDF 文件路径")
    created_at = Column(DateTime, default=datetime.now)

    projects = relationship("ProjectPaper", back_populates="paper", cascade="all, delete-orphan")


class ProjectPaper(Base):
    """课题-论文关联（中间表，存储该课题下的打分和状态）"""
    __tablename__ = "project_papers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    paper_id = Column(Integer, ForeignKey("papers.id"), nullable=False)

    # 五维打分（Phase 1 仅用 similarity，其余为 Phase 2 预留）
    score_similarity = Column(Float, default=0.0, comment="向量相似度得分")
    score_keywords = Column(Float, default=0.0, comment="关键词共现得分")
    score_method = Column(Float, default=0.0, comment="方法匹配得分")
    score_conclusion = Column(Float, default=0.0, comment="结论价值得分")
    score_recency = Column(Float, default=0.0, comment="时效性得分")
    total_score = Column(Float, default=0.0, comment="加权总分")

    # AI 精排分（Phase 2）
    ai_score = Column(Float, nullable=True, comment="AI 精排分数 0-100")
    ai_reason = Column(Text, nullable=True, comment="AI 打分理由 JSON")

    # 阅读状态
    status = Column(
        String(20), default="unread",
        comment="unread / skimmed / deep_read"
    )
    # AI 笔记（Phase 2 填充）
    ai_notes = Column(Text, nullable=True, comment="AI 生成的结构化笔记")
    user_notes = Column(Text, nullable=True, comment="用户批注")

    added_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="papers")
    paper = relationship("Paper", back_populates="projects")


class Feedback(Base):
    """用户行为反馈（Phase 2 自适应权重用）"""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_paper_id = Column(Integer, ForeignKey("project_papers.id"), nullable=False)
    action_type = Column(String(20), nullable=False, comment="star / skip / deep_read")
    timestamp = Column(DateTime, default=datetime.now)


class Keyword(Base):
    """课题关键词"""
    __tablename__ = "keywords"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    keyword = Column(String(100), nullable=False)
    source = Column(String(20), default="auto", comment="auto(KeyBERT) / manual(用户手动)")

    project = relationship("Project", back_populates="keywords")


def init_db(path: str = "paperpilot.db") -> Session:
    """初始化数据库，创建所有表，返回 Session factory 可用的 session。

    调用方式:
        from paperpilot.models import init_db
        session = init_db("paperpilot.db")
    """
    engine = create_engine(f"sqlite:///{path}", echo=False)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()
