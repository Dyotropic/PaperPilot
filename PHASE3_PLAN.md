# PaperPilot Phase 3 开发方案

> 文档版本：v1.1 | 核对日期：2026-09-19 | 状态：部分已实现，待办未重新排期
>
> 本文档区分当前代码与未来设计。已实现不等于已验收；本轮测试摘要记录在本文 7.3 节。旧报告和 Word/PPT 保留为历史材料，不作为当前功能说明。原有协作/合并流程已移除；接口表是历史设计约定，修改前须与当前源码核对。

---

## 一、项目背景与现状回顾

### 1.1 已完成工作

| 阶段 | 核心产出 |
|------|----------|
| **Phase 1** | 课题关键词提取 → 中英文翻译 → arXiv/OpenAlex 双源检索 → Cross-Encoder 语义精排 → 拖拽三区关键词管理 |
| **Phase 2** | AI 精读（RLM 三层策略）→ AI 精排打分 → StudyCopilot 对话助手 → 文献库 CRUD → 本地 PDF 导入 → BibTeX/CSV 导出 → 对话持久化与压缩 |

### 1.2 技术栈现状

已实现：页面拆分、多供应商 LLM、数据源抽象、Europe PMC、ECharts 知识图谱。未实现：定时推送、Zotero 导入、独立 AI 写作、本地多用户与网络协作。

```
UI 层       : Flet 0.85 (Flutter/Python 跨平台桌面)
AI 服务     : 统一 LLMClient（OpenAI 兼容 / Anthropic，含本地 Ollama）, 本地 Cross-Encoder (mxbai-rerank-base-v2)
数据存储    : SQLite + SQLAlchemy ORM, 本地文件系统 (repository/)
文献源      : arXiv API + OpenAlex API + Europe PMC API
PDF 处理    : PyMuPDF + pywebview + PDF.js (独立窗口)
关键词      : LLM 提取优先；中文回退为 jieba 候选 + 可选 MiniLM 筛选，英文回退依赖 KeyBERT 模型
对话管理    : 本地 JSON 持久化 + 滑动窗口压缩
```

### 1.3 PPT "接下来要做的" — Phase 3 五大方向

根据答辩 PPT 第 9 页（ROADMAP），下一阶段重点：

1. **智能推送通知** — 定时检索 + 邮件/微信推送
2. **扩展数据源** — Europe PMC（已接入）、Zotero 导入（待实现）、CNKI（备选）
3. **多模型 / 自定义 API Key 支持** — OpenAI/Claude/GLM 等可切换
4. **可视化知识图谱 + AI 辅助写作** — 引用关系可视化 + 综述草稿生成
5. **协作模式** — 多用户共享文献库 + 批注协同

---

## 二、功能详细规划

### 2.1 功能一：智能推送通知（未实现，以下为设计）

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

> **修订（2026-08-28）**：Semantic Scholar 经实测不可用（API 稳定性差），**弃用**；
> 已改接 **Europe PMC**（免 Key、含生物医学与最新预印本，按被引数降序）作为第三数据源。
> 数据源统一抽象层已落地：`paperpilot/sources/`（base.py 定义 `PaperSource` 接口 + 注册表，
> 三个内置源已迁移接入，`fetcher.py` 保留编排逻辑并重导出兼容）。
> Zotero 只读导入仍按本节计划推进；CrossRef 维持可选增强。

#### 子功能拆解

| 数据源 | 接入方式 | 说明 |
|--------|----------|------|
| **Europe PMC** | REST API | 已实现第三源适配器；Semantic Scholar 已弃用 |
| **CNKI/知网** | 网页爬虫（无官方 API） | 需模拟浏览器，受反爬限制，优先级低 |
| **Zotero 本地库** | 读取 Zotero SQLite DB | 免安装，直接解析 `zotero.sqlite` |
| **CrossRef** | 免费 REST API | 补充 DOI → 元数据解析，增强现有管道 |

> **优先级**：Europe PMC 已完成；待办为 Zotero 导入 > CrossRef 增强 > CNKI（技术难度高，放入备选）

#### 数据源统一抽象层（已实现）

PaperSource 是基类，提供 fetch(keywords, max_results, logic, year_min, year_max)、fetch_raw(query, max_results, year_min, year_max) 和 is_available()。后者检查配置开关，不是连通性测试。

内置 ArxivSource、OpenAlexSource、EuropePMCSource 已注册；fetcher 保留级联和多主关键词编排及兼容导出。当前 UI 三源按顺序执行。

