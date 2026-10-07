# PaperPilot 验证指南

> 版本：v2.1.0；核对日期：2026-10-07。本文维护验证方法和入口，历史日期和结果按当时记录保留，不代表本次重新运行。当前功能见 [ARCHITECTURE.md](ARCHITECTURE.md)，用户操作见 [USER_GUIDE.md](USER_GUIDE.md)。

## 验证原则

先确定受影响的真实用户操作与成功标准，再选择必要的单元、集成、桌面或外部服务检查。脚本通过只证明其覆盖范围，模型替身不证明回答质量，HTTP/SDK 模拟不证明服务商真实计费，直接调用 Python 回调不证明鼠标和键盘可用。

按全局 AGENTS 的强制规则，开工建立覆盖范围，选择不同任务、资料类型/规模、语言、状态及权限；正常路径之外验证适用的缺失/错误/超限、失败/超时、取消/恢复、并发归属与旧数据兼容。已有用例仅作为相关回归，不能反复同一例子代表新覆盖；重跑须由修复、变更或未解决风险驱动。验收分别记录替身、真实服务、原生 UI 的已验证、失败和未验证范围，测试数量不替代业务效果。

当前开发基线是 Windows、Python 3.13.5、Flet/flet-desktop 0.85.1。命令在项目根目录的 PowerShell 中执行，使用项目解释器。变更后的源码与当次运行输出优先于旧报告；其他平台、DPI、Python 或模型组合须各自核验。

## 工具与本地测试的区别

版本与文档变更先核对 `paperpilot.__version__`、窗口标题及导航底部，检查 Markdown 链接、示例配置、Word 包结构及同级排版。窗口实屏证据与控件树检查分别记录，不以静态字符串检查冒充真实 UI 验收；不因这类改动机械重跑付费模型或检索全链路。Word 项目文档是发布文档；早期阶段报告、答辩 PPT 和本地证据各自保持历史边界。

2026-10-07，v2.1.0 文档与版本同步验证：已核对 Python 语法、版本引用、Markdown 本地文件链接和示例 YAML；Word 包校验、微软雅黑及同级样式检查通过，LibreOffice 渲染为 13 页，检查了分页、文本边界与布局。隔离启动真实 `app.main`，核对窗口标题、导航版本及文献／设置／检索页重建；深色、浅色启动的自有窗口截图已检查。首次沙箱启动未产生有效 UI 证据；浅色探针初次未同步应用状态，纠正为实际浅色配置启动后复核。本轮没有请求真实学术源或付费模型，不新增科研质量验收结论。本地证据保存在 `validation_evidence/version_sync_20261007/`，不随发布提交。

同日，Word 项目说明按需求、功能和业务使用重新组织，补充 Agent 主业务链及分阶段开发职责，修订后渲染为 12 页。复核了功能说明与操作入口、微软雅黑及同级样式、文档包、各页文本边界和页脚间距，并检查了页面布局及 Agent／分工章节；本次只修改文档，未重复运行界面或付费服务。修订证据保存在 `validation_evidence/word_business_20261007/`。

- `tools/` 包含隔离 runner、Agent 行为/原生输入验证与显式开启的真实 DeepSeek 探针。
- 项目根目录的 `test_*.py` 是本机保留的测试资产，当前 `.gitignore` 忽略这些文件；新克隆不能假设全部存在。运行前确认目标脚本，勿把缺文件当产品故障。
- `validation_evidence/` 只在本地保存计数、验收 JSON 和截图，已列入 `.gitignore`，不得提交或推送；新克隆不包含这些历史资料。文件名日期标明快照，不表示当前代码重新验证；截图中的固定模拟缓存率不作服务商实测。

## 无真实模型凭据的隔离检查

AI 服务商及模型更新的业务验收：

```powershell
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_llm_providers.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_llm_settings_ui.py
```

