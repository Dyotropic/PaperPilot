# PaperPilot 架构与工程说明

> 核对日期：2026-10-02。描述当前工作区实现；功能存在不等于所有环境、模型和外部服务均已验收。接口变更仍须核对源码与调用方。

## 文档分工

| 文档 | 用途 |
| --- | --- |
| [README.md](README.md) | 项目介绍与快速开始 |
| [USER_GUIDE.md](USER_GUIDE.md) | 安装、配置、操作、故障排查与备份 |
| 本文 | 当前架构、模块关系、接口、数据与配置契约 |
| [TESTING.md](TESTING.md) | 验证入口、环境隔离、真实服务与证据边界 |
| [PHASE3_PLAN.md](PHASE3_PLAN.md) | 项目发展脉络与尚未实现的方向 |
| [AGENTS.md](AGENTS.md)、[CLAUDE.md](CLAUDE.md) | 当前工程约定与开发提示 |

早期阶段报告及配套 Word/PPT 是历史资料。其旧接口、排序方案、协作分工和测试数字不作为现行规范。近期专项研究与实施报告中的有效设计已归入长期文档，原始验证计数和截图保存在 `validation_evidence/`。

## 项目定位与主流程

PaperPilot 面向学生及科研人员的课题研究，以 Python + Flet 桌面应用整合课题分析、文献检索、排序、归档、阅读和学术对话。数据存于本机，当前没有账号系统、共享服务器或后台定时推送服务。

主要业务链路为：课题描述 → 分层关键词 → 启用的数据源召回 → 去重与排序 → 保存课题文献库 → 获取原文 → AI 精读、评分或对话 → 导出、图谱。DOI、arXiv ID、完整标题的精确查找另走严格匹配路径；本地 PDF 可直接导入文献库。

| 层次 | 入口与职责 |
| --- | --- |
| 应用装配 | `app.py` 初始化配置、数据库、窗口及页面回调，启动回收站清理 |
| 页面 | `pages/search_page.py`、`library_page.py`、`settings_page.py`、`sidebar.py`；`agent_panel.py` 管理聊天、会话、命令、用量及运行状态 |
| UI 共享 | `pages/context.py` 集中设计令牌、主题、AppState/AppContext 与回调注册；`components.py` 提供复用控件 |
| 检索 | `keywords.py`、`mt_translator.py` 提取和翻译；`fetcher.py` 级联召回；`sources/` 适配三源；`search_filters.py`、`exact_search.py` 管理严格筛选和精确匹配 |
| 排序与归档 | `indexer.py` 精排；`library.py`、`models.py` 管理 SQLite；`repo_manager.py` 管理课题目录、PDF、缓存与回收站；`local_import.py`、`export.py` 导入导出 |
| 模型与 Agent | `llm_client.py` 适配服务商；`ai_service.py` 编排精读、评分及聊天；`agent_sessions.py` 管理独立会话；`conversation.py` 管理事件、历史和压缩；`context_budget.py` 管理预算；`agent_runtime.py` 管理运行及取消；`llm_usage.py` 记录计数 |
| 原文与图谱 | `downloader.py` 获取 PDF/HTML；`pdf_viewer.py` 通过 pywebview + PDF.js 阅读；`graph_service.py` 构图；`graph_window.py` 通过 ECharts 展示 |

页面通过 `pages.context.ctx` 使用共享状态和注入回调，不反向导入 `app.py`。业务层保留同步公共接口，耗时工作由页面调度到后台；运行状态随任务链延续，避免模型输出结束后工具仍在执行而界面误判为空闲。

## 检索、身份与评分

### 课题检索

- 关键词分为主、副、普通三层。LLM 提取失败或未配置时，中文回退到 jieba TF-IDF，MiniLM 可选增强；英文回退依赖 KeyBERT 及其模型。
- arXiv、OpenAlex、Europe PMC 按启用情况参与召回。多主词分别召回并合并；级联策略在不放宽用户筛选的前提下调整关键词组合。
- 年份区间、单个作者姓名短语、完整期刊名按 AND 组合，尽量下推到各源原生查询，并在返回结果上核验；缺失元数据不能当作满足筛选。分页、超时与候选上限控制请求，数量不足须显示原因。
- 当前排序为 **API 分数粗筛 → Cross-Encoder 精排 → 关键词加分 → 短摘要降权**，主流程不使用 FAISS。CE 模型是 `mixedbread-ai/mxbai-rerank-base-v2`，优先本地缓存，加载或预测失败时退回 API 分数基础排序，后续修正仍生效。
- API 分、CE/关键词排序分及 AI 四维评分不是同一尺度，不应混写为同一种“相关性分”。AI 评分基于摘要，独立保存；不得无需求改动权重。