---

### 2.3 功能三：多模型 / 自定义 API Key 支持（客户端与 UI 已实现）

#### 目标
支持用户在设置页切换 AI 后端（OpenAI / Claude / GLM / 本地 Ollama），不再绑定单一 DeepSeek API。

#### 子功能拆解

| 子功能 | 说明 |
|--------|------|
| 统一 LLM 接口层 | 所有 AI 调用通过 `LLMClient` 抽象，屏蔽具体 provider 差异 |
| Provider 支持列表 | DeepSeek（现有）、OpenAI/ChatGPT、Claude（Anthropic）、智谱 GLM、Ollama（本地） |
| 设置页 UI | 下拉选择 Provider → 输入 API Key → 测试连通性 → 保存 |
| OpenAI 格式兼容 | 大量第三方模型（如 Qwen、Moonshot）提供 OpenAI 兼容接口，一套代码复用 |
| Key 安全存储 | 当前为本地明文 YAML，可引用环境变量；keyring/加密尚未实现，禁止明文日志 |

#### 当前 LLM 接口

get_client(task=None) 根据配置创建 OpenAICompatClient 或 AnthropicClient；Ollama 复用 OpenAI 兼容客户端，不存在独立 OllamaClient。

LLMClient.chat(messages, temperature=0.3, max_tokens=2000, timeout=120, model=None, thinking=None, retries=1) 返回 ChatResult(content, reasoning)，不是字符串。chat_stream 返回文本迭代器；test_connection 返回 (bool, message)。is_available 是属性，配置可用不等于服务连通。

#### config.yaml 新增字段

```yaml
llm:
  provider: deepseek          # deepseek / openai / anthropic / glm / kimi / qwen / ollama
  api_key: sk-xxx
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com/v1  # 可覆盖，兼容第三方
```

---

### 2.4 功能四：可视化知识图谱 + AI 辅助写作

#### 目标
将文献库中的论文关系以图谱形式可视化展示；基于课题和文献库自动生成研究综述草稿。

#### 子功能拆解

**知识图谱**

> **修订（2026-08-29，已落地）**：渲染方案定为 **pywebview 独立窗口 + ECharts graph**
> （复用 PDF 阅读器的子进程窗口方案，经 `pdf_viewer._create_window` 新增的
> `js_api_factory` 键注入 JS↔Python 桥）；引用关系数据源由 Semantic Scholar
> （已弃用）改为 **OpenAlex `referenced_works` 两级缓存**：
> ① 检索入库时顺带把响应中本就携带的 `referenced_works`/`keywords` 写入
> diskcache（`refs:{doi}`，零额外请求）；② 图谱构建时未命中者按 DOI 批量补查
> （每批 ≤50 篇 1 次请求，`select=id,doi,referenced_works,keywords`），写回后
> 不再联网。实现见 `paperpilot/graph_service.py`（数据构建）与
> `paperpilot/graph_window.py`（窗口），入口为文献库页工具栏「知识图谱」按钮。

| 子功能 | 说明 |
|--------|------|
| 引用关系图 | 以 DOI/标题为节点，引用关系为边，展示论文间的引用网络 |
| 关键词共现图 | 论文之间共有关键词越多，边越粗 |
| 时间线视图 | 按年份排列论文，直观展示研究演进脉络 |
| 交互操作 | 点击节点展示详情、通过操作打开 PDF；支持拖拽/缩放 |
| 渲染方案 | pywebview 独立窗口 + ECharts Graph（已实现） |

**AI 辅助写作（未实现，以下为设计）**

| 子功能 | 说明 |
|--------|------|
| 研究综述生成 | 基于文献库 + 课题描述，生成结构化综述草稿（背景/现状/方法/趋势） |
| 大纲生成 | 根据课题方向生成论文/报告大纲框架 |
| 段落扩写 | 用户输入关键句 + 引用论文，AI 扩写为学术段落 |
| 引文插入 | 生成文字时自动标注 [作者, 年份] 引用格式 |
| 导出 | 生成内容支持导出为 .md / .docx |

---

### 2.5 功能五：协作模式（未实现，以下为设计）

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

## 三、当前系统架构与文件结构