`validate_llm_providers.py` 使用真实 OpenAI SDK 连接本机 HTTP/SSE，验证 Gemini 的科研精读/评分/对话、三领域中英文输入、GPT-6.1 Sol Responses、两家的中英文课题关键词提取/分组翻译/缓存、短输出思考预算及显式禁用预留、原生工具计算及签名/加密项回传、token/cache 计数、截断、认证失败、开始前/流中取消及后续恢复、旧配置/自定义模型/任务覆盖、本地与云端 Ollama 的密钥边界、容量及图片能力覆盖。返回内容和图片载荷是 fixture，不证明真实模型推理或视觉质量。`validate_llm_settings_ui.py` 启动真实 `app.main`，对生产设置控件调用回调，验证八项接入方式的模型选项、Gemini/OpenAI SDK 连通结果、密钥切换与保存、自定义 ID、空远程 Key 拒绝及本地 Ollama。截取自有前台窗口的实屏像素并拒绝空白截图，不宣称物理鼠标/键盘覆盖。真实 API、账户权限及套餐扣费需单独验收；脚本要求经隔离 runner 启动。

Codex 订阅功能和专用脚本已移除。上述两个脚本补充验证旧 `provider: codex` 的停用及设置迁移：空/旧模型、旧密钥和任务覆盖不会触发调用或自动回退，原配置和历史用量保留；旧工具记录在 Gemini、OpenAI Responses 和 Claude 下继续时保留正文/工具 ID，排除旧传输标记，Claude 原生思考签名保持原样。原生设置可显示提示、拒绝未选择时的测试/保存，并在重新选择后使用该家的密钥与模型。历史订阅报告仅保留在本地 `validation_evidence/`，不作为当前功能的验收依据。

通过 runner 运行已有的非付费脚本：

```powershell
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_context.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_stop.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_team.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_attachments.py
```

`tools/run_validation.py` 从示例构造去凭据配置，不读取用户 `config.yaml`；数据库、课题仓库、输出、下载缓存及临时路径重定向到项目内 `.validation-search-20260922/目标脚本名/`。Python 写入守卫拒绝目标目录以外的写入；可读取已有本地模型缓存。隔离的是配置和数据写入，不自动禁止所有网络请求，缺模型时仍可能尝试下载。

可用 `PAPERPILOT_VALIDATION_SCRATCH` 指定项目内新的 `.validation-*` 目录。本 runner 会清理该目录下同名脚本的旧工作目录；应使用本次任务专用目录，不能指向需要保留的历史证据。

| 脚本 | 主要验证范围 |
| --- | --- |
| `tools/validate_agent_context.py` | 模型容量与估算/校准、自动预算、手动压缩、结构/缩减/选区检查、取消保留、原始历史、日志重建及继续 |
| `tools/validate_agent_stop.py` | 本地 HTTP 服务器 + 真实 SDK 的等待/流中取消、不重试、usage 缺失与完整流、取消检查点、目标恢复及结果归属 |
| `tools/validate_agent_team.py` | 方法对比/实验限制、双语资料、统计纠错、仪器标定、空间采样、缺失记录及测序资料等不同任务；并行读取与追问、批量原生工具及 ID 匹配、权限/限额、局部失败、父子停止/超时/回收、重启/压缩与会话归属。模型替身及真实 SDK 对本地 HTTP 服务，不调用付费模型 |
| `tools/validate_agent_attachments.py` | 文件、Office/PDF/扫描页及目录摘录、范围/限额、损坏或缺失资产、图片协议、停止/重启/压缩后的快照保留；模型使用替身，不验证真实视觉质量 |
| `tools/validate_agent_loop.py` | 长任务三档权限、多阶段依赖计划、实际文本文件与 SQLite 文献链路、三领域中英文及不同规模、审批拒绝/过期/取消、无累计上限与共享用量、旧额度解除及恢复、目标与原始证据压缩保真、重复/无进展提醒后继续、工具截断、验收失败后继续、服务配置改变、链接/junction/硬链接与会话归属；内部有界探针保留显式限额测试；检索、排序及模型使用替身 |
| `tools/validate_agent_interaction.py` | 无强制计划的能力问答/文本写入、探索后修订清单、取消事项而保留验收条件、具体审批理由与宿主批准记录、旧文件证据的有效反馈、问询选择/自由答案/多题完整性/无计时自动回答、稍后回答与重启确认、停止/重复/过期/追加要求、工具配对、原文和代码/路径保留、隐藏内部检查点；模型为替身 |
| `tools/validate_agent_loop_protocols.py` | 真实 SDK 对本地 HTTP/SSE，覆盖 DeepSeek/OpenAI Responses/Gemini/Claude/GLM/Kimi/Qwen/Ollama 的原生问询→明确人工答复→只读取证→零工具验收，不先建计划；工具调用/结果配对、连续性字段及共享计数；401/402/429 暂停、500 重试后继续；云端输出与人工答复为 fixture |
| 本地 `test_agent_sessions_usage.py` | 独立会话、迁移、资料快照、前缀与用量统计 |
| 本地 `test_llm_client.py` | 统一调用契约、Provider/Key/模型与思考参数 |
| 本地 `test_search_features.py`、`test_search_library_integration.py` | 筛选、分页、精确匹配、身份去重、保存及课题语义 |
| 本地 `test_unit_core.py`、`test_db_path.py`、`test_local_import.py` | 核心数据行为、数据库路径及 PDF 导入 |
| 本地 `test_graph_service.py`、`test_graph_window.py` | 图谱数据与窗口相关行为 |

