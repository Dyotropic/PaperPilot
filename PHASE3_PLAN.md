# PaperPilot Phase 3 开发方案

> 文档版本：v1.0 | 日期：2026-07-04 | 状态：规划中
>
> 本文档面向两位开发者，覆盖 Phase 3 全部方向的功能设计、架构、可行性分析、技术选型及协作规范。

---

## 一、项目背景与现状回顾

### 1.1 已完成工作

| 阶段 | 核心产出 |
|------|----------|
| **Phase 1** | 课题关键词提取 → 中英文翻译 → arXiv/OpenAlex 双源检索 → Cross-Encoder 语义精排 → 拖拽三区关键词管理 |
| **Phase 2** | AI 精读（RLM 三层策略）→ AI 精排打分 → StudyCopilot 对话助手 → 文献库 CRUD → 本地 PDF 导入 → BibTeX/CSV 导出 → 对话持久化与压缩 |

### 1.2 技术栈现状

```
UI 层       : Flet 0.85 (Flutter/Python 跨平台桌面)
AI 服务     : DeepSeek API (deepseek-v4-flash), 本地 Cross-Encoder (mxbai-rerank-base-v2)
数据存储    : SQLite + SQLAlchemy ORM, 本地文件系统 (repository/)
文献源      : arXiv API + OpenAlex API
PDF 处理    : PyMuPDF + pywebview + PDF.js (独立窗口)
关键词      : KeyBERT + jieba + DeepSeek fallback
对话管理    : 本地 JSON 持久化 + 滑动窗口压缩
```

### 1.3 PPT "接下来要做的" — Phase 3 五大方向

根据答辩 PPT 第 9 页（ROADMAP），下一阶段重点：

1. **智能推送通知** — 定时检索 + 邮件/微信推送
2. **扩展数据源** — CNKI/知网、Semantic Scholar、Zotero 导入
3. **多模型 / 自定义 API Key 支持** — OpenAI/Claude/GLM 等可切换
4. **可视化知识图谱 + AI 辅助写作** — 引用关系可视化 + 综述草稿生成
5. **协作模式** — 多用户共享文献库 + 批注协同

---

## 二、功能详细规划

### 2.1 功能一：智能推送通知

#### 目标
系统后台常驻，按用户设定周期自动检索新论文，将高相关性论文通过通知渠道推送给用户。

#### 子功能拆解

| 子功能 | 说明 |
|--------|------|
| 后台定时任务 | Windows 系统托盘常驻进程，定时触发检索流水线 |
| 推送阈值配置 | 用户在设置页配置：推送周期（天）、AI 分数阈值、最大推送篇数 |
| 桌面通知 | Windows 原生 Toast 通知，点击跳转到 PaperPilot |
| 邮件推送 | SMTP 发送 HTML 格式论文摘要邮件 |
| 微信推送（可选）| 通过 Server酱/WxPusher 等第三方 Webhook 推送到微信 |
| 推送记录 | 记录已推送论文，避免重复推送 |

#### 新增 DB 字段

```python
# Project 表新增
last_push_at = Column(DateTime, nullable=True, comment="最近一次推送时间")
push_score_threshold = Column(Float, default=60.0, comment="推送分数阈值")
push_max_papers = Column(Integer, default=5, comment="每次最大推送篇数")
push_channels = Column(String(200), default="desktop", comment="推送渠道: desktop/email/wechat")

# PushRecord 新表
class PushRecord(Base):
    __tablename__ = "push_records"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    paper_id = Column(Integer, ForeignKey("papers.id"))
    pushed_at = Column(DateTime, default=datetime.now)
    channel = Column(String(50))
    score = Column(Float)
```

---

### 2.2 功能二：扩展数据源

#### 目标
在现有 arXiv + OpenAlex 基础上，接入中文学术源和更多英文源，同时支持 Zotero 文献库导入。

#### 子功能拆解

| 数据源 | 接入方式 | 说明 |
|--------|----------|------|
| **Semantic Scholar** | 免费 REST API，无需 Key | 补充 arXiv 未收录论文，含引用图 |
| **CNKI/知网** | 网页爬虫（无官方 API） | 需模拟浏览器，受反爬限制，优先级低 |
| **Zotero 本地库** | 读取 Zotero SQLite DB | 免安装，直接解析 `zotero.sqlite` |
| **CrossRef** | 免费 REST API | 补充 DOI → 元数据解析，增强现有管道 |

> **优先级**：Semantic Scholar > Zotero 导入 > CrossRef 增强 > CNKI（技术难度高，放入备选）

#### 数据源统一抽象层

```python
# paperpilot/sources/base.py
class PaperSource(Protocol):
    name: str
    def fetch(self, keywords: list[str], max_results: int, ...) -> list[dict]: ...
    def is_available(self) -> bool: ...

# 已有：ArxivSource, OpenAlexSource
# 新增：SemanticScholarSource, ZoteroSource, CrossRefSource
```