```text
app.py → pages/{context,sidebar,search_page,library_page,agent_panel,settings_page,components}.py
            ↓ UI 回调、后台任务、AppContext
检索：keywords/core_extractor/mt_translator → fetcher → sources/{arxiv,openalex,europepmc}_source
排序：indexer.rank_papers（API 粗筛 → Cross-Encoder → 关键词加分 → 短摘要降权）
存储：library/models（SQLite） + repo_manager（repository、目录 JSON、缓存、回收站）
AI：ai_service → llm_client；conversation 保存会话和压缩摘要
阅读：downloader/local_import → pdf_viewer（PDF.js + pywebview 子进程）
图谱：graph_service → graph_window（ECharts + pywebview），OpenAlex 引用缓存/补查
导出：export（BibTeX/CSV）
```

app.py 已拆分为入口与页面组装；ui/ 不是当前图谱前端所在位置，HTML 模板位于 graph_window.py。Ollama 复用 OpenAICompatClient，没有单独的 OllamaClient 类。

模型仍保留 embedding_id、五维打分与 push_interval_days 等历史字段，不表示 FAISS、自适应权重或定时推送已启用。当前只有 Project、Paper、ProjectPaper、Feedback、Keyword 五张业务表。

未来可能新增 writing_service、push_service、协作及 Zotero 导入模块；这些是候选设计文件名，不是现有文件。

## 四、可行性研究

### 4.1 技术可行性

| 方向 | 可行性 | 说明 |
|------|--------|------|
| 智能推送通知 | **高** | Windows 系统托盘 + Toast 通知已有成熟库（pystray + win10toast）；SMTP 邮件为标准协议 |
| Europe PMC | 已接入 | 使用现有适配器；服务连通性须实际验证 |
| Zotero 导入 | **高** | Zotero 使用 SQLite，直接读取即可，无需反爬 |
| 多模型支持 | **高** | DeepSeek/OpenAI/Claude 均提供 REST API；OpenAI 兼容格式统一了大多数第三方模型 |
| 知识图谱可视化 | **中** | 已采用 pywebview + ECharts，引用关系来自 OpenAlex；网络失败可能导致图谱不完整 |
| AI 辅助写作 | **高** | 复用现有 LLM 调用能力，主要是 Prompt 工程和 UI 设计 |
| 协作（本地多用户） | **高** | SQLite WAL 模式支持多进程并发读写，用户切换只需隔离 config 路径 |
| 协作（局域网共享） | **中** | 需引入轻量 HTTP 服务器（FastAPI/Flask），对 Flet 桌面应用是新增复杂度 |
| CNKI 爬虫 | **低** | 有严格反爬措施，法律风险，**建议暂不实现** |

### 4.2 进度与后续排期

旧“四周开发排期”是 2026-07 的计划，不代表当前剩余期限，亦不是完成证据。

| 工作 | 当前状态 | 后续范围 |
|------|----------|----------|
| 页面拆分、多模型与设置页 | 已实现 | 回归验证与兼容性完善 |
| 数据源抽象、第三源 | 已实现 Europe PMC | Semantic Scholar 已弃用 |
| 知识图谱 | 已实现 | 交互、离线降级与性能验证 |
| Zotero 只读导入 | 未实现 | 待确定版本兼容与导入边界 |
| 定时推送 | 未实现 | 待设计调度、记录、去重与通知 |
| AI 写作 | 未实现 | 待设计引用可追溯、编辑和导出 |
| 本地多用户 | 未实现 | 待设计身份、数据/配置隔离 |
| CNKI/局域网/云同步 | 备选 | 不纳入现有功能承诺 |

开发者重新确认范围与验收标准后排期，不沿用过期日程。

### 4.3 风险评估

| 风险 | 概率 | 影响 | 应对策略 |
|------|------|------|----------|
| 学术数据源限流（实际额度以服务端为准） | 中 | 中 | 本地缓存 + 指数退避重试，超额时降级到已有源 |
| 图谱节点/共现边过多 | 中 | 中 | 已采用 ECharts，仍需检查大数据量与交互性能 |
| OpenAI/Claude API 格式与 DeepSeek 有细微差异 | 中 | 低 | 抽象层做参数适配，单独编写集成测试 |
| Zotero 数据库结构随版本变化 | 低 | 中 | 只依赖 Zotero 稳定表（items / itemAttachments），做版本检测 |
| 协作局域网方案引入安全漏洞 | 低 | 高 | 仅局域网，不暴露公网；接入时做基本 Token 鉴权 |
| 页面模块继续膨胀 | 中 | 中 | app.py 已拆分；后续保持 AppContext 与业务接口边界 |

---

## 五、技术选型与实现方法

### 5.1 多模型 LLM 抽象层（已实现）