### 精确查找与文献身份

精确查找支持 DOI、arXiv ID（含明确版本）和完整标题，不支持 ISBN。标题只进行既定 Unicode、空白和末尾句号规范化后比较；不以模糊相似度冒充精确匹配。该路径不调用 CE，内部占位分数在 UI 显示为“精确”。

去重与入库优先使用 DOI、arXiv ID、OpenAlex ID、稳定 URL，再考虑标题与年份等弱标识。不同 DOI 不得因同名标题合并；明确不同的 arXiv 版本保守区分；弱标识不能把本地 PDF 与不同网络论文误并。相关规则由 `fetcher.py`、`library.py` 和来源适配器共同维护。

保存检索结果只保存当前展示的排序结果。`save_papers_to_project(...)` 的两个返回计数分别为新增课题关联数、补填 PDF 路径数，第二项不是下载成功数。后续 PDF 下载独立执行，下载不到不否定元数据已经入库。

## AI 服务与调用契约

### 统一模型适配

支持 `deepseek / openai / anthropic / glm / kimi / qwen / ollama`。除 Anthropic SDK 外，其余走 OpenAI 兼容 SDK；支持不等于每家最新模型、思考参数及代理端点均已实测。

`get_client(task=None)` 每次调用读取配置并解析任务模型。`score_model`、`chat_model` 是同一 provider 下的覆盖；非空 `reasoning_model` 启用先推理再回答的聊天流程。关键词提取、翻译及未覆盖任务使用主模型。`thinking=None` 表示不显式传参、采用模型默认值；`True/False` 经适配层转换，不能把“默认”当作关闭推理。运行中的请求继续使用创建时的 client，新配置作用于后续调用。

`ChatResult` 包含 `content`、`reasoning`、`usage`、`provider`、`model`、`request_id`、`elapsed_ms`、`first_token_ms`、`finish_reason`。字段可能为空；推理 token 若已计入服务商输出计数，不再重复相加。`LLMClient.chat(...)` 返回该对象，`chat_stream(...)` 逐段返回正文。

### 精读与评分

精读原文按 PDF → HTML → 摘要回退获取，结果必须依据实际取得的材料解读。文本长度以字符计：≤8,000 直接分析，>8,000 且 ≤60,000 采用滑动窗口及渐进笔记，>60,000 切块分析再合成；边界由 `_TIER1_MAX/_TIER2_MAX` 控制。窗口/分块无有效笔记时回退到正文前 8,000 字符，最终合成使用阅读笔记前 15,000 字符。报告包含贡献、方法、证据、亮点、局限与新颖性/严谨性/重要性三维评分，不能把降级分析说成完整全文审阅。

AI 精细打分以课题描述和论文摘要为依据，分批请求，返回四维理由、0–100 综合分与档位。精读笔记与评分经文献库接口保存，精读 JSON 另存 `outputs/deep_read/`。失败、取消或无有效内容不得被记录为已完成精读。

### 主要公共入口

下表省略可选参数，完整签名以所列源码为准；既有位置参数保持兼容，新会话参数优先以关键字传递。

| 源文件 | 入口与关键语义 |
| --- | --- |
| `fetcher.py` | `fetch_with_cascade(primary_kw, secondary_kw, regular_kw, source=..., ...)`；`fetch_multi_primary(...)`；`filters/max_pages/request_timeout` 为关键字参数 |
| `indexer.py` | `rank_papers(query, papers, top_k=50, ce_candidates=100, ...)` 返回 `(paper, score)` 列表；函数缺省值与 UI/示例配置值分别管理 |
| `library.py` | `create_project(name, description, push_interval_days=7)`、`get_all_projects()`、`get_project_papers(project_id, status_filter=None)`、`save_papers_to_project(project_id, papers, scores=None)`、`update_project(...)` |
| `ai_service.py` | `AIService.deep_read(paper, full_text=None)`；`score_papers(topic_desc, papers, max_papers=50)`；`chat(project_id, project_name, message, ..., session_id=..., include_library_context=..., operation=...)` 返回 `reply/compressed/session_id` |
| `ai_service.py` | `create_session(...)`、`select_session(...)`、`get_conversation(..., session_id=...)`；`get_context_status(..., session_id=..., draft=...)`；`compact_context(..., session_id=...)` 返回完成、未变化或失败状态 |
| `graph_service.py` | `build_graph_data(project_id, papers, on_progress=None)` 为图谱窗口提供节点与关系 |
| `pages/agent_panel.py` | `send_agent_message(text, role='user')`、`set_agent_project(project_id, project_name='', topic_desc='')`、`begin_agent_run(...)`、`stop_agent_run(...)` |