---

### 2.3 功能三：多模型 / 自定义 API Key 支持

#### 目标
支持用户在设置页切换 AI 后端（OpenAI / Claude / GLM / 本地 Ollama），不再绑定单一 DeepSeek API。

#### 子功能拆解

| 子功能 | 说明 |
|--------|------|
| 统一 LLM 接口层 | 所有 AI 调用通过 `LLMClient` 抽象，屏蔽具体 provider 差异 |
| Provider 支持列表 | DeepSeek（现有）、OpenAI/ChatGPT、Claude（Anthropic）、智谱 GLM、Ollama（本地） |
| 设置页 UI | 下拉选择 Provider → 输入 API Key → 测试连通性 → 保存 |
| OpenAI 格式兼容 | 大量第三方模型（如 Qwen、Moonshot）提供 OpenAI 兼容接口，一套代码复用 |
| Key 安全存储 | config.yaml 本地加密（或系统 keyring），不明文写日志 |

#### LLM 抽象层设计

```python
# paperpilot/llm_client.py
class LLMClient:
    def chat(self, messages: list[dict], thinking: bool = False) -> str: ...
    def is_available(self) -> bool: ...

class OpenAICompatClient(LLMClient):
    """兼容 OpenAI 格式: DeepSeek / ChatGPT / Qwen / Moonshot / GLM"""
    def __init__(self, base_url: str, api_key: str, model: str): ...

class AnthropicClient(LLMClient):
    """Claude API (Anthropic SDK)"""
    def __init__(self, api_key: str, model: str): ...

class OllamaClient(LLMClient):
    """本地 Ollama，无需 API Key"""
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2"): ...
```

#### config.yaml 新增字段

```yaml
llm:
  provider: deepseek          # deepseek / openai / claude / glm / ollama
  api_key: sk-xxx
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com/v1  # 可覆盖，兼容第三方
  timeout: 30
```

---

### 2.4 功能四：可视化知识图谱 + AI 辅助写作

#### 目标
将文献库中的论文关系以图谱形式可视化展示；基于课题和文献库自动生成研究综述草稿。

#### 子功能拆解

**知识图谱**

| 子功能 | 说明 |
|--------|------|
| 引用关系图 | 以 DOI/标题为节点，引用关系为边，展示论文间的引用网络 |
| 关键词共现图 | 论文之间共有关键词越多，边越粗 |
| 时间线视图 | 按年份排列论文，直观展示研究演进脉络 |
| 交互操作 | 点击节点展示论文详情，双击打开精读；拖拽布局 |
| 渲染方案 | 内嵌 pywebview + D3.js / Vis.js，或 ECharts Graph |

**AI 辅助写作**

| 子功能 | 说明 |
|--------|------|
| 研究综述生成 | 基于文献库 + 课题描述，生成结构化综述草稿（背景/现状/方法/趋势） |
| 大纲生成 | 根据课题方向生成论文/报告大纲框架 |
| 段落扩写 | 用户输入关键句 + 引用论文，AI 扩写为学术段落 |
| 引文插入 | 生成文字时自动标注 [作者, 年份] 引用格式 |
| 导出 | 生成内容支持导出为 .md / .docx |

---

### 2.5 功能五：协作模式

#### 目标
支持师生/团队在同一台或不同机器上共享文献库、协同批注。

#### 子功能拆解

| 子功能 | 说明 |
|--------|------|
| 本地多用户 | 同一台机器支持多个用户身份，独立 config 和文献库 |
| 局域网共享 | 一台机器开启 HTTP 服务端，同局域网其他机器以只读/读写方式接入 |
| 协同批注 | 对同一篇论文的 `user_notes` 支持多用户追加（带时间戳和用户名） |
| 权限控制 | 所有者（读写删）/ 协作者（读写）/ 访客（只读） |
| 进度可视化 | 课题内各论文的阅读进度（未读/浏览/精读）汇总展示 |

> **实现策略**：Phase 3 仅实现本地多用户 + 局域网只读共享（低复杂度），云端同步作为 Phase 4 方向。

---

## 三、系统架构图

