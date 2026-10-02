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

早期阶段报告及配套 Word/PPT 是历史资料。其旧接口、排序方案、协作分工和测试数字不作为现行规范。近期专项研究与实施报告中的有效设计已归入长期文档，原始验证计数和截图只保存在本地 `validation_evidence/`，该目录由 `.gitignore` 排除，不随仓库发布。

## 项目定位与主流程

PaperPilot 面向学生及科研人员的课题研究，以 Python + Flet 桌面应用整合课题分析、文献检索、排序、归档、阅读和学术对话。数据存于本机，当前没有账号系统、共享服务器或后台定时推送服务。

主要业务链路为：课题描述 → 分层关键词 → 启用的数据源召回 → 去重与排序 → 保存课题文献库 → 获取原文 → AI 精读、评分或对话 → 导出、图谱。DOI、arXiv ID、完整标题的精确查找另走严格匹配路径；本地 PDF 可直接导入文献库。

| 层次 | 入口与职责 |
| --- | --- |
| 应用装配 | `app.py` 初始化配置、数据库、窗口及页面回调，启动回收站清理 |
| 页面 | `pages/search_page.py`、`library_page.py`、`settings_page.py`、`sidebar.py`；`agent_panel.py` 管理聊天、会话、命令、用量及运行状态 |
| UI 共享 | `pages/context.py` 集中设计令牌、主题、AppState/AppContext 与回调注册；`components.py` 提供复用控件 |
| 检索 | `keywords.py`、`mt_translator.py` 提取和翻译；`search_service.py` 调度多源，`search_metrics.py` 记录阶段耗时；`fetcher.py` 级联召回；`sources/` 适配三源；`search_filters.py`、`exact_search.py` 管理严格筛选和精确匹配 |
| 排序与归档 | `indexer.py` 精排；`library.py`、`models.py` 管理 SQLite；`repo_manager.py` 管理课题目录、PDF、缓存与回收站；`local_import.py`、`export.py` 导入导出 |
| 模型与 Agent | `llm_client.py` 适配服务商；`ai_service.py` 编排精读、评分及聊天；`agent_team.py` 管理并行只读子 Agent；`agent_sessions.py` 管理独立会话；`conversation.py` 管理事件、历史和压缩；`context_budget.py` 管理预算；`agent_runtime.py` 管理运行及取消；`llm_usage.py` 记录计数 |
| 原文与图谱 | `downloader.py` 获取 PDF/HTML；`pdf_viewer.py` 通过 pywebview + PDF.js 阅读；`graph_service.py` 构图；`graph_window.py` 通过 ECharts 展示 |

页面通过 `pages.context.ctx` 使用共享状态和注入回调，不反向导入 `app.py`。业务层保留同步公共接口，耗时工作由页面调度到后台；运行状态随任务链延续，避免模型输出结束后工具仍在执行而界面误判为空闲。

## 检索、身份与评分

### 课题检索