## Agent 会话、停止与恢复

StudyCopilot 是课题感知聊天与已有应用功能编排器。当前通过回复中的动作标记及面板解析调度检索、保存、评分、精读与课题更新等功能；不能描述为已经具备通用代码执行沙箱、任意工具协议或多代理运行平台。

### 独立会话与资料注入

同一课题可创建、切换、重命名多个独立聊天。会话共享文献库，各自保存历史、摘要和运行状态；`sessions/index.json` 保存列表与最近活动会话，UUID 标识会话。旧课题单份 `conversation.json` 迁移为“历史对话”，保留旧文件及 `legacy-conversation.json` 备份；格式异常时保留原件并报错，不自动覆盖为空记录。

聊天固定 system 提供稳定的身份及动作约定，课题变化以独立消息记录。选中文献、标题引用与研究现状等预设提供论文资料。预设仅使用标题及摘要节选，不默认注入全部全文；同一会话中未变化的资料复用已有快照，资料变化或被压缩移出有效上下文时再注入。覆盖篇数和截取范围要明确。

会话及课题身份在任务开始时固定，迟到结果仍归属原会话。UI 不将迟到回复错误展示到新切换的聊天。

### 停止当前轮

空闲时为发送箭头，工作中为停止方块；停止语义是结束当前轮，保留已落盘历史及已完成成果。`AgentRun`、取消令牌与运行作用域贯穿模型、动作和并发子任务，检查点阻止尚未开始的后续操作。

模型请求通过可取消的流式传输取得片段；取消会关闭当前流，不自动重试。生成片段保留为中断记录，未完成的动作标记不派发。已保存文献、完成评分及笔记不回滚。同步下载或本地计算无法安全强杀线程，须当前步骤返回后退出，期间显示“正在停止”；迟到结果受取消检查约束。

目标、阶段、已完成步骤与终态存于会话元数据。重启后无存活任务的未结束轮标记为 interrupted，不自动重执行。发送“继续”以保存的目标及状态开启新一轮，未完成部分可能重新计算；不从 HTTP 断点续传。流式片段在突然崩溃前尚未落盘时，不能保证恢复。

## 上下文管理

### 三种计量分开

| 指标 | 含义 |
| --- | --- |
| 当前上下文占用 | 固定 system + 最新有效摘要 + 未压缩消息及已注入资料的近似 token；草稿另列 |
| 累计用量 | 服务商返回的每次输入、输出计数之和；重复发送历史会重复计费 |
| 缓存命中率 | 有缓存计数的请求中，命中的输入 token 占已报告缓存输入 token 的比例 |

占用按字符和消息封装估算，最近主聊天的实际输入用量可作为基准，后续消息按估算增量修正；始终显示 `≈`，不是 tokenizer 精确计数。压缩提交或更换模型使旧基准失效，压缩请求的输入计数不作为压缩后聊天占用。

`context_budget.py` 按 provider/model 查容量。内置 DeepSeek V4 Flash/Pro 相关 ID 使用保守十进制 1,000,000 token，其他模型未知时显示“未配置”。用户可在上下文详情保存该模型容量覆盖值，或配置 `agent.context_windows`；代理端点限制可能不同，应按实际限制填写。

已知容量默认达到 70% 时尝试压缩，为输出预留 `min(8192, window // 8)`；未知容量沿用 80,000 估算 token 阈值。`auto_compact_ratio` 允许 0.5–0.95，`keep_recent_rounds` 允许 0–10，非法值回退到 0.7/2。两步推理路径同时考虑相关模型预算。单条资料过大且压缩无法解决时，保留问题并返回明确提示，不静默丢弃资料再发请求。

### 压缩、原始记录与有效历史

输入 `/` 展开命令菜单：`/compact` 手动压缩，`/new` 新建独立会话，`/usage` 打开用量详情；支持过滤、上下键、Enter、Esc 和鼠标。命令在本地执行，不作为聊天问题发送。