本地脚本存在时，例如：

```powershell
.\.venv\Scripts\python.exe -B tools/run_validation.py test_agent_sessions_usage.py
```

无需为文档编辑或低风险改动机械执行所有脚本。先读脚本的调用与退出语义，按改动选择覆盖；实际数据源、模型、窗口及付费脚本不因文件名包含 `test` 就被当作离线检查。

## 长任务业务与恢复验收

源码设计和固定上游提交见 [AGENT_LOOP_DESIGN.md](AGENT_LOOP_DESIGN.md)。以下入口分别提供业务、交互状态、SDK 协议和原生桌面证据，不能相互替代：

```powershell
$env:PAPERPILOT_VALIDATION_SCRATCH = '.validation-loop-local'
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_loop.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_interaction.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_loop_protocols.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_loop_ui.py
```

`validate_agent_loop.py` 先跑通库内分析 → 分批取证 → 创建报告 → 逐项验收，以及检索 → 精排 → AI 打分 → 经审批入库链路；另用实际 CSV → 有界计算 → 报告验证多阶段依赖。光子学、肿瘤纵向队列和城市洪涝的中英文资料与 3/35/180 篇规模区分覆盖。附件覆盖不可变 Markdown 及 PNG：源文件删除、实际压缩后原摘录重读、主模型与独立验收的原生图片投影、磁盘无 base64；图片内容为 fixture，不证明真实视觉质量，也不重做 PDF/Office 解析的视觉验收。批次无效时整批不修改，失败/未知用量及停止后记录可以重建；当前 SDK、团队、压缩及停止相关回归按调用层改动执行。

另覆盖 Windows 超过 260 字符的工作文件、任务响应/证据/数据集及隔离目录清理，重规划后重新检查依赖、隐藏团队工具不能绕过禁用、损坏检查点保留、未进入循环即退出后的确认恢复，以及实际课题改名后的暂停/完成记录迁移、运行中团队取消与 owner 回收。

单个用户目标经过多次工具执行和多次真实会话压缩的场景，核对原目标及计划重注入、完整原始历史、工具调用/结果配对与最终产物。该用例人为降低压缩触发阈值以制造压力；验收资料本身仍受单次模型窗口限制，超限应暂停，不能通过截掉证据取得通过。