- 关键词分为主、副、普通三层。LLM 提取失败或未配置时，中文回退到 jieba TF-IDF，MiniLM 可选增强；英文回退依赖 KeyBERT 及其模型。
- arXiv、OpenAlex、Europe PMC 按启用情况参与召回。多主词分别召回并合并；级联策略在不放宽用户筛选的前提下调整关键词组合。
- 课题检索默认在不同源之间并行（最多三个源任务），单源内的级联、主词查询、描述召回、游标分页和摘要补齐仍顺序执行。全部源完成后按 arXiv → OpenAlex → Europe PMC 汇总、去重并精排一次，不按线程完成次序选择重复项。线程复制运行/取消、用量与计时上下文，各源拥有独立错误列表及 HTTP Session；已有请求排空后才结束取消中的源任务。精确查找路径不因这次优化改变调度方式。
- 描述召回策略保持原语义：无元数据筛选时，关键词与描述两路各自使用 `max_results`，总候选可能超过滑块数字；筛选时仅在该源数量不足目标时补取描述结果。未削减召回数量、CE 候选或排序权重。去重保留原 0.9 阈值、比较方向及首次保留规则，用 `SequenceMatcher.quick_ratio()` 上界预筛和第二序列索引复用减少计算。
- 中文描述与三组关键词合并翻译，重复术语只翻译一次。仅成功结果进入有界内存缓存（最多 512 项），按服务商、端点、模型与原文隔离，缺省有效期一小时；配置未就绪及失败不缓存。长描述按长度预留翻译输出预算。
- OpenAlex 普通检索分页每页最多 100 条。该源各路召回结束后，摘要按唯一 W-ID、每批最多 100 个从 `api.openalex.org/works` 补查，复用源会话并携带已配置的 API Key。旧正摘要缓存仍可读；明确返回 `abstract_inverted_index: null` 时短期缓存缺失（缺省一小时），网络/限流、漏返回 ID、漏字段及畸形数据不缓存为缺失。摘要缺失不等于论文不存在，补查失败保留已召回论文并提示。
- arXiv 继续使用成熟的 `arxiv` SDK 解析和顺序分页，400 条用单页完成。课题检索的实际 HTTP 请求共用进程级、可取消的 3 秒频控锁，跨同时检索也不绕过限制；源任务内复用连接。取消 SDK 默认等待与外层重复重试，仅由 SDK 对失败页重试一次；HTTP 403 直接结束。逐条收集保留分页失败前的结果，失败/取消/畸形 feed 不写查询缓存。仅成功非空查询按数量和严格筛选条件隔离缓存，TTL 沿用 `cache.ttl_hours`；空结果不作为长期缺失缓存。[分页与频率依据](https://info.arxiv.org/help/api/user-manual.html#3112-start-and-max_results-paging)
- Europe PMC 400 条仍为一次 `resultType=core` 请求，保留完整摘要与被引排序。成功普通查询缓存已解析的公共记录，省去未使用的作者机构等大字段，兼容读取旧原始列表缓存。原生严格筛选游标分页保持独立。普通查询遵循 `search.request_timeout`；403 不重试、429/网络失败最多三次尝试且最后一次不再空等；失败及无效响应不缓存，并通知编排层停止该源后续级联/描述召回。OpenAlex 普通查询也遵循传入超时。
- 年份区间、单个作者姓名短语、完整期刊名按 AND 组合，尽量下推到各源原生查询，并在返回结果上核验；缺失元数据不能当作满足筛选。分页、超时与候选上限控制请求，数量不足须显示原因。
- 当前排序为 **API 分数粗筛 → Cross-Encoder 精排 → 关键词加分 → 短摘要降权**，主流程不使用 FAISS。CE 模型是 `mixedbread-ai/mxbai-rerank-base-v2`，优先本地缓存，加载或预测失败时退回 API 分数基础排序，后续修正仍生效。
- CE 继续使用 CPU/float32/512 tokens，缺省空闲 300 秒后释放；搜索与文献库排序可复用同一模型。加载只启动一个 future，等待加载不长期持有模型状态锁，因此另一排序任务能共享加载并及时响应取消。推理串行并持有使用计数，取消/超时后仍运行的预测结束前不会卸载。`release_cross_encoder()` 安排空闲释放，`unload_cross_encoder()` 保持立即请求释放的兼容入口，遇到在途预测则延迟到其结束。
- `[SearchTiming]` 按 run ID 记录翻译、各源召回/摘要、HTTP、去重、CE 加载/预测、排序及 UI 提交刷新边界，并统计请求与缓存命中；不记录查询、URL 或凭据。源耗时可能重叠，不得求和当作总耗时；`ui_submitted` 表示调用 Flet 更新完成，不表示已测量屏幕像素绘制。

实现依据：[OpenAlex 分页](https://help.openalex.org/api/paging)、[批量 ID 查询](https://help.openalex.org/how-to/api-recipes)、[Requests Session](https://requests.readthedocs.io/en/latest/user/advanced/#session-objects)、[SequenceMatcher 的缓存与上界](https://docs.python.org/3/library/difflib.html#sequencematcher-objects)。实际数据源行为与计时以本次验证记录为准。
- API 分、CE/关键词排序分及 AI 四维评分不是同一尺度，不应混写为同一种“相关性分”。AI 评分基于摘要，独立保存；不得无需求改动权重。

### 精确查找与文献身份

精确查找支持 DOI、arXiv ID（含明确版本）和完整标题，不支持 ISBN。标题只进行既定 Unicode、空白和末尾句号规范化后比较；不以模糊相似度冒充精确匹配。该路径不调用 CE，内部占位分数在 UI 显示为“精确”。

精确查找与文献库入库优先使用 DOI、arXiv ID、OpenAlex ID、稳定 URL，再考虑标题与年份等弱标识；不同 DOI 和明确不同的 arXiv 版本保守区分。课题检索汇总的 `fetcher.deduplicate()` 仍使用既有标题相似度规则（阈值 0.9、保留首次出现），本次只优化计算，不更改身份判重策略。

保存检索结果只保存当前展示的排序结果。`save_papers_to_project(...)` 的两个返回计数分别为新增课题关联数、补填 PDF 路径数，第二项不是下载成功数。后续 PDF 下载独立执行，下载不到不否定元数据已经入库。

下载器复用精确查找的 arXiv 编号规范化，兼容现代及旧式编号；论文带正式出版商 DOI 时仍检查 arXiv URL，PDF/HTML 下载不改写 DOI 或 URL。普通出版商 DOI 中的数字不作为 arXiv 编号。下载延续既有最新版本路径；旧论文能否取得 HTML 仍由 arXiv 的实际可用性决定。

## AI 服务与调用契约

### 统一模型适配

支持 `deepseek / openai / anthropic / glm / kimi / qwen / ollama`。除 Anthropic SDK 外，其余走 OpenAI 兼容 SDK；支持不等于每家最新模型、思考参数及代理端点均已实测。

`get_client(task=None)` 每次调用读取配置并解析任务模型。`score_model`、`chat_model` 是同一 provider 下的覆盖；非空 `reasoning_model` 启用先推理再回答的聊天流程。关键词提取、翻译及未覆盖任务使用主模型。`thinking=None` 表示不显式传参、采用模型默认值；`True/False` 经适配层转换，不能把“默认”当作关闭推理。运行中的请求继续使用创建时的 client，新配置作用于后续调用。

`ChatResult` 包含 `content`、`reasoning`、`usage`、`provider`、`model`、`request_id`、`elapsed_ms`、`first_token_ms`、`finish_reason`，新增 `tool_calls/provider_blocks` 保存原生工具及供应商连续性字段。字段可能为空；推理 token 若已计入服务商输出计数，不再重复相加。`LLMClient.chat(...)` 返回该对象，`chat_stream(...)` 逐段返回正文。Agent 使用 `chat` 的可取消传输；独立 `chat_stream` 接口不承担团队工具编排。

`tools_scope(...)` 通过 ContextVar 限定当前请求工具，不修改共享 client 或原有公开调用签名。OpenAI 兼容请求传入 function tools，SSE 拼接各 index 的工具参数；Anthropic 转换 `tool_use/tool_result`，保留返回块及思考签名。指纹包含工具定义但只存摘要。团队需要端点支持原生函数调用，可通过 `agent.team.enabled: false` 关闭；协议适配不等于所有服务商均已实测。

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
| `ai_service.py` | `AIService.deep_read(paper, full_text=None)`；`score_papers(topic_desc, papers, max_papers=50)`；`chat(project_id, project_name, message, ..., session_id=..., include_library_context=..., operation=..., attachments=..., on_team_change=...)` 返回 `reply/compressed/session_id` |
| `ai_service.py` | `create_session(...)`、`select_session(...)`、`get_conversation(..., session_id=...)`；`get_context_status(..., session_id=..., draft=...)`；`compact_context(..., session_id=...)` 返回完成、未变化或失败状态 |
| `graph_service.py` | `build_graph_data(project_id, papers, on_progress=None)` 为图谱窗口提供节点与关系 |
| `pages/agent_panel.py` | `send_agent_message(text, role='user')`、`set_agent_project(project_id, project_name='', topic_desc='')`、`begin_agent_run(...)`、`stop_agent_run(...)` |

## Agent 会话、停止与恢复

StudyCopilot 是课题感知聊天、只读科研 Agent Team 与已有应用功能编排器。通过最终主回复中的动作标记及面板解析调度检索、保存、评分、精读与课题更新等功能；团队工具不授予通用代码执行或任意外部工具权限。

### 独立会话与资料注入

同一课题可创建、切换、重命名多个独立聊天。会话共享文献库，各自保存历史、摘要和运行状态；`sessions/index.json` 保存列表与最近活动会话，UUID 标识会话。旧课题单份 `conversation.json` 迁移为“历史对话”，保留旧文件及 `legacy-conversation.json` 备份；格式异常时保留原件并报错，不自动覆盖为空记录。

聊天固定 system 提供稳定的身份及动作约定，课题变化以独立消息记录。选中文献、标题引用与研究现状等预设提供论文资料。预设仅使用标题及摘要节选，不默认注入全部全文；同一会话中未变化的资料复用已有快照，资料变化或被压缩移出有效上下文时再注入。覆盖篇数和截取范围要明确。

会话及课题身份在任务开始时固定，迟到结果仍归属原会话。UI 不将迟到回复错误展示到新切换的聊天。

### 本地附件

`pages/agent_attachments_ui.py` 管理加号菜单、原生 `FilePicker`、预览及按会话隔离的内存草稿；`paperpilot/agent_attachments.py` 负责限额、成熟文档解析器、不可变资产及模型消息投影。选择时快照文件字节，发送前不调用模型；发送时先原子保存到 `sessions/{id}/attachments/{sha256}`，再记录运行与消息事件。JSON/日志只保存相对资产引用、元数据和文本摘录，不保存 base64 或依赖原文件路径。

文本、PDF、DOCX、PPTX、XLSX 读取有界摘录；扫描 PDF 只渲染前 3 页，限制写入消息并在预览中显示。文件夹不跟随符号链接/目录联接，跳过隐藏、构建及缓存目录，只读取显式选择目录内支持的资料；批次超限或读取失败不部分替换草稿。每消息最多 20 个文件、原文件合计 40 MiB、单文件 20 MiB；图片最多 8 张、单张 8 MiB、合计 12 MiB。请求消息 JSON 限制 32 MiB，超限提示压缩、减量或新会话，不静默删除附件。

图片只在 user 消息中投影为 OpenAI `image_url` 数据 URL；Anthropic 适配器转换为 `image/source`，请求时校验资产摘要、字节数、格式和尺寸。DeepSeek Flash 的图片能力已按官方文档内置，其他模型通过 `agent.image_support` 明确声明；两步推理的两个模型均须支持图片。估算不计算 base64 字符，采用每图 4096 token 的保守跨服务商预留，收到真实 usage 后校准仪表。附件内提示和操作标记视为研究资料，不能扩大用户授权。

停止或重启恢复发生在消息落盘前时，从运行元数据恢复已接收附件；压缩仅改变有效模型历史，原事件及资产继续归档，历史气泡仍可预览。重命名移动整个课题目录，引用随会话目录保持有效。删除课题沿用原回收站机制；备份必须包含附件目录。未发送附件只在内存中保留，退出后不恢复。

### 科研 Agent Team：派发、权限与审查

`agent_team.py` 提供独立模型循环，`pages/agent_team_ui.py` 在右侧展示团队状态、历史及完整子对话。`_active_run` 仍限制同时活动的主轮次；子 Agent 不获取主聊天 `request_lock`，使用独立历史、client、取消令牌和 usage 作用域，通过有界 ThreadPoolExecutor 同时工作。

1. `MAIN_TEAM_PROMPT` 要求只拆可并行的任务，明确目标、资料范围及验收要求。`team_dispatch({tasks: [...]})` 派发，或以 `agent_id` 同轮追问；多个原生派发合为一批，整体校验再执行。可选空标识规范化，不接受未知字段、重复任务或跨团队目标。完整 `[TEAM]...[/TEAM]` 仅作旧文本响应兼容，不解析正文中引用的示例。
2. `WORKER_PROMPT` 限定只读、禁止递归派发和项目修改。子上下文含角色、资料目录及任务；资料是主聊天当前有效上下文的快照，目录展示最近24项及限制，不复制其它子 Agent 推断。`read_source` 每次最多6000字符，每项任务最多四次，图片按需提供。仅有摘要时必须说明未读全文，不能声称联网。
3. 主子均提供受限 AST `calculate`：无 eval、变量、属性、任意代码或文件权限；子每次任务最多六次计算，主每轮十二次。保存表达式及值，验证算术，不证明统计假设、因果或研究结论。模型可能忽略单次工具调用偏好，因此支持有界多调用并逐 ID 返回结果；图片资料在全部工具结果之后提供。
4. 每批观察返回真实状态、读取位置、计算记录、错误及每名最多5000字符结果；超出标注截断，完整回复继续保存。主模型对照原问题核验证据、冲突、数字及缺口，必要时追问，再凝练输出。失败/停止不算完成，两名 Agent 一致不等于事实正确。只有最终主回复进入 ACTION/PROJECT_UPDATE 路由，子操作标记仅为数据；主修改沿用原有确认机制。

缺省同时三名、本轮六名、最多三批（含追问），每项任务总超时180秒。任务排队可见，超时从开始执行时计。子模型继承聊天模型，读取/审查阶段显式关闭附加思考。当前不提供跨服务商路由、子间通信、共享任务板或递归团队。简单问答直接回答，团队会增加请求、token及延迟；缓存率不是团队质量验收标准。

### 团队保存、停止、回收与 UI

`sessions/{session_id}/teams/{parent_run_id}/team.json` 原子保存目标、父子身份、状态、任务、完整文本对话、读取/计算记录、结果、错误及回收标记。子日志使用图片占位和工具 ID，不存 base64；已发送图片仍由主会话不可变附件保存。主事件日志保留内部工具交换及观察，普通气泡隐藏这些中间消息，轮次与压缩边界按实际用户问答计。

子状态有 queued/running/stopping/completed/failed/timed_out/cancelled/interrupted，主另有 reviewing。右侧名单显示各自状态；点击查看完整只读子对话，可仅停止一个子 Agent。输入框停止按钮取消整个当前轮并传播到所有子任务。网络关闭及检查点阻止后续动作，未回答的原生工具 ID 补入明确的中断结果，避免继续时留下无效工具链；已完成结果与未完成片段分别保留。

主审查结束或当前轮失败/停止后关闭线程池、解除取消订阅、移除 live 注册表，保留原始团队记录。回收和完成是不同状态。同轮追问复用子历史；跨轮旧标识不可投递，发送“继续”携带主目标及记录，需要时新建同职责子 Agent。重启将无 live 所有者的未结束团队标记 interrupted，不自动重执行；压缩不删除团队原始记录，压缩请求单独关闭团队工具。历史入口加载最近30个团队，较早记录仍在会话目录中。SQLite无新增业务字段，旧会话无团队记录仍可使用。

### 实读源码和报告的设计依据（2026-10-02）

按科研只读业务化用，不声称复刻完整平台或读取未公开的 Codex 桌面 UI 源码。

| 来源与快照 | 研读入口及机制 | 本项目采用 |
| --- | --- | --- |
| [Codex，14a477ea](https://github.com/openai/codex/tree/14a477ea89712071944244022e8a10142845456e) | `codex-rs/core/src/tools/handlers/multi_agents_spec.rs`、`agent/control/spawn.rs`、`interrupt.rs`、`templates/collab/experimental_prompt.md`：派发、独立/分叉上下文、父子所有权、追问、停止及关闭 | 独立子历史、父轮归属、结果审查、停止保留记录及资源回收；[官方文档](https://developers.openai.com/codex/multi-agent) 补充操作原则 |
| [DeepSeek Harness，639ed015](https://github.com/deepseek-ai/deepseek-harness/tree/639ed015397290b3745d163aafe02ffee4aa3f84) | `packages/experimental/tool-agent-team/src/index.ts`、`agent-team/src/lifecycle.ts`、`mailbox.ts`、`client-ui-agent-team/src/client/TeamAction.tsx`：lead-only派发、活动/任务状态分离、持久邮箱、取消和释放、子会话入口 | 主统一修改、终态与释放分开、持久身份、右端名单及子对话；本项目同轮批次汇合无需完整共享任务板/点对点邮箱 |
| [ZCode，29628c9a](https://github.com/zai-org/ZCode/tree/29628c9acdb81b703bbd4080c207a0e7ce5e276e) | `apps/zcode-cli/packages/core/src/subagent/system-prompt.ts`、`runner.ts`、`packages/ui/src/app-shell/SubagentSessionSidePane.tsx`：子角色、独立会话、父信号/看门狗、终态通知、只读侧栏 | 分离角色提示、超时取消、完整只读子对话入口及主审查 |

[Anthropic 的多 Agent 研究系统报告](https://www.anthropic.com/engineering/multi-agent-research-system) 强调分工、明确来源/输出、适度并行及成本；[Towards a Science of Scaling Agent Systems，v3](https://arxiv.org/html/2512.08296v3) 分析可分解任务收益、协调开销与集中验证；[Why Do Multi-Agent LLM Systems Fail?，v3](https://arxiv.org/html/2503.13657v3) 区分系统设计、Agent间对齐及任务验证失败。这些结果支持有界分工和主审查，不证明任何科研任务一定受益。实测曾发现主 Agent 沿用子算术错误，故增加计算工具；该风险不能仅靠提示词或人数消除，验证边界见 [TESTING.md](TESTING.md)。

### 停止与恢复

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

占用按字符、消息封装及图片预留估算，最近主聊天的实际输入用量可作为基准，后续消息按估算增量修正；始终显示 `≈`，不是 tokenizer 精确计数。压缩提交或更换模型使旧基准失效，压缩请求的输入计数不作为压缩后聊天占用。草稿与待发附件另列，图片预留不是服务商账单计数。

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

完整备份在关闭应用及其他写入进程后复制数据库、`repository/`、`outputs/` 和 `config.yaml`，妥善保护凭据。运行中的 SQLite 应使用备份接口，不能只复制主文件。不要只备份会话投影而漏掉 `events.jsonl`、索引和会话 `attachments/`。课题重命名须同步数据库、目录与会话绑定；目录移动失败时保留记录并提示。

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
- 附件借鉴 [Codex 存储接口](https://github.com/openai/codex/blob/a75987455a2879ca151cea5e118fa307be868583/codex-rs/attachment-store/src/lib.rs)、[DeepSeek Harness 附件子系统](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/docs/subsystems/attachment.md)及[不可变文件存储](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/packages/attachment/attachment-local/src/file-store.ts)。图片协议与 Flash 能力见 [DeepSeek 官方视觉文档](https://api-docs.deepseek.com/guides/vision)。

自动化、原生 UI 和付费实测各有独立范围，入口见 [TESTING.md](TESTING.md)。历史 evidence 仅证明当时脚本覆盖的场景，不替代当前代码复验或全供应商验收。