使用 openai 与 anthropic SDK。供应商映射、默认模型和候选列表以 llm_client.py 为准，文档不再复制易过期的型号表作为事实承诺。ai_service 的 _call_api/_call_api_full 调用 get_client().chat() 并读取 ChatResult。任务模型通过 get_task_model 解析；score/chat/reasoning 配置分别用于相应业务路径。

### 5.2 Europe PMC 数据源（已实现）

实现位于 sources/europepmc_source.py，通过 PaperSource 注册，fetcher 统一编排。Semantic Scholar 在 2026-08-28 修订中弃用，不再作为待实现项。

### 5.3 Zotero 本地导入（未实现，候选设计）

Zotero 导入拟以用户选择的数据库路径为入口，实际数据目录和 schema 必须按目标版本验证，不硬编码 profile 路径或条目类型编号。

```python
# paperpilot/sources/zotero_import.py
def import_from_zotero(db_path: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读，不锁定 Zotero
    # 查询 items + itemData（title/abstractNote/date/DOI）
    # 按目标版本 schema 识别并排除附件和笔记类型
    ...
```

关键点：以 `?mode=ro` URI 方式只读打开，不影响 Zotero 运行中写入。

---

### 5.4 知识图谱可视化（已实现）

数据在 graph_service.build_graph_data 构建，包括节点、引用/共现边、警告、统计和时间线坐标。OpenAlex 缓存优先，缺失时补查；局部网络失败返回警告。引用边使用反向索引查表，关键词共现仍为两两比较，不能声称整个构图为 O(n)。

graph_window.py 内嵌 ECharts HTML 模板，在独立 pywebview 窗口展示引用、共现和时间线视图。节点详情在前端渲染，“打开 PDF”经 JS 桥调用阅读器；不存在 ui/graph_view.html 或 Vis.js 当前实现。

### 5.5 智能推送通知（未实现，候选设计）

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

### 5.6 AI 辅助写作（未实现，候选设计）

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

### 6.2 学术数据源限流

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

### 6.4 页面拆分（已完成）

app.py 已降为约 230 行，页面在 pages/，通过 AppContext 共享状态与回调；后续新增页面继续遵守这一边界。原先“4600+ 行、启动 Phase 3 前拆分”描述已失效。

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

以下是 Phase 3 设计时记录的公共接口，不代表当前源码的完整签名。修改前须核对实现及调用点；优先保持向后兼容：

| 模块 | 接口 | 说明 |
|------|------|------|
| `library.py` | `get_all_projects()` → `list[Project]` | 文献库所有课题 |
| `library.py` | `get_project_papers(project_id, status_filter=None)` → `list[dict]` | 课题下论文列表 |
| `library.py` | `save_papers_to_project(project_id, papers, scores=None)` → `tuple[int, int]`（新增关联数、补填 PDF 路径数） | 保存检索结果 |
| `library.py` | `create_project(name, description, push_interval_days=7)` → `Project` | 新建课题 |
| `library.py` | `update_project(project_id, name=None, description=None, push_interval_days=None)` → `bool` | 更新课题信息 |
| `ai_service.py` | `AIService.chat(project_id, project_name, message, ...)` → `dict` | Agent 对话 |
| `ai_service.py` | `AIService.deep_read(paper, full_text=None)` → `dict` | AI 精读 |
| `ai_service.py` | `AIService.score_papers(topic_desc, papers, max_papers=50)` → `list[dict]` | AI 精排 |
| `pages/agent_panel.py` | `send_agent_message(text, role="user")` | Agent 面板发消息 |
| `pages/agent_panel.py` | `set_agent_project(project_id, project_name="", topic_desc="")` | 切换 Agent 课题 |

修改上述接口时，应说明必要性、调用方影响和向后兼容方案，并同步更新相关接口记录。

#### 7.1.2 新接口规范

新增公共接口须满足：
- 函数签名有完整类型注解
- 参数名清晰，布尔参数改用枚举或具名参数
- 返回值为 `dict` 时，在函数注释中标注所有 key 及含义
- 添加对应单元测试（至少覆盖正常路径和空输入）

#### 7.1.3 数据库 Schema 变更规则

- `models.py` 字段变更须先核对现有数据库、调用方与迁移方案，不得只按新库设计直接改写
- 新增字段必须有默认值（不破坏现有数据库），推荐 `nullable=True` 或指定 `default`
- 新增表须同步更新 `init_db()` 并说明迁移方案
- 字段重命名须提供迁移脚本（`alembic` 或手写 SQL），不得直接删除旧字段