`validate_agent_loop_ui.py` 启动真实 `app.main`，操作生产控件回调，覆盖勾选后直接发送、无启动/预算表单、输入区三档权限、拒绝后不写文件、点击「继续任务」确认恢复并解除旧额度、单次批准后创建报告及完成状态、长中英文目标完整保留、文本附件直接发送、启动失败保留消息及附件草稿、直接修改、详情和独立聊天。新增对话内审批理由/折叠差异、问询选项+自由回复、空答案拒绝、输入区回复、主题切换保留草稿、320 宽待答卡片及差异展开后按钮可见。保存自有前台窗口的真实像素；焦点不可用时尝试自有窗口渲染并标明方式，空白截图明确标为不可用，不以空图当视觉通过。模型是替身，**未模拟物理鼠标/键盘输入**，不据此宣称所有快捷键或付费模型已验收。主题切换截图只验收右侧面板，探针没有重建其他页面。

用量摘要恢复到上方后的原生验证，使用隔离 SQLite 账本写入不同大小的主聊天请求、缺缓存计数/缺用量请求及独立评分请求，核对可见的输入 16000、输出 1600、四次主聊天请求、加权缓存率 56.0% 和缺用量提示；评分请求不混入主聊天摘要。上方入口实际打开生产用量详情；新会话显示零请求和缓存暂无数据，深浅主题及 320 宽布局截图已检查。数值为明确的测试输入，不能当作服务商实测命中率；本轮没有付费请求。初次探针误从旧 `page.overlay` 查找对话框，修正为当前 Flet 对话框栈后复检通过，产品用量详情入口本身正常。

2026-10-04 审批/问询改动的验证：18 个新增交互业务场景、43 个长任务相关回归、14 个上下文回归、20 个只读团队回归通过；四个 SDK 测试方法覆盖八路本地传输与请求故障。交互场景另验证答案已落盘但尚未进入历史时的异常退出恢复，以及历史已写而状态未写时不重复回复。原生控件回调与八张有效前台截图通过，逐图检查了当前修改范围。首次检查发现两个旧测试断言仍要求全局修改前计划门禁，改为验证真实只读工具边界和清单依赖；新增测试的一处加载 API 写错，修正为现行会话构造入口。首次沙箱截图不可用，未计为视觉通过；真实桌面重检发现选中答案后残留空答案错误、主题边框、缩窄后的卡片滚动和短差异空白过大，修正后复检。文件执行记录及旧证据提示改动后重跑相关业务回归，未重复付费成功场景。SQLite 退出时仍有连接 `ResourceWarning`；不是无告警运行。

2026-10-04 交互修订：43 个业务场景、4 个 SDK 测试方法（含八路服务与请求故障）、14 个既有模型客户端回归，以及新的原生窗口直接启动/恢复链路通过。新增持续任务用例实际执行 112 次不同的有界算术工具，并用模拟用量和时长越过先前的 1800 秒、100 请求、1000000 token 阈值后完成；这不代表真实小时级或等额付费实测。重复读取、纯文本、状态空转及无效批次超过旧五轮限制后仍可调整并完成。先前报告中的预算弹窗、超限自动暂停是旧方案，当前生产界面和配置不再施加。

2026-10-04 本轮业务与八路 SDK 协议检查已通过；原生窗口回调、审批/恢复和有效截图已通过。业务测试退出时出现 SQLite 连接 `ResourceWarning`，未造成断言失败；不能视为资源无告警。真实外部检索的长任务端到端、小时级稳定性及全服务商科研回答质量仍未验证。文件边界是应用层版本/权限检查，不是操作系统隔离。真实服务验收应分别记录所选模型/资料、实际请求及用量、暂停恢复与产物审查结果，不能把历史团队实测当成本次长任务证据。

`tools/validate_agent_loop_live.py --live` 是显式开启的真实模型探针，读取当前聊天模型凭据至内存，配置/数据库/用量及工作文件写入任务专用隔离目录，不经去凭据 runner。每个目标限制 120 秒、25 请求、180,000 token，无自动重跑；`--case chinese` / `--case english` 可只复测失败场景。默认两例分别为中文合成观察队列的报告写入和英文光子学计数的只读分析；检查真实规划、取证、受限计算、文件边界与验收状态。证据为脚本输出的 `live-result.json`，整理时放入忽略提交的 `validation_evidence/`。