压缩按完整问答选区，默认保留最近两轮原文及未获回复的问题；短会话手动压缩可覆盖全部已完成问答。请求复用原 system 与完整历史前缀，在末尾追加科研交接指令，摘要合并此前有效摘要。结构包含研究目标、依据与引用、约束、已完成工作、待办、关键数据，要求保留 DOI、数值单位和验证状态。

压缩与聊天由会话 `request_lock` 及界面运行所有权串行化。提交前重校验选区与取消状态，拒绝空摘要、截断响应、过期选区及未减少估算占用的结果；失败或停止保留原上下文。手动压缩属于维护操作，运行状态与研究目标分开存储，不以 `/compact` 覆盖上一轮目标。

成功后 UI 展示折叠的压缩记录，可展开阅读摘要。模型只接收最新有效摘要和未压缩消息；原始消息与历次摘要继续归档，不无限累加旧摘要到模型输入。面板初始展示最近 30 轮，可加载更早历史。`events.jsonl` 追加事件并 flush/fsync 后更新 JSON 投影，`conversation.json` 缺失时可从日志重建；旧版本已经丢失的原文无法补回，会标记历史不完整。摘要仍是有损概括，保存原文不等于模型主动重读全部原文。

## 用量与缓存诊断

`llm_usage.py` 对各服务商 usage 归一化，增量迁移旧账本保留已有计数。仅保存 token、任务、功能来源、模型、思考参数、状态、耗时及消息指纹，不保存提示词正文和密钥。缺失计数保持未知，中断请求未交付最终 usage 时不填零；账单以服务商为准。

主面板统计当前会话主聊天；详情按任务/模型查看精读、评分、推理、压缩等用量，并显示最近十次请求。缓存率为 `sum(cache_hit_tokens) / sum(cache_hit_tokens + cache_miss_tokens)`，只纳入两项均已报告的请求，不平均逐次百分比。升级前未记录的消耗无法追补，“首条本机记录”不代表服务端冷缓存。

**LLM 前缀缓存**由模型服务端管理，与数据源响应缓存、PDF 缓存和本地模型缓存不同。固定 system、追加历史、稳定资料快照与压缩指令放在末尾有利于复用前缀；新的资料首次输入、独立任务提示、模型切换、压缩和缓存回收仍可能降低命中率。精读或评分的独立请求不能假设复用主聊天缓存；预设的主聊天路径则复用稳定前缀，并通过 operation 标明来源。

指纹比较只诊断消息前缀变化，不能证明服务端缓存必然命中。合成重复长文本可能得到很高比例，不代表用户混合工作负载；项目不承诺固定缓存率。优化须同时核对输入总量、输出、请求次数、延迟与任务效果，不能用冗余提示词或降低思考质量换漂亮百分比。

## 存储、配置与迁移

| 位置 | 内容与注意事项 |
| --- | --- |
| 项目根目录 `paperpilot.db` | 课题、论文、关联、阅读状态、评分和笔记；数据库位置不随启动 CWD 改变 |
| `config.yaml` | 本地配置及明文凭据；不提交。值整体写为 `${环境变量名}` 时从环境读取，变量未定义则为空字符串 |
| `repository/课题目录/` | `catalog.json`、`pdfs/`、会话及迁移备份；目录名经规范化，同名/目录冲突需处理，不能静默覆盖 |
| `repository/.recycle/` | 回收的课题目录/PDF；启动时清理达到默认七天保留期的内容，不是常驻定时器 |
| `outputs/deep_read/` | 精读结构化 JSON |
| `outputs/llm_usage.sqlite3` | 用量账本，含任务和会话标识，独立于论文数据库 |
| `cache/api/` | 数据源响应示例 TTL 为 24 小时；OpenAlex 引用/关键词事实缓存至少 30 天，本地提取关键词缓存不设过期；根目录及响应 TTL 可配置 |
| `cache/pdfs/`、`cache/cache_index.json` | 项目管理的下载缓存及索引 |
| 用户主目录 `.paperpilot_*` | PDF/HTML 下载、PDF.js、ECharts 和图谱窗口数据缓存，具体用途见用户指南 |
| 用户主目录 `.cache/` | CE/关键词等模型缓存，可能与其他应用共享 |

