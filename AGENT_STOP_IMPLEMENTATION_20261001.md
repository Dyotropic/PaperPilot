# Agent 当前轮停止机制与验收（2026-10-01）

本次实现用户确认的语义：停止当前轮，保留记录；用户随后发新消息或“继续”开启下一轮。

## 源码研究结论

| 项目与本次实际阅读的原始来源 | 停止机制 | 状态与继续 |
| --- | --- | --- |
| [Codex 任务实现](https://github.com/openai/codex/blob/main/codex-rs/core/src/tasks/mod.rs)、[协议](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)，2026-10-01 阅读 main | 每轮 CancellationToken 传给任务；中断进入独立的收尾生命周期，有任务 abort 清理钩子 | 中断轮有历史标记，结束前 flush rollout；协议区分 Interrupt、RecoverTurn 与 SuspendTurnAndShutdown。PaperPilot 本次对应停止一轮，不实现进程挂起与原运行时断点恢复 |
| [DeepSeek Harness Agent loop README](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/packages/core/agent-loop/README.md)、[会话模型](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/docs/subsystems/session.md)、[loop 所有权](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/packages/core/agent-loop/src/index.ts) | 协作式取消当前 activity；keepInbox 控制待处理输入。取消流结束时保留已经交付的正文 | 会话为追加事件日志，模型历史由日志派生；中断的 assistant 锚点保留已输出前缀，恢复时补全中断轮语义。上述固定版本的实现没有将取消等同于删除会话 |
| [OpenCode prompt](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts)，2026-10-01 阅读 dev | cancel 委托会话运行状态；工具收到 AbortSignal，interrupt 清理会触发 abort | 中断会给已启动工具写入取消状态与完成时间，并保存 assistant 消息；已发生的副作用并不会仅因停止而自动撤销 |

Harness 的[人工暂停目标修复记录](https://github.com/deepseek-ai/deepseek-harness/blob/639ed015397290b3745d163aafe02ffee4aa3f84/.agents/notes/archived/bug-fix/2026-09-01-host-goal-pause-aborts-turn.md)还说明：仅阻止下一轮不足以停止已经运行的模型，需要同时取消当前轮；迟到的旧轮不能改回新的目标状态。本次采用运行 ID、原会话身份和子任务所有权避免同类问题。

网络搜索备用 MCP 本次请求失败，官方 Codex 文档页面读取受限；以上结论来自成功读取的公开原始源码，不将失败的页面读取列为证据。SDK 接口还逐项核对了本机已安装的 OpenAI、Anthropic 源码。

## PaperPilot 改动

- `pages/agent_panel.py`：空闲时为向上发送箭头，当前轮运行时为停止方块；停止处理中禁用方块并显示等待退出的提示。Enter 仍只负责发送，工作中不重复发送，也不触发停止；输入草稿保留。
- `paperpilot/agent_runtime.py`：每轮保存运行 ID、原课题/会话、原目标、阶段、已完成步骤和未开始步骤。模型线程、动作回调、检索/评分线程与后台 PDF 下载预先登记所有权，最后一个任务退出后才恢复发送。
- `paperpilot/llm_client.py`：Agent 作用域使用异步 SDK 流式请求；取消信号在线程安全回调中取消等待中的 asyncio 任务，随后关闭连接。支持尚未收到响应头时取消。停止不进入网络重试。默认同步接口与自定义客户端仍兼容；自定义同步客户端仅支持协作检查，无法承诺在其内部阻塞时即时退出。
- OpenAI SSE 解析复用 SDK 的解码与类型处理，并明确顺序关闭嵌套迭代器。本机 SDK 提前遇到终止标记时会遗留迭代器，最初验证发现关闭事件循环的异常；修复后 HTTP 验证结束没有该异常。
- `paperpilot/ai_service.py` 与检索数据源：在下一批评分、下一段精读、下一条级联查询/分页以及重试等待前检查停止。OpenAlex 并行摘要线程显式继承取消作用域；arXiv Agent 路径保留作用域并给 socket 设置超时。
- `pages/search_page.py`、`pages/library_page.py`：停止后丢弃当前未提交阶段的迟到结果。取消检索恢复此前检索结果；评分保留此前评分；精读停止后不再提交新笔记或改变阅读状态。已经提交的文献、PDF 或笔记保留，不自动回滚。
- `paperpilot/conversation.py`：使用现有 metadata 事件记录运行状态，沿用 `events.jsonl` 先 flush/fsync、随后更新 JSON 投影的持久化顺序，旧会话格式兼容。原目标在请求前落盘；摘要阶段停止也补存当前用户请求。只将已生成的主回复正文及中断说明加入历史，截断动作提案不派发。
- 应用重启时将无存活任务的未结束轮标记为 interrupted，不自动重新执行。显式发送“继续”时，在新消息末尾补充保存的原目标、阶段及完成情况；新轮仍保留原目标。运行 ID、时间与状态留在本地元数据，避免改变模型历史前缀。
- 用量列表显示“已停止”。服务商未交付最终 usage 时，输入、输出及缓存计数保持未知，不将缺失数据当作零缓存或零消耗；实际计费仍以服务商账单为准。

## 验证证据与范围

| 验证 | 结果与覆盖 |
| --- | --- |
| `tools/validate_agent_stop.py`（经隔离 runner） | **12 项通过**：DeepSeek 兼容与 Anthropic SDK 在等待响应头/输出途中取消、不重试、完整流 usage；摘要中断保留新目标；日志重建、原目标继续、重启恢复；阻止下一条级联查询；并行摘要线程取消；并发子任务只产生一个终态 |
| `tools/validate_agent_stop_ui.py`（隔离原生 Flet 桌面窗口） | **通过**：真实鼠标点击停止方块，键盘 Enter/Shift+Enter，保留部分正文/草稿，继续携带中断历史；模型完成后仍保留检索与评分任务的停止方块；同步步骤退出前显示停止中；检索/评分迟到结果不提交。精读由真实库页回调启动，真实鼠标停止，迟到结果不写数据库、JSON 或阅读状态 |
| 原有 `test_agent_sessions_ui.py` | 桌面回归输出全部业务 PASS：新建/切换/恢复，迟到回复保持原会话，用量/重命名，深浅主题与窄面板，错误精读不保存，切换会话后精读保持原归属。关闭窗口时发现迟到滚动报错，补充卸载控件的 RuntimeError 收尾后再次回归通过，未再出现该异常 |
| 原有 `test_agent_sessions_usage.py` | **23 项通过**，覆盖会话、用量、资料快照与前缀等既有行为 |
| 原有 `test_llm_client.py` | **37 项通过**，保持默认同步接口、模型参数、Provider 与密钥语义兼容 |
| 原有 `test_search_features.py` | **14 项通过**，覆盖检索参数、分页、缓存与原文献身份规则 |

原生验收 JSON 为 `validation_evidence/agent_stop_native_20261001.json`，截图为 `agent_stop_running_20261001.png` 与 `agent_stop_preserved_20261001.png`。模型与工具结果使用合成夹具；SDK 传输使用真实本地 HTTP 服务器，无真实模型付费请求。本次未验证 DeepSeek 官网账单对中断请求的计费，也未对每一家 OpenAI 兼容服务商逐一做外部请求验收。

已进入的同步 HTTP 下载及本地排序无法安全强杀 Python 线程，需当前受超时限制的 I/O 或计算返回后退出；其后续步骤受取消检查限制。关闭应用时，已经落盘的目标/历史/完成步骤可恢复；突然崩溃前尚未保存的流式片段不保证保留。停止后继续是一轮新的请求，未完成阶段可能重新计算。