`--case interaction` 验证中文「修改前解释目的→明确批准→保存/重读报告」与英文「先问清无单位数据的解释→明确回复→计算均值」；`--case interaction_chinese` 仅复测前者。人工批准/答复由脚本明确提供，不能视为真实用户审查或原生输入证据。2026-10-04 DeepSeek V4 Flash：中文首测因报告修订后仍引用初稿记录被宿主拒绝，17 请求/151026 token/24.078 秒后触及探针预留上限，未完成。补充旧记录的有效替代提示及宿主审批证据，离线验证后仅复测中文：13 请求/93244 token/18.105 秒，报告实际 405 字符、21 行，SE≈0.0770177，明确审批及重读后通过完成验收。英文 9 请求/42147 token/11.143 秒，无任务清单、明确答复后计算均值≈123.333，未写文件，完成验收；未重复请求。三次尝试合计 39 请求/286417 token，失败证据同样保留。

人工检查确认本次完成场景的修改理由和问题可读、所需数值及正文换行正确，不含内部 UUID；仍不能声称任意模型稳定生成高质量选项或科研结论。英文模型给出的额外单位选项缺少资料依据，最终依据明确的无单位答复继续；中文首次报告含未要求的区间估计，不能据其声称科研质量通过。完成验收是有限证据检查，不代替主张、假设和方法的科研审查。本轮未做小时级持续运行、全部云端服务商或真实文献源端到端实测。

2026-10-04 DeepSeek V4 Flash：中文任务自主形成四阶段计划，13 次请求、100137 token、24.083 秒，实际写入/重读报告并算得 SE≈0.0770177；英文初测在第一次响应保存时触发 Windows 260 字符路径限制（1 次请求、2997 token），未完成。修复扩展长度路径并补充长路径业务验证后，仅复测英文：三阶段计划，9 次请求、45028 token、12.329 秒，只读边界保持，工具复算 0.9375、0.88 和差 0.0575，目标验收通过。两个完成场景只证明这些有界操作和数值；中文“600字”未按全部 Markdown 字符数验收，英文“聚合计数没有方差估计”的表述过宽，不能宣称科研质量全部通过。真实验收仍需人工审查主张与假设。

## 检索并行与性能验证

```powershell
$env:PAPERPILOT_VALIDATION_SCRATCH = '.validation-search-my-run'
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_search_performance.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_remaining_sources.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_search_workflow.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_search_originals.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_search_native.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_search_app.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_search_graph_native.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/benchmark_sources.py --live --count 400
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/benchmark_search.py --live-openalex --cached-model --count 400
```

`validate_search_performance.py` 用受控数据源与模型、本地真实 HTTP 服务检查多源重叠、固定合并顺序、单源依赖顺序、描述召回/严格筛选、错误与取消排空、同时检索的上下文归属、400 条分页/摘要批量请求、正/缺失缓存兼容与过期、异常不负缓存、双语翻译复用、旧去重算法等价，以及在途预测的模型释放。受控延迟比较仅证明调度收益，不代表真实源加速比例。

`validate_search_native.py` 启动隔离的 Flet 桌面窗口，通过限制窗口归属的 Win32 鼠标输入点击探针按钮，使用生产检索事件、后台流水线、轮询和结果控件；三源各 400 条替身数据、替身 CE，核对源重叠和 50 条结果，截图保存在本次工作目录。探针预填真实控件和扁平/分层关键词；不能把仅赋值业务状态当作用户已经输入。它不证明云端召回或模型排序质量。

`benchmark_search.py` 默认不执行：必须显式选择 `--live-openalex` 或 `--cached-model`。400 规模采用英文课题、关键词和描述两路召回，50 篇展示、100 个 CE 候选；不调用云端 LLM，模型只允许现有本地缓存，不下载。真实 API 使用隔离的无凭据配置，是否可用以实际响应为准；需要网络权限。脚本报告初次/复用缓存的阶段耗时和计数，随后用同批数据对比原去重算法的内容/顺序与耗时，并保存 `benchmark_report.json`。`--seed-cache <项目内隔离缓存目录>` 可复制此前真实请求的缓存用于离线复放，报告明确标记 `seeded_cache`；该模式的网络阶段不能当作新真实请求或冷网络数据。