### 3.1 Phase 3 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          PaperPilot 桌面应用                          │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                         Flet UI 层                            │    │
│  │  检索页  │  文献库  │  知识图谱(NEW)  │  写作助手(NEW)  │  设置  │    │
│  └────────────────────────┬─────────────────────────────────────┘    │
│                           │ 调用                                       │
│  ┌────────────────────────▼─────────────────────────────────────┐    │
│  │                       业务服务层                               │    │
│  │                                                                │    │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐ │    │
│  │  │ 检索管道     │  │  AI 服务层   │  │    知识图谱服务(NEW)  │ │    │
│  │  │ (现有)      │  │  (现有+扩展) │  │    graph_service.py  │ │    │
│  │  └─────────────┘  └──────┬───────┘  └──────────────────────┘ │    │
│  │                          │                                     │    │
│  │  ┌───────────────────────▼──────────────────────────────────┐ │    │
│  │  │                  LLM 抽象客户端(NEW)                      │ │    │
│  │  │   OpenAICompat  │  Anthropic  │  Ollama  │  DeepSeek     │ │    │
│  │  └───────────────────────────────────────────────────────────┘ │    │
│  │                                                                │    │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐ │    │
│  │  │ 数据源层     │  │  推送服务    │  │  写作服务(NEW)        │ │    │
│  │  │(现有+扩展)  │  │  (NEW)      │  │  writing_service.py  │ │    │
│  │  │ arXiv       │  │  定时器      │  └──────────────────────┘ │    │
│  │  │ OpenAlex    │  │  SMTP邮件   │                             │    │
│  │  │ SemanticSch │  │  桌面通知   │  ┌──────────────────────┐ │    │
│  │  │ Zotero(NEW) │  │  Webhook    │  │  协作服务(NEW)        │ │    │
│  │  │ CrossRef    │  └──────────────┘  │  collab_service.py  │ │    │
│  │  └─────────────┘                   └──────────────────────┘ │    │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                         数据持久层                             │    │
│  │   SQLite (paperpilot.db)  │  本地文件 (repository/)           │    │
│  │   JSON 对话记录            │  PDF 缓存 (cache/)               │    │
│  └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘

外部依赖:
  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐
  │  LLM APIs    │  │  学术数据源  │  │  通知渠道                  │
  │  DeepSeek    │  │  arXiv       │  │  Windows Toast            │
  │  OpenAI      │  │  OpenAlex    │  │  SMTP 邮件               │
  │  Claude      │  │  SemanticSch │  │  WxPusher/Server酱        │
  │  GLM/Qwen    │  │  CNKI (可选) │  └──────────────────────────┘
  │  Ollama(本地)│  │  Zotero      │
  └──────────────┘  └──────────────┘
