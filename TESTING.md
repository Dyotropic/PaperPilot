# PaperPilot 验证指南

> 核对日期：2026-10-02。本文维护验证方法和入口，不汇总每轮“全部通过”的数字。当前功能见 [ARCHITECTURE.md](ARCHITECTURE.md)，用户操作见 [USER_GUIDE.md](USER_GUIDE.md)。

## 验证原则

先确定受影响的真实用户操作与成功标准，再选择必要的单元、集成、桌面或外部服务检查。脚本通过只证明其覆盖范围，模型替身不证明回答质量，HTTP/SDK 模拟不证明服务商真实计费，直接调用 Python 回调不证明鼠标和键盘可用。

按全局 AGENTS 的强制规则，开工建立覆盖范围，选择不同任务、资料类型/规模、语言、状态及权限；正常路径之外验证适用的缺失/错误/超限、失败/超时、取消/恢复、并发归属与旧数据兼容。已有用例仅作为相关回归，不能反复同一例子代表新覆盖；重跑须由修复、变更或未解决风险驱动。验收分别记录替身、真实服务、原生 UI 的已验证、失败和未验证范围，测试数量不替代业务效果。

当前开发基线是 Windows、Python 3.13.5、Flet/flet-desktop 0.85.1。命令在项目根目录的 PowerShell 中执行，使用项目解释器。变更后的源码与当次运行输出优先于旧报告；其他平台、DPI、Python 或模型组合须各自核验。

## 工具与本地测试的区别

- `tools/` 包含隔离 runner、Agent 行为/原生输入验证与显式开启的真实 DeepSeek 探针。
- 项目根目录的 `test_*.py` 是本机保留的测试资产，当前 `.gitignore` 忽略这些文件；新克隆不能假设全部存在。运行前确认目标脚本，勿把缺文件当产品故障。
- `validation_evidence/` 保存计数、验收 JSON 和截图。文件名日期标明快照，不表示当前代码重新验证；截图中的固定模拟缓存率不作服务商实测。

## 无真实模型凭据的隔离检查

通过 runner 运行已有的非付费脚本：

```powershell
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_context.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_stop.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_team.py
.\.venv\Scripts\python.exe -B tools/run_validation.py tools/validate_agent_attachments.py
```

`tools/run_validation.py` 从示例构造去凭据配置，不读取用户 `config.yaml`；数据库、课题仓库、输出、下载缓存及临时路径重定向到项目内 `.validation-search-20260922/目标脚本名/`。Python 写入守卫拒绝目标目录以外的写入；可读取已有本地模型缓存。隔离的是配置和数据写入，不自动禁止所有网络请求，缺模型时仍可能尝试下载。

| 脚本 | 主要验证范围 |
| --- | --- |
| `tools/validate_agent_context.py` | 模型容量与估算/校准、自动预算、手动压缩、结构/缩减/选区检查、取消保留、原始历史、日志重建及继续 |
| `tools/validate_agent_stop.py` | 本地 HTTP 服务器 + 真实 SDK 的等待/流中取消、不重试、usage 缺失与完整流、取消检查点、目标恢复及结果归属 |
| `tools/validate_agent_team.py` | 方法对比/实验限制、双语资料、统计纠错、仪器标定、空间采样、缺失记录及测序资料等不同任务；并行读取与追问、批量原生工具及 ID 匹配、权限/限额、局部失败、父子停止/超时/回收、重启/压缩与会话归属。模型替身及真实 SDK 对本地 HTTP 服务，不调用付费模型 |
| `tools/validate_agent_attachments.py` | 文件、Office/PDF/扫描页及目录摘录、范围/限额、损坏或缺失资产、图片协议、停止/重启/压缩后的快照保留；模型使用替身，不验证真实视觉质量 |
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