`validate_remaining_sources.py` 经本地真实 HTTP 与 arXiv SDK 验证 400 条单页、完整摘要、缓存兼容/副本、跨查询连接复用、严格筛选、后页失败保留、取消不缓存、全局频控、超时透传、失败停止级联、403/429 与重试次数。`validate_search_workflow.py` 用机器人、肿瘤免疫、钙钛矿三领域中英文输入，真实三源适配器连接本地元数据服务，现有真实 CPU CE 每次精排 100 候选/取 50 条；继续走实际数据库、重复保存、库内重排、AI 评分/摘要与全文精读、Agent 对话/独立会话、导出、PDF 下载/归档/本地导入和缓存引用构图。LLM 使用真实 SDK 的 JSON/SSE 传输与受控回复，不测试云端回答质量；未安装 ReportLab 时用项目现有 PyMuPDF 构造文本 PDF。

`validate_search_app.py` 使用 `app.main` 构建全部原生页面；受限鼠标触发探针，进入生产 Agent 检索动作，再调用生产勾选/保存对话框/文献库 CE 回调，检查三源重叠、50 行、保存归属、模型复用与检索/库/设置页切换。默认源与 CE 是替身；显式 `--live-sources --cached-model` 改用真实公开 API 与现有真实 CE，不下载模型、不调用云端 LLM。保存后 PDF 自动下载在探针内禁用，下载/导入另由连续业务测试与原生阅读器验证；不得把探针回调等同于所有控件都经鼠标逐一操作。

`benchmark_sources.py --live` 对三个领域各源目标 400 条，分别记录首次与缓存召回（不含 CE/UI），中文课题使用人工指定英文术语，成功与失败均写报告。返回不足 400 必须报告真实数量；实际网络错误不伪装成缓存加速。原生阅读器/图谱的本地窗口回归另见本地 `test_native_regression.py`。

`validate_search_originals.py` 使用真实本地 HTTP，检查带正式 DOI 的现代/旧式 arXiv 链接经 PDF/HTML 下载、缓存、归档与数据库路径补填后保持原始身份；另检查普通出版商 DOI/外站链接不会被数字误识别。它不证明旧文献在 arXiv 上有 HTML，也不证明外部出版商可下载。连续业务测试可用 `--case-index 0/1/2` 聚焦重跑一个领域；已有三领域覆盖不因重跑同一路径重复计数。

`validate_search_graph_native.py` 从实际 SQLite 文献库及受控引用缓存调用生产构图服务，打开原生 ECharts 窗口，核对三节点、两引用边、三共现边、三视图、详情关闭与最小化恢复。使用现有引擎缓存，不联网下载；视图/详情通过原生页面 JS 事件调用，并非真实鼠标点击节点。旧窗口回归的单节点样本仅证明窗口与恢复行为，不能代替非空边的显示检查。

本机本次验收说明保存在 `validation_evidence/search_all_sources_20261002.md`，逐次证据保存在该本地目录的 `search_performance_20261002*.json/png` 与 `search_all_sources_20261002*.json/png`，不随仓库发布。未做付费云端中文翻译、AI 服务质量或所有服务商的完整验收；真实网络、本地受控 SDK、真实本地模型和原生替身界面须分别列明，不降低召回或 CE 候选换取更短数字。

## 桌面与原生输入验证

```powershell
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_stop_ui.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_context_ui.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_attachments_ui.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_team_ui.py
```

UI 脚本启动隔离 Flet 桌面窗口，以虚构课题/文献及模型替身验证界面。目标按钮的鼠标和键盘通过 Win32 输入及真实 Flutter/Flet 事件链发送；附件探针另在后端切换会话检查草稿隔离。`tools/agent_native_input.py` 对窗口标题、进程和焦点作保护，只向指定验收窗口及其原生选文件对话框输入。