```

### 3.2 文件结构变化（新增/修改）

```
PaperPilot/
├── app.py                         # 新增知识图谱页、写作助手页、多模型设置 UI
├── paperpilot/
│   ├── llm_client.py              # [NEW] LLM 统一抽象层
│   ├── writing_service.py         # [NEW] AI 辅助写作服务
│   ├── graph_service.py           # [NEW] 知识图谱构建与数据接口
│   ├── push_service.py            # [NEW] 定时推送服务
│   ├── collab_service.py          # [NEW] 协作服务（局域网共享）
│   ├── sources/
│   │   ├── __init__.py            # [NEW] 数据源注册
│   │   ├── base.py                # [NEW] PaperSource Protocol
│   │   ├── arxiv_source.py        # [MOVE] 从 fetcher.py 迁移
│   │   ├── openalex_source.py     # [MOVE]
│   │   ├── semantic_scholar.py    # [NEW]
│   │   ├── zotero_import.py       # [NEW]
│   │   └── crossref_source.py     # [NEW]
│   ├── models.py                  # [MODIFY] 新增 PushRecord 表及相关字段
│   ├── ai_service.py              # [MODIFY] 引用 LLMClient 替换直接调用
│   └── config.py                  # [MODIFY] 支持 llm.provider 多字段
├── ui/
│   ├── graph_view.html            # [NEW] 知识图谱 D3.js/Vis.js 前端
│   └── writing_view.html          # [NEW] 写作助手前端（可选 webview 方案）
└── requirements.txt               # [MODIFY] 新增依赖
```

---

## 四、可行性研究

### 4.1 技术可行性

| 方向 | 可行性 | 说明 |
|------|--------|------|
| 智能推送通知 | **高** | Windows 系统托盘 + Toast 通知已有成熟库（pystray + win10toast）；SMTP 邮件为标准协议 |
| Semantic Scholar | **高** | 免费开放 API，无需 Key，文档完整，响应稳定 |
| Zotero 导入 | **高** | Zotero 使用 SQLite，直接读取即可，无需反爬 |
| 多模型支持 | **高** | DeepSeek/OpenAI/Claude 均提供 REST API；OpenAI 兼容格式统一了大多数第三方模型 |
| 知识图谱可视化 | **中** | 需 pywebview 内嵌 HTML/JS 方案，或迁移到 Flet 的 WebView 控件；引用关系数据依赖 Semantic Scholar API |
| AI 辅助写作 | **高** | 复用现有 LLM 调用能力，主要是 Prompt 工程和 UI 设计 |
| 协作（本地多用户） | **高** | SQLite WAL 模式支持多进程并发读写，用户切换只需隔离 config 路径 |
| 协作（局域网共享） | **中** | 需引入轻量 HTTP 服务器（FastAPI/Flask），对 Flet 桌面应用是新增复杂度 |
| CNKI 爬虫 | **低** | 有严格反爬措施，法律风险，**建议暂不实现** |

### 4.2 四周开发排期（两人并行）

> 项目剩余开发时间共 **4 周**。原 15.5 周工作量估算不再适用，Phase 3 调整为“核心底座优先、亮点功能形成可演示 MVP、协作能力控制范围”的集中开发方案。两位开发者按后端/数据能力与前端/UI 接入并行推进，每周完成一次集成验收。

| 周次 | 后端/数据侧重点 | 前端/UI 侧重点 | 周交付目标 |
|------|----------------|----------------|------------|
| **第 1 周** | 拆分 `app.py` 的关键页面；完成 `LLMClient` 抽象及 DeepSeek/OpenAI 兼容接入；准备配置与数据库迁移 | 完成多模型设置页、API Key/模型配置及连通性测试界面 | 工程结构可继续扩展，多模型底座跑通，现有 Phase 1/2 主流程回归通过 |
| **第 2 周** | 接入 Semantic Scholar；实现 Zotero 只读导入；完成推送记录与桌面通知最小闭环 | 完成数据源开关、Zotero 导入入口及推送周期/阈值配置 | 扩展数据源可用，桌面智能推送可演示；邮件/微信推送作为有余力时的增强项 |
| **第 3 周** | 完成知识图谱数据构建与 AI 写作服务 MVP | 完成知识图谱交互视图、综述/大纲生成与导出界面 | 可演示引用/关键词关系图，并能基于课题文献生成可编辑的综述或大纲 |
| **第 4 周** | 实现本地多用户与数据隔离；集中修复缺陷、补齐测试和发布准备 | 完成用户切换与阅读进度展示；开展全流程验收和演示优化 | 本地协作 MVP、回归测试、用户文档和最终演示版本完成 |

**范围控制原则：**
- 必须完成：多模型底座、Semantic Scholar、Zotero 导入、桌面智能推送、知识图谱 MVP、AI 写作 MVP、本地多用户。
- 视进度增强：邮件/微信推送、CrossRef 数据增强、知识图谱高级筛选与复杂交互。
- 暂不纳入四周硬性交付：CNKI 爬虫、局域网共享、云端同步和双向实时协同；其中局域网只读共享仅在前三周任务提前完成且安全测试充分时作为拓展成果。

> 四周阶段划分：
> - **Phase 3a（第 1—2 周）**：工程治理 + 多模型支持 + Semantic Scholar + Zotero + 桌面推送
> - **Phase 3b（第 3—4 周）**：知识图谱 MVP + AI 辅助写作 MVP + 本地多用户 + 集成验收

### 4.3 风险评估

| 风险 | 概率 | 影响 | 应对策略 |
|------|------|------|----------|
| Semantic Scholar API 限速（100 req/5min 免费额度） | 中 | 中 | 本地缓存 + 指数退避重试，超额时降级到已有源 |
| D3.js/Vis.js 与 pywebview 通信延迟高 | 中 | 中 | 先验证原型；备选方案改用 Flet Canvas 或 ECharts |
| OpenAI/Claude API 格式与 DeepSeek 有细微差异 | 中 | 低 | 抽象层做参数适配，单独编写集成测试 |
| Zotero 数据库结构随版本变化 | 低 | 中 | 只依赖 Zotero 稳定表（items / itemAttachments），做版本检测 |
| 协作局域网方案引入安全漏洞 | 低 | 高 | 仅局域网，不暴露公网；接入时做基本 Token 鉴权 |
| app.py 持续膨胀（当前 4600+ 行） | 高 | 中 | Phase 3 启动前先按页面拆分 app.py（见规范第七章） |

---

## 五、技术选型与实现方法

### 5.1 多模型 LLM 抽象层

**选型：openai Python SDK（>=1.0）+ anthropic SDK**

DeepSeek、Qwen、Moonshot、GLM 均兼容 OpenAI Chat Completions 格式，通过 `base_url` 复用同一客户端。Ollama 也提供 OpenAI 兼容接口。Claude 需单独使用 `anthropic` SDK。

```python
# paperpilot/llm_client.py
PROVIDERS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1",   "default_model": "deepseek-v4-flash"},
    "openai":   {"base_url": "https://api.openai.com/v1",    "default_model": "gpt-4o-mini"},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "default_model": "qwen-turbo"},
    "glm":      {"base_url": "https://open.bigmodel.cn/api/paas/v4", "default_model": "glm-4-flash"},
    "ollama":   {"base_url": "http://localhost:11434/v1",     "default_model": "qwen2.5:7b"},
}

class LLMClient:
    def chat(self, messages: list[dict], thinking: bool = False, timeout: int = 30) -> str: ...