完整备份在关闭应用及其他写入进程后复制数据库、`repository/`、`outputs/` 和 `config.yaml`，妥善保护凭据。运行中的 SQLite 应使用备份接口，不能只复制主文件。不要只备份会话投影而漏掉 `events.jsonl` 和索引。课题重命名须同步数据库、目录与会话绑定；目录移动失败时保留记录并提示。

新增 ORM 字段考虑旧 SQLite 的增量迁移与回滚，禁止以清空用户库解决不兼容。新增配置同步示例及用户说明，提供合理缺省值。旧 `deepseek:` 在未配置 `llm.provider` 时仍兼容；设置页保存各家 Key 到 `llm.api_keys`，不是加密保险库。

### 当前配置基线

以下取自 [config.example.yaml](config.example.yaml)，不是已有用户配置的强制覆盖值。

| 项目 | 示例值 | 缺省/运行约束 |
| --- | --- | --- |
| 数据源 | OpenAlex 开；arXiv、Europe PMC 关；本地 PDF 开 | 启用开关决定参与检索的源 |
| 每源召回 / 展示 / CE 候选 | `100 / 20 / 20` | 未配置 UI 值为 `250 / 50 / 100`；滑块范围分别 `100–500 / 10–200 / 10–200` |
| 页数 / 请求超时 / 精确结果上限 | `5 / 15 秒 / 25` | 分别限制 `1–20 / 1–120 / 0–100`；精确上限 0 时校验后不联网 |
| 服务商 / 主模型 | `deepseek / deepseek-flash` | 内置模型清单是候选，实际可用性由账户与端点决定 |
| 用量 / 自动压缩比例 / 保留轮数 | `true / 0.7 / 2` | 容量覆盖为 `{}`；未知模型不猜容量 |
| 主题 / 深色 | `slate / true` | 六套主题定义于 `pages/context.py` |

桌面当前在 `app.py` 初始化为 1200×750；示例的 `ui.window_width/window_height/title` 尚未接入该入口，修改这些字段不会改变启动窗口。已使用字段与预留字段应区分。当前验证基线为 Python 3.13.5、Flet/flet-desktop 0.85.1；`requirements.txt` 中 Flet 的历史宽下限不证明旧 Flet 可运行当前界面。

## 图谱与阅读器

图谱提供引用、关键词共现及时间线三视图，支持全课题或所选论文。引用优先本地缓存，缺失时按 DOI 从 OpenAlex 批量补查；只展示课题内的引用边。无 DOI 的本地 PDF 仍可参与共现和时间线。断网、限流及缓存过期可能使引用不完整，不能据此断言文献不存在关系。

PDF 阅读器通过 pywebview 独立窗口加载 PDF.js；图谱通过 ECharts。引擎首次使用可能下载，完整引擎及所需数据/模型缓存就绪后才具备相应离线能力。已有元数据和本地 PDF 管理不要求云端 LLM；学术源检索需要网络，AI 功能需要配置的远程或本地服务。

## 设计参考与验证入口

上下文、存储及停止设计借鉴已研读的固定版本，按科研桌面业务调整；不是照搬其他项目的阈值或宣称其整个实现已被移植。

- [Codex 压缩实现](https://github.com/openai/codex/blob/8d44977aa2fb9ae1b128660668dc5b36966613fa/codex-rs/core/src/compact.rs)、[压缩检查点](https://github.com/openai/codex/blob/8d44977aa2fb9ae1b128660668dc5b36966613fa/codex-rs/history/src/compaction_checkpoint.rs)、[slash 输入](https://github.com/openai/codex/blob/8d44977aa2fb9ae1b128660668dc5b36966613fa/codex-rs/tui/src/bottom_pane/chat_composer/slash_input.rs)。
- [DeepSeek Harness 压缩子系统](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/docs/subsystems/compaction.md)、[摘要器](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/packages/compaction/compaction-basic/src/summarizer.ts)、[折叠记录](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/packages/client/ui-chat/src/client/chat/CompactionItem.tsx)。
- [DeepSeek 缓存说明](https://api-docs.deepseek.com/guides/kv_cache)、[上下文容量来源](https://api-docs.deepseek.com/quick_start/pricing)。服务商的实时限制与政策仍须按当前账户和响应确认。

自动化、原生 UI 和付费实测各有独立范围，入口见 [TESTING.md](TESTING.md)。历史 evidence 仅证明当时脚本覆盖的场景，不替代当前代码复验或全供应商验收。