`validate_agent_attachments.py` 为离线业务检查：文本/Office/PDF/扫描页解析、文件夹范围、超限与读取失败、图片协议与估算、原文件移除后恢复、停止及异常退出、压缩后保留原附件。UI 探针验证实际加号菜单、文件/照片/文件夹系统选择、预览关闭、移除、Enter/纯附件发送、停止继续及窄面板；模型使用替身，不能据此宣称服务商视觉已通过。

2026-10-02 附件改动的本轮验证：附件业务检查 7 项、上下文回归 14 项、停止回归 12 项通过；原生附件流程通过。DeepSeek Flash 合成图片+CSV 单请求正确读取颜色与数值（输入 1044、输出 9 token）。其他服务商未做真实视觉请求；这是有界合成样本验证，不代表任意文档/图片的理解质量或固定缓存比例。

Agent Team 的原生探针用光谱/标定与英文指标两种任务，验证真实行点击、完整子回复、仅停止一名成员、主审查、关闭按钮、独立聊天及320宽浅色面板。模型仍为替身；子图片只读投影在离线检查中验证，未做真实团队多模态请求。Anthropic 仅检查协议块/签名转换，没有真实请求；其他服务商未作团队验收。

当前探针坐标基于 Windows 150% 缩放及 1000×750 逻辑窗口，依赖可用的交互桌面。不同 DPI/窗口位置应先校准，不能在目标窗口未就绪时盲目发送坐标。Flet native client 必须安装或缓存可用。

需要覆盖的业务流程：

1. 新建、切换、重命名并重启恢复独立会话；保存/关闭按钮实际生效，迟到结果归属原会话。
2. Enter 发送、Shift+Enter 换行、工作中防重复发送，箭头与停止方块转换。
3. 模型生成及后续检索/评分/精读期间停止，保留片段与草稿，阻止未开始操作，继续携带保存的目标；取消后的迟到结果不入库。
4. 点击用量/上下文详情，保存当前模型容量覆盖，核对关闭按钮及深浅主题、窄面板。
5. 输入 `/` 展开与过滤命令，上下键/Enter/Esc 与鼠标可用，菜单变化后输入焦点保持。
6. 手动压缩、展开摘要、压缩后继续；取消压缩保留原上下文与目标，原始记录可加载。
7. 文件、照片与目录添加，发送前预览/移除及显示读取范围；会话草稿独立；停止、重启及压缩后附件快照保持可读。
8. 多成员真实重叠工作、排队/终态准确、点击完整子对话、局部停止与全轮停止、结果审查/纠错、回收后记录保留；新聊天和历史团队不串结果，原生工具中断后可以继续。

本地 `test_agent_sessions_ui.py` 等直接回调脚本可覆盖状态与集成逻辑，但其成功不能代替上述原生输入验证。检索、图谱、阅读器等其他页面按受影响链路补充验证，不以 Agent 探针代替整个应用验收。

## 真实 DeepSeek 请求：显式开启

以下脚本直接读取实际 LLM 配置，使用合成材料及独立会话/用量路径，不经去凭据 runner；默认不发请求，带 `--live` 才开启。它们保留生产配置、用户历史及生产用量账本，真实消耗仍会计入服务商账户。

| 脚本 | 成功路径调用数与用途 |
| --- | --- |
| `tools/validate_agent_context_live.py` | 强制最多一次有界压缩请求；检查六段摘要、合成 DOI/数值/未验证标记、原文保留与上下文缩减 |
| `tools/validate_agent_attachments_live.py` | 一次 DeepSeek Flash 原生图片+CSV 请求，最多 128 输出 token、不重试；核对颜色与文本数值，独立会话及用量路径 |
| `tools/validate_agent_team_live.py` | `--live` 用方法可比性和观察性证据两种合成任务；`--live --case numeric` 核验两名子 Agent 读取/复算及主 Agent 纠错。按实际模型派发/读取/审查循环产生多次请求，子每调用不重试，主调用沿用原失败重试策略；无固定请求数或缓存率承诺 |
| `tools/validate_agent_presets.py` | 三次聊天/研究预设调用，不重试；核对资料首次注入、复用及 operation/usage，要求单步 DeepSeek 路径 |
| `tools/validate_agent_cache.py` | 三次对照 + 三次稳定前缀调用；合成长历史实验，成功路径六次，稳定路径沿用调用层失败重试行为，异常时实际请求可能增加 |

