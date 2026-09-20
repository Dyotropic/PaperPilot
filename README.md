# PaperPilot

面向课题攻关的可解释智能文献工作流系统。

## 项目背景

学生在面对一个全新的研究课题时，从课题分析、文献检索、筛选排序、阅读消化到整理归档的整个流程耗时耗力，且需要频繁切换于不同平台（学术搜索引擎、PDF 阅读器、笔记工具、文献管理软件）之间。PaperPilot 将这一完整工作流整合到单一桌面应用中，让科研人员专注于研究本身。

## 核心功能

- **智能检索**：AI 自动提取课题核心技术术语，支持 arXiv + OpenAlex + Europe PMC 多源检索（当前按源顺序执行；OpenAlex 支持配置 API Key），Cross-Encoder 语义模型精排
- **多模型 AI 服务**：统一 LLM 抽象层，支持 DeepSeek / OpenAI / Anthropic(Claude) / GLM / Kimi / 通义千问 / Ollama(本地) 等多服务商，设置页一键切换 Provider、Key 与模型；内置模型名是配置候选，实际可用性以服务商账户和连接测试为准
- **AI 精读**：基于 RLM 三层阅读策略，对论文全文进行结构化分析（核心贡献 / 研究方法 / 关键证据 / 创新亮点 / 局限不足 / 三维评分）
- **AI 对话助手（StudyCopilot）**：课题上下文感知的学术对话助手，支持 Markdown 富文本渲染、自动检测论文引用、多篇对比分析、Agent 主动执行操作，对话历史自动持久化
- **文献管理**：课题/论文 CRUD、阅读状态追踪、本地 PDF 导入（自动提取标题/作者/摘要）、回收站、BibTeX/CSV 导出；检索页与文献页勾选支持 Shift+点击范围选择（勾选两篇之间所有论文）
- **知识图谱**：按课题生成可交互文献关系图谱（独立窗口），提供引用关系、关键词共现、时间线三种视图，支持节点拖拽/缩放、点击查看详情与直接打开 PDF；引用数据离线优先（检索时自动缓存，必要时按 DOI 批量补查）
- **PDF 阅读器**：内置 PDF.js，独立窗口渲染，支持文字选择、页码导航、夜间模式

## 快速开始

**环境要求**：Windows 10+，Python 3.10+（本轮本机回归环境为 Python 3.13.5；其他版本的依赖兼容性需单独验证）

```bash
git clone https://github.com/Dyotropic/PaperPilot.git
cd PaperPilot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml   # 编辑所选 LLM 的 Provider、模型与 Key（Ollama 可免 Key）
python app.py
```

**主要依赖**：Flet、SQLAlchemy、sentence-transformers、KeyBERT、arxiv、PyMuPDF、pywebview、jieba、PyYAML、openai、anthropic

## 当前范围与限制

截至 2026-09-19，页面拆分、多供应商 LLM、三数据源、文献库、精读/对话和知识图谱已有实现。当前排序不使用 FAISS；requirements.txt 的 faiss-cpu 为历史依赖，本次仅注明而未移除。

定时推送、Zotero 导入、独立 AI 写作、多用户协作仍未实现。离线时可处理本地文献，联网检索和云端 AI 不可保证可用；部分本地能力及阅读引擎需提前缓存。配置保存为本地 YAML，未实现 keyring/加密存储。

2026-09-19 本机回归共通过 348 项（六组离线 256、数据库路径 4、端到端 64、扩展真实服务 10、边界 14），另有 5 项 unittest 与 8 项 UI 检查通过。PDF/图谱窗口最小化恢复连续 24 次未复现此前的一次整窗空白，但根因尚未确认；这些结果只代表该次本机验证，不等同于其他环境的持续集成保证。

详细使用指南见 [USER_GUIDE.md](USER_GUIDE.md) 或项目文档。