```

**迁移方案**：`ai_service.py` 中所有 `_call_api()` 替换为 `self._llm.chat()`，DeepSeek 特有的 `"thinking": {"type": "disabled"}` 放入子类覆盖。

---

### 5.2 Semantic Scholar 数据源

免费 REST API，无需 Key，有限速（100 req/5min）。

```python
# paperpilot/sources/semantic_scholar.py
class SemanticScholarSource:
    BASE = "https://api.semanticscholar.org/graph/v1"
    FIELDS = "paperId,title,authors,year,abstract,citationCount,externalIds"

    def fetch(self, keywords: list[str], max_results: int = 50) -> list[dict]:
        # 指数退避：429 时等待后重试（最多 3 次）
        # 返回格式统一为项目内部 paper dict
        ...
```

节点标准化输出字段：`title / authors / year / abstract / cited_by_count / doi / url / source="semantic_scholar"`

---

### 5.3 Zotero 本地导入

Zotero 使用 SQLite，默认路径：`%APPDATA%\Zotero\Zotero\profiles\*\zotero.sqlite`。

```python
# paperpilot/sources/zotero_import.py
def import_from_zotero(db_path: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读，不锁定 Zotero
    # 查询 items + itemData（title/abstractNote/date/DOI）
    # 排除 attachment(1) 和 note(14) 类型
    ...
```

关键点：以 `?mode=ro` URI 方式只读打开，不影响 Zotero 运行中写入。

---

### 5.4 知识图谱可视化

**方案：pywebview 独立窗口 + Vis.js Network**

```
graph_service.py（Python 侧）
  ├── 构建节点：每篇论文一个节点，size ∝ cited_by_count，color 按来源区分
  ├── 构建边：引用关系（Semantic Scholar API）+ 关键词共现（≥2 个共同词时连边）
  └── 输出 JSON → pywebview js_api.inject_data(json)

ui/graph_view.html（前端）
  ├── Vis.js Network：物理布局，支持拖拽/缩放
  ├── 点击节点：右侧面板展示论文详情
  └── 筛选控件：年份 / 来源 / 关键词
```

备选方案（若 pywebview 通信延迟不可接受）：改用 Flet `WebView` 控件嵌入主窗口，或使用 ECharts Graph。

---

### 5.5 智能推送通知

```
apscheduler BackgroundScheduler（进程内，无独立进程）
  └── 每 N 天触发 → 复用现有检索管道 → AI 打分过滤 → 推送

推送渠道:
  desktop : plyer.notification.notify()（Windows/macOS/Linux 跨平台）
  email   : smtplib + email.mime（HTML 格式摘要邮件）
  wechat  : requests.post(wxpusher_webhook, json={"content": ...})
```

推送防重：`push_records` 表记录已推送的 `(project_id, paper_id)`，检索新结果时先过滤。

---

### 5.6 AI 辅助写作

```python
# paperpilot/writing_service.py
class WritingService:
    def generate_survey(self, topic_desc: str, papers: list[dict]) -> str:
        """生成综述草稿，含 [作者, 年份] 引文标注。"""

    def generate_outline(self, topic_desc: str) -> str:
        """生成多级大纲。"""

    def expand_paragraph(self, key_sentence: str, ref_papers: list[dict]) -> str:
        """段落扩写。"""

    def export_docx(self, content: str, output_path: str):
        """导出为 .docx，保留引文格式。"""
```

所有方法通过 `LLMClient` 调用，Prompt 在方法内部管理，UI 只传数据。

---

### 5.7 新增 Python 依赖

```
openai>=1.30.0       # LLM 统一客户端（DeepSeek/ChatGPT/Qwen/GLM/Ollama）
anthropic>=0.30.0    # Claude API
apscheduler>=3.10.0  # 定时推送任务
plyer>=2.1.0         # 跨平台桌面通知
python-docx>=1.1.0   # 写作功能导出 Word
# pystray            # 系统托盘（可选，按需添加）
# vis.js             # 通过本地静态文件加载，不需要 pip
```

---

## 六、可能遇到的问题与解决方案

### 6.1 LLM 抽象层兼容性问题

**问题**：不同 Provider 的 API 响应格式存在细微差异，例如 DeepSeek 的 `thinking` 参数、Claude 的 `max_tokens` 必填、Ollama 本地无需 API Key 等。

**解决方案**：
- 每个 Provider 实现独立的 `_normalize_request()` 和 `_normalize_response()` 方法，在抽象层内部适配
- 为每个 Provider 编写独立集成测试（`test_llm_providers.py`），mock 网络层，验证请求格式和响应解析
- 设置页面增加"测试连通性"按钮，用户配置后立即验证 API Key 是否可用

---

### 6.2 Semantic Scholar API 限速

**问题**：免费额度 100 req/5min，批量检索多个课题时容易触发 429。

**解决方案**：
- 本地缓存 API 响应（复用现有 `cache/api/` 目录，TTL 24h）
- 指数退避重试（1s → 2s → 4s，最多 3 次）
- 限流器：`threading.Semaphore` 控制并发数，确保同一时刻最多 2 个并发请求
- 申请免费 API Key（https://www.semanticscholar.org/product/api），配额提升至 1 req/s

---

### 6.3 知识图谱 pywebview 通信延迟

**问题**：Python ↔ JS 双向通信需通过 `js_api`，大图（>200 节点）序列化可能有明显卡顿。

**解决方案**：
- 前端分页加载：首次只渲染当前课题的 50 个核心节点，用户展开时增量加载
- 数据在 Python 侧预处理完成后一次性注入，避免多次小批量通信
- 若延迟仍不可接受，降级为 Flet Canvas 自绘（无 JS 通信开销），牺牲部分交互效果
- 或改用 ECharts（已有 Python 封装 pyecharts），通过生成静态 HTML 文件后打开

---

### 6.4 app.py 代码膨胀

**问题**：当前 `app.py` 已 4600+ 行，Phase 3 再加三个新页面将突破 7000 行，维护和协作成本极高。

**解决方案**（Phase 3 启动前必须完成）：

按页面拆分 app.py：
```
app.py                  # 入口：page 初始化 + 全局状态 + page_switcher（保留 ~300 行）
pages/
  search_page.py        # 检索页（build_project_page 及其内部函数）
  library_page.py       # 文献库页
  settings_page.py      # 设置页
  graph_page.py         # [NEW] 知识图谱页
  writing_page.py       # [NEW] 写作助手页
```

全局状态（`AppState`）和 Agent 面板（常驻右侧）保留在 `app.py`，页面函数通过参数注入所需回调，**禁止**跨文件直接访问全局变量。

---

### 6.5 Zotero 数据库版本兼容

**问题**：Zotero 不同版本的数据库表结构可能变化，直接 SQL 查询脆弱。

**解决方案**：
- 查询前检查 `version` 表确认 Zotero DB 版本
- 只依赖 Zotero 长期稳定的核心表：`items`、`itemData`、`fields`、`itemAttachments`
- 添加 `try/except` 保护每个字段查询，缺失字段以 `None` 填充而非抛异常
- 提供"跳过此文献"而非"整体失败"的容错逻辑

---

### 6.6 推送通知渠道配置

**问题**：SMTP 配置复杂（需要用户提供服务器、端口、授权码），微信推送需要第三方服务注册。

**解决方案**：
- 默认只启用桌面通知（零配置），邮件和微信作为可选高级功能
- 设置页提供 SMTP 配置向导，内置常见邮件服务商预设（Gmail/QQ Mail/163）
- 微信推送使用 WxPusher 而非企业微信，个人用户注册更简单
- 发送前在后台线程验证配置，失败时回退到桌面通知并提示用户检查配置

---

### 6.7 协作模式数据冲突

**问题**：多用户同时写入同一个 SQLite 数据库时可能产生锁竞争或数据冲突。

**解决方案**：
- 启用 SQLite WAL 模式（`PRAGMA journal_mode=WAL`），允许并发读 + 单写
- 写操作使用应用层乐观锁：更新前检查 `updated_at` 时间戳，不一致时提示冲突
- Phase 3 协作仅实现"本地多用户"（独立 config 文件，隔离数据库）和"局域网只读共享"，不实现双向实时同步（留给 Phase 4）

---

## 七、项目规范

> 以下规范为**强制性**要求，两位开发者均须遵守。规范变更须双方确认后更新本文档。

### 7.1 接口规范

#### 7.1.1 模块间接口冻结规则

以下接口为**已稳定接口**，Phase 3 中**禁止修改签名**，只允许向后兼容地添加可选参数（有默认值）：

| 模块 | 接口 | 说明 |
|------|------|------|
| `library.py` | `get_all_projects()` → `list[Project]` | 文献库所有课题 |
| `library.py` | `get_project_papers(project_id)` → `list[dict]` | 课题下论文列表 |
| `library.py` | `save_papers_to_project(pid, papers, scores)` → `(int, list)` | 保存检索结果 |
| `library.py` | `create_project(name, desc)` → `Project` | 新建课题 |
| `library.py` | `update_project(pid, **kwargs)` | 更新课题信息 |
| `ai_service.py` | `AIService.chat(project_id, project_name, message, ...)` → `dict` | Agent 对话 |
| `ai_service.py` | `AIService.deep_read(paper, full_text)` → `dict` | AI 精读 |
| `ai_service.py` | `AIService.score_papers(topic, papers)` → `list[dict]` | AI 精排 |
| `app.py` | `send_agent_message(text, role)` | Agent 面板发消息 |
| `app.py` | `set_agent_project(pid, name, desc)` | 切换 Agent 课题 |

**修改已稳定接口前必须**：
1. 在 PR 描述中说明修改原因和向后兼容方案
2. 搭档代码 Review 通过后方可合并
3. 更新本文档中的接口表格

#### 7.1.2 新接口规范

新增公共接口须满足：
- 函数签名有完整类型注解
- 参数名清晰，布尔参数改用枚举或具名参数
- 返回值为 `dict` 时，在函数注释中标注所有 key 及含义
- 添加对应单元测试（至少覆盖正常路径和空输入）

#### 7.1.3 数据库 Schema 变更规则

- `models.py` 是**两人共用的宪法文件**，任何字段变更必须先提 PR 合并，不得单方面修改
- 新增字段必须有默认值（不破坏现有数据库），推荐 `nullable=True` 或指定 `default`
- 新增表须同步更新 `init_db()` 并在 PR 中说明迁移方案
- 字段重命名须提供迁移脚本（`alembic` 或手写 SQL），不得直接删除旧字段

#### 7.1.4 config.yaml 变更规则

- 新增配置项必须同步更新 `config.example.yaml`（含注释说明）
- 新配置项须有合理默认值，缺失时不报错（程序自动回退默认）
- 敏感值（API Key、密码）**禁止**写入日志或代码注释
- `config.py` 中的 `load_config()` 是唯一配置读取入口，不得在其他模块硬编码路径或默认值

---

### 7.2 边界处理规范

#### 7.2.1 网络请求边界

所有外部 API 调用（LLM / arXiv / OpenAlex / Semantic Scholar）必须满足：

| 要求 | 说明 |
|------|------|
| 超时保护 | 每个请求必须设置 `timeout`，参考现有链路超时表（见 Phase1 报告第九章） |
| 失败降级 | 单个数据源失败不影响其他数据源；LLM 调用失败返回明确错误信息而非抛异常到 UI |
| 重试策略 | 仅对网络错误（连接超时 / 5xx）重试，最多 2 次；4xx 错误（认证失败 / 参数错误）不重试，立即返回错误 |
| 后台线程 | 所有网络 I/O 在 `threading.Thread(daemon=True)` 中执行，UI 通过 `threading.Event` 轮询，禁止在主线程阻塞 |

#### 7.2.2 文件系统边界

- PDF 路径在写入数据库前须用 `os.path.isfile()` 验证存在
- 所有文件写入使用 `try/except`，失败时日志记录并向上返回 `None`，不崩溃
- 缓存目录（`cache/`）和输出目录（`outputs/`）写入前须确保目录存在（`os.makedirs(exist_ok=True)`）
- 用户数据路径（`repository/`）使用相对路径，不假设绝对路径

#### 7.2.3 AI 输出边界

- LLM 返回的 JSON 须用 `try/except json.JSONDecodeError` 包裹，解析失败时返回降级结果而非抛出
- 精读 / 打分输出有 schema 校验（检查必填字段存在），缺字段时填充默认值
- AI 生成内容不得直接拼接到 SQL 查询（虽当前为 ORM，养成习惯）
- `ai_score` 须限制在 `[0, 100]` 范围内（`max(0, min(100, score))`）

#### 7.2.4 用户输入边界

- 课题名称：最长 200 字符，`strip()` 去首尾空白，空字符串不允许创建课题
- 关键词：单个最长 100 字符，超过截断；总数不超过 20 个（避免查询过于宽泛）
- API Key：仅检查非空，不在 UI 明文显示（用 `•••` 替代），不写入日志
- 年份筛选：验证为 4 位数字，范围 `[1900, 当前年份+1]`

---

### 7.3 Git 提交规范

#### 7.3.1 分支策略

```
main          ← 稳定版本，仅接受来自 develop 的 PR
develop       ← 日常开发集成分支
feature/xxx   ← 功能分支，从 develop 拉出，完成后 PR 回 develop
fix/xxx       ← Bug 修复分支
```

**禁止**直接向 `main` 或 `develop` push，所有变更通过 PR 合并。

#### 7.3.2 Commit Message 格式

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```
<type>(<scope>): <subject>

[可选 body，说明 WHY，不超过 72 字/行]
```

| type | 用途 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `refactor` | 重构（不改变功能） |
| `docs` | 文档更新 |
| `test` | 测试相关 |
| `chore` | 依赖/配置更新 |
| `perf` | 性能优化 |

scope 示例：`llm`、`sources`、`graph`、`push`、`writing`、`collab`、`ui`、`db`

示例：
```
feat(llm): add OpenAI-compatible LLM client abstraction

Replaces direct DeepSeek HTTP calls with LLMClient.
Supports DeepSeek / OpenAI / Qwen / GLM / Ollama via openai SDK.
Claude uses anthropic SDK with same interface.

fix(sources): handle Semantic Scholar 429 with exponential backoff

chore(deps): bump openai to 1.30.0, add anthropic 0.30.0
```

#### 7.3.3 PR 规范

每个 PR 必须包含：
1. **标题**：一句话描述变更（同 commit subject 格式）
2. **变更说明**：做了什么、为什么这样做
3. **测试说明**：如何验证功能正确（手动测试步骤 / 自动化测试）
4. **接口变更**（如有）：说明变更的接口签名及向后兼容方案
5. **数据库变更**（如有）：迁移方案

PR 大小：单个 PR 尽量不超过 400 行新增，大功能拆分为多个小 PR（如先合并 LLM 抽象层，再合并各 Provider 实现）。

#### 7.3.4 代码 Review 规则

- 涉及**稳定接口**变更的 PR：搭档必须 Review 且 Approve 后方可合并
- 涉及 `models.py` / `config.py` 变更的 PR：同上强制 Review
- 其他 PR：搭档 Review 为推荐项，48h 内无异议可自行合并
- Review 时重点关注：接口兼容性、边界处理、线程安全、配置安全

---

### 7.4 测试规范

| 测试类型 | 要求 | 工具 |
|---------|------|------|
| 单元测试 | 新增公共函数必须有单元测试 | `pytest` |
| 集成测试 | LLM Provider / 数据源接入必须有 mock 集成测试 | `pytest + unittest.mock` |
| 手动验证 | UI 新功能上线前，两人分别在本地完整走一遍主流程 | — |
| 回归验证 | 涉及检索管道 / AI 服务的修改，跑 `test_app_integration.py` | `pytest` |

**禁止**：测试文件提交包含真实 API Key；测试依赖真实网络请求（须 mock）。

---

### 7.5 开发启动检查清单（Phase 3 开始前）

- [ ] `app.py` 按页面拆分完成（见 6.4 节方案），不超过 500 行
- [ ] `requirements.txt` 锁定新依赖版本（`pip freeze > requirements.txt` 后人工审查）
- [ ] `config.example.yaml` 更新，包含 `llm.provider` 等新字段及注释
- [ ] `models.py` PR 合并，新增 `PushRecord` 表，存量数据库迁移脚本就绪
- [ ] 本地验证现有功能（Phase 1+2）在 `develop` 分支上正常运行
- [ ] 两人确认 Phase 3a / 3b 功能分工，记录在本文档附录

---

## 八、Phase 3 功能分工建议

> 以下为建议分工，两人可协商调整。核心原则：**后端接口先行，UI 后接入**。

### Phase 3a（优先）

| 功能 | 建议负责人 | 说明 |
|------|-----------|------|
| LLM 抽象层（`llm_client.py`） | 后端开发者 | 影响所有 AI 功能，优先完成 |
| 设置页多模型 UI | 前端开发者 | 接入 LLMClient，配置 Provider/Key/Model |
| Semantic Scholar 数据源 | 后端开发者 | 复用现有 fetcher 模式 |
| Zotero 导入 | 后端开发者 | 独立模块，低耦合 |
| 数据源 UI（设置页开关） | 前端开发者 | 复用现有 arXiv/OpenAlex 开关模式 |
| 推送通知后端（`push_service.py`） | 后端开发者 | apscheduler + 推送渠道 |
| 推送配置 UI（设置页） | 前端开发者 | 周期/阈值/渠道配置 |

### Phase 3b（后续）

| 功能 | 建议负责人 | 说明 |
|------|-----------|------|
| 知识图谱数据层（`graph_service.py`） | 后端开发者 | 节点/边构建，输出 JSON |
| 知识图谱前端（`ui/graph_view.html`） | 前端开发者 | Vis.js + pywebview 交互 |
| AI 辅助写作后端（`writing_service.py`） | 后端开发者 | Prompt 工程 + python-docx 导出 |
| 写作助手 UI | 前端开发者 | 输入框 + 生成结果展示 + 导出按钮 |
| 协作模式（本地多用户） | 后端开发者 | 用户切换 + config 隔离 |
| 协作模式 UI | 前端开发者 | 用户选择界面 + 阅读进度汇总 |

---

## 附录：关键技术参考

| 资源 | 地址 |
|------|------|
| Semantic Scholar API 文档 | https://api.semanticscholar.org/api-docs/ |
| OpenAI Python SDK | https://github.com/openai/openai-python |
| Anthropic Python SDK | https://github.com/anthropics/anthropic-sdk-python |
| Ollama API 文档 | https://github.com/ollama/ollama/blob/main/docs/api.md |
| Vis.js Network 文档 | https://visjs.github.io/vis-network/docs/network/ |
| apscheduler 文档 | https://apscheduler.readthedocs.io/ |
| Zotero DB Schema | https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/db.js |
| Conventional Commits | https://www.conventionalcommits.org/zh-hans/ |

---

*文档作者：Claude Opus 4.6（辅助生成） | 最终版本由开发者确认后生效*
*如有疑问或需要修订，直接编辑本文档并在 PR 中说明变更原因*