示例命令，仅在已授权付费实测时运行：

```powershell
.\.venv\Scripts\python.exe -B tools/validate_agent_context_live.py --live
```

运行前核对服务商、任务模型、端点及脚本请求/输出预算，勿输出 `config.yaml` 或密钥。探针写入项目内专用 `.validation-agent-*` 目录及 `validation_evidence/`。合成样本通过不代表真实论文摘要保真或完整科研任务成功；冷请求及短样本不能据此判断缓存架构无效。

2026-10-02 团队实测：初次文本工具协议失败，修为原生工具；一次隔离脚本复用了失败会话，改为每任务新会话。原生方法对比与观察性证据两个任务均完成两名子 Agent 的读取和主汇总，但观察性任务存在算术错误，不能当作质量通过。加入受限计算后，针对该风险的复测还发现多原生调用及可选空字段被旧校验拒绝，已修复；最终数值场景11次真实 DeepSeek请求，输入20648、输出6002 token，命中13949输入 token（约67.6%）。两名子 Agent 与主计算均得到 SE≈0.07701769，最终纠正0.11347。分布近似、错误来源推测等科学判断仍须审查，不能据此宣称完整科研回答无误或生产缓存达到该比例。

真实数据源及整条研究流水线另行验证。根目录 `test_e2e_pipeline.py`、`test_validation_live.py` 等本地脚本需先检查凭据读取、用户数据写入、网络和费用行为，不作为默认批量入口。

## 如何解释证据与缓存率

上下文仪表反映当前有效聊天背景，用量账本记录反复发送产生的消耗，两者不能互相代替。缓存率按有命中/未命中计数的输入 token 加权，未知计数不填零；服务端保留策略决定最终命中。

比较改动前后应记录同类真实任务的 provider/model、功能来源、思考设置、输入/输出、缓存命中/未命中、请求次数、延迟及任务结果。会话年龄、首次资料注入、压缩、模型变化与辅助调用会改变比例；重复长文本实验不作为固定缓存率或生产最低比例承诺。

当前保留的 Agent 证据入口：

- `validation_evidence/agent_context_20261002_regression.json`、`agent_context_20261002_native.json`、`agent_context_20261002_live.json` 及同前缀截图。
- `validation_evidence/agent_attachments_20261002_native.json`、`agent_attachments_20261002_live.json` 及同前缀截图（仅在对应验证成功后生成/保留）。
- `validation_evidence/agent_team_20261002_native.json`、`agent_team_20261002_regression.json`、`agent_team_20261002_live.json`、`agent_team_20261002_numeric_live.json` 及同前缀截图；协议、会话复用和数值批次失败另存对应 `*_failure.json`，不得仅展示成功快照。
- `validation_evidence/agent_stop_native_20261001.json`、`agent_followup_native_result.json` 及对应截图。
- `validation_evidence/agent_cache_20261001_live.json`、`agent_presets_20261001_live.json` 的实际 token 计数。
- `validation_evidence/` 下的检索快照目录用于对应历史场景，不能用其旧结果推定本轮重新通过。

每次交付写清当次执行的命令、源码范围、实际结果、模型/数据是否真实及未验证项。若只改文档，可核对链接、命令路径、接口、配置默认值和历史/当前分类，不需重复付费或启动整个应用。

## 临时文件与清理

测试日志、数据库、脚本和缓存放项目工作区，不写 C 盘。结束后先确认归属本次运行，再删除不再需要的专用临时目录；不能批量清空未知 `.validation*` 目录、用户数据库、历史 evidence 或本地测试资产。保留必要的计数/截图作为可复核证据，避免生成持续堆积的一次性 Markdown 报告。
