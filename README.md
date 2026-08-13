# PaperPilot

面向课题攻关的可解释智能文献工作流系统。

## 项目背景

学生在面对一个全新的研究课题时，从课题分析、文献检索、筛选排序、阅读消化到整理归档的整个流程耗时耗力，且需要频繁切换于不同平台（学术搜索引擎、PDF 阅读器、笔记工具、文献管理软件）之间。PaperPilot 将这一完整工作流整合到单一桌面应用中，让科研人员专注于研究本身。

## 核心功能

- **智能检索**：AI 自动提取课题核心技术术语，支持 arXiv + OpenAlex 双源并行检索，Cross-Encoder 语义模型精排
- **AI 精读**：基于 RLM 三层阅读策略，对论文全文进行结构化分析（核心贡献 / 研究方法 / 关键证据 / 创新亮点 / 局限不足 / 三维评分）
- **AI 对话助手（StudyCopilot）**：课题上下文感知的学术对话助手，支持 Markdown 富文本渲染、自动检测论文引用、多篇对比分析、Agent 主动执行操作，对话历史自动持久化
- **文献管理**：课题/论文 CRUD、阅读状态追踪、本地 PDF 导入（自动提取标题/作者/摘要）、回收站、BibTeX/CSV 导出
- **PDF 阅读器**：内置 PDF.js，独立窗口渲染，支持文字选择、页码导航、夜间模式

## 快速开始

**环境要求**：Windows 10+，Python 3.10 ~ 3.12

```bash
git clone https://github.com/Dyotropic/PaperPilot.git
cd PaperPilot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml   # 编辑填入 DeepSeek API Key
python app.py
```

**主要依赖**：Flet、SQLAlchemy、sentence-transformers、KeyBERT、arxiv、PyMuPDF、pywebview、jieba、PyYAML

详细使用指南见 [USER_GUIDE.md](USER_GUIDE.md) 或项目文档。