#### 7.1.4 config.yaml 变更规则

- 新增配置项必须同步更新 `config.example.yaml`（含注释说明）
- 新配置项须有合理默认值，缺失时不报错（程序自动回退默认）
- 敏感值（API Key、密码）**禁止**写入日志或代码注释
- `config.py` 中的 `load_config()` 是唯一配置读取入口，不得在其他模块硬编码路径或默认值

---

### 7.2 边界处理规范

#### 7.2.1 网络请求边界

所有外部 API 调用（LLM / arXiv / OpenAlex / Europe PMC）必须满足：

| 要求 | 说明 |
|------|------|
| 超时保护 | 每个请求必须设置 `timeout`，以当前函数实现为准；CE 缓存加载 180 秒、预测 300 秒，在线下载路径需另测 |
| 失败降级 | 单个数据源失败不影响其他数据源；LLM 调用失败返回明确错误信息而非抛异常到 UI |
| 重试策略 | 仅对网络错误（连接超时 / 5xx）重试，最多 2 次；4xx 错误（认证失败 / 参数错误）不重试，立即返回错误 |
| 后台线程 | 所有网络 I/O 在 `threading.Thread(daemon=True)` 中执行，UI 通过 `threading.Event` 轮询，禁止在主线程阻塞 |

#### 7.2.2 文件系统边界

- PDF 路径在写入数据库前须用 `os.path.isfile()` 验证存在
- 所有文件写入使用 `try/except`，失败时日志记录并向上返回 `None`，不崩溃
- 缓存目录（`cache/`）和输出目录（`outputs/`）写入前须确保目录存在（`os.makedirs(exist_ok=True)`）
- 用户数据目录锚定应用根目录，不依赖启动工作目录；库内 PDF 路径可为绝对路径

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

### 7.3 验证记录与现有入口

本轮验证采用本地独立 Python 脚本和 check/assert，并补充 unittest 与 UI 检查；这些测试脚本、报告和证据目录不随本次项目提交。单元/mock 集成测试不得依赖真实 API；真实服务与端到端验收使用隔离数据库、文件和小规模调用。

2026-09-19 本机结果：六组离线测试 256/256、数据库路径 4/4、端到端 64/64、扩展真实服务 10/10、边界场景 14/14，共 348 项通过；另有 5 项 unittest 与 8 项 UI 检查通过。PDF/图谱窗口最小化恢复连续执行 24 次，未复现此前的一次整窗空白，但该偶发问题的根因尚未确认。

上述数字只代表该次本机快照，拉取仓库后不能据此推定当前环境仍通过。测试缺失、跳过、服务受限、降级与功能失败必须分别报告；没有完整执行的链路不得判为通过。测试不得写入用户数据库/仓库或泄露真实 Key。

### 7.4 当前开发检查清单

- [x] 页面拆分，app.py 小于 500 行。
- [x] LLM 抽象和 config.example.yaml 已包含多供应商字段。
- [x] 三个数据源及知识图谱已接入 UI。
- [ ] 依赖版本锁定与可重复安装验证。
- [x] 2026-09-19 本机离线、真实服务与 UI 回归完成，结果见 7.3 节。
- [ ] PDF/图谱窗口此前一次整窗空白的根因确认与针对性回归。
- [ ] Zotero、推送、写作、多用户：设计与开发尚未完成。

## 八、后续开发边界

新增能力先确定接口和数据契约，再接 UI；核心打分和排序策略须结合业务目标审视。已完成底座不重复安排“从零开发”。推送/写作/协作若需新增表与字段，须先明确迁移方案；未落实的新功能应按当前需求重新排序，不将旧建议视为已接受任务。

## 附录：关键技术参考

| 资源 | 地址 |
|------|------|
| Europe PMC 文档 | https://europepmc.org/RestfulWebService |
| OpenAI Python SDK | https://github.com/openai/openai-python |
| Anthropic Python SDK | https://github.com/anthropics/anthropic-sdk-python |
| Ollama API 文档 | https://github.com/ollama/ollama/blob/main/docs/api.md |
| ECharts | https://github.com/apache/echarts |
| apscheduler 文档 | https://apscheduler.readthedocs.io/ |
| Zotero DB Schema | https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/db.js |
| Conventional Commits | https://www.conventionalcommits.org/zh-hans/ |

---

*文档作者：Claude Opus 4.6（辅助生成） | 最终版本由开发者确认后生效*
*如有疑问或需要修订，应说明变更原因并核对当前源码。*



