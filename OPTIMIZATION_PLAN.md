# PaperPilot 代码质量优化计划

> 撰写日期：2026-08-29 ｜ 状态：**已批准并全部执行完成**（批次 A/B/C/D 均已完成并推送 develop）
> 执行进度与逐项交接见 [OPTIMIZATION_HANDOVER.md](OPTIMIZATION_HANDOVER.md)

**调研方式**：3 个审计代理（业务层/UI 层/数据层）+ 对全部关键论断逐条亲自复核（对比按钮 disabled 全仓零赋值点、is_searching 零读取、RLM 滑窗公式数值推演、_score_chunk 类型崩溃路径、流式方法死链、indexer 超时被 with 块 shutdown(wait=True) 抵消、滑条逐刻度写盘、Timer 裸 update、文件对话框同步阻塞、overlay 15 append/1 remove——**全部属实**）。所有死代码均经全仓调用点计数验证。**不改架构、不动 models.py/config.py、不碰冻结接口签名**。

## 批次 A：死代码删除（~600 行，零行为变化）—— ✅ 已完成

| 项 | 位置 | 证据 |
|---|---|---|
| A1 | ai_service.py：chat_stream + _call_api_stream 流式死链、ask_question/_QUESTION_TRIGGERS/_needs_full_text/_qa_sessions 死簇、get_conversation_info | 全仓调用点 0（唯一引用链是内部互调） |
| A2 | pdf_viewer.py：render_preview/is_downloader_available/_open_html_window/_open_text_window/_open_error_window/_build_error_html/_ERROR_HTML/_escape_html、死 import threading/base64/re、模板永假 `__PDF_DATA__` 分支 | 全仓 0 调用；旧 base64 方案残留 |
| A3 | indexer.py：FAISS 全家桶（embed_*/build_index/search_similar/save_index/load_index/fuse_scores/_cache_*/_get_model）+ _MODEL/_cache_dir + config/hashlib import | docstring 自证"FAISS 已移除"；0 调用 |
| A4 | library.py：update_project_name/update_user_notes/record_feedback | 0 调用（Feedback 表因此成死表，models 不动仅记录） |
| A5 | keywords.py：merge_keywords_weighted | 0 调用 |
| A6 | fetcher.py：_extract_text_from_page | 0 调用 |
| A7 | context.py：selected_paper/containers 死字段 | 0 读写 |
| A8 | 未使用 import：ai_service os/uuid、agent_panel AppState、library_page downloader | AST/grep 验证 |

保留（有意不删）：import_local_pdfs（测试钉住的兼容面）、translate_all_terms（多组单次调用的更优实现，留作接线候选）、PaperSource.fetch（协议方法）、llm_client.chat_stream（LLMClient 层公共 API）。

## 批次 B：真 bug 修复（16 项）—— ✅ 已完成

| 项 | 问题 | 类型 | 修法 |
|---|---|---|---|
| B1 | 两页"对比分析"按钮创建时 disabled=True 且无任何解禁赋值点（grep 证实）→ 功能性死键 | 健全性 | update_count/_update_search_count 加 `disabled = n < 2` |
| B2 | "开始检索"无重入防护（双击=两条流水线）；is_searching 写而不读 | 健全性 | 入口 `if state.is_searching: return` |
| B3 | show_paper_detail 的 _sidebar_busy 无 try/finally，异常即永久锁死详情侧栏 | 健全性 | try/finally 包裹函数体 |
| B4 | _score_chunk：LLM 返回字符串 index/"8.5" 分数即 TypeError/ValueError，单条畸形毁整批精排 | 边界 | 单条 try/except continue + int() 包裹 |
| B5 | RLM Tier2 滑窗 total_windows 用整除，8K-11.5K 文本最多 48% 正文从未被读（数值验证） | 边界 | 改向上取整 `-(-a//b)` |
| B6 | rerank_with_cross_encoder 空列表 np.min 必崩（潜伏雷） | 边界 | 空守卫 return [] |
| B7 | _DEEP_READ_DIR 相对 CWD，换目录启动落盘位置漂移 | 边界 | 锚定项目根（对齐 library.py frozen 模式） |
| B8 | Timer 线程裸 update（library_page） | 健全性 | components 新增 safe_update，Timer 改用 |
| B9 | 后台线程读控件值（search_page ai_limit_dd.value） | 线程 | 用主线程已读的 limit |
| B10 | 保存到文献库/Agent 导入后选中计数陈旧 | 健全性 | 补调 update_search_count |
| B11 | 课题改名/删除的文件操作失败被静默吞 → 对话/目录与 DB 分叉、目录孤儿 | 边界 | 失败时 UI 提示（删除则中止） |
| B12 | import_pdf 拷贝失败仍把不存在文件写进 catalog | 边界 | 失败登记源文件原名 |
| B13 | extract_all_keywords 本地回退重复调 LLM（同一课题描述打两次 API） | 性能/逻辑 | 回退直接走本地提取 |
| B14 | 设置页保存 OpenAlex API Key 运行中永不生效（读 import 期冻结单例） | 正确性 | _get_api_key 改 load_config() 现读 |
| B15 | score_papers docstring 与实现矛盾；AI 回填混入"PDF 更新数"计数 | 可维护性 | 改 docstring；计数独立（保持二元组） |
| B16 | clean_recycle 承诺的回收站 7 天清理从未接线（经用户确认启用） | 功能 | app 启动 daemon 线程静默调一次 |

## 批次 C：性能优化（行为/输出不变）—— 部分完成

| 项 | 问题 | 修法 | 收益 | 状态 |
|---|---|---|---|---|
| C1 | fetcher.deduplicate O(n²)：每对重复正则归一化 + 无预筛 SequenceMatcher | 归一化预计算 + 集合判重 + 数学界长度预筛（hi ≤ 11/9·lo，安全不误杀） | 去重阶段 2.9× 提速，结果逐项一致（400 篇随机集验证） | ✅ 完成（f625f90） |
| C2 | graph_service 引用边 O(n²) 扫描 + 共现边内层每对重建 lower2disp dict | W-id 反转映射查表 + 映射每篇只建一次 | 内循环 O(n²)→O(n) | ✅ 完成（225f3ae） |
| C3 | remove_papers_from_project：N 篇 = N 个 Session + N 次 commit；update_paper_scores 循环单查；update_paper_ai_scores 回退分支循环内全表查询 | 单 Session `in_()` 批删（rowcount 保持返回语义）+ 预取字典 | 批删 50 篇从 50 次 fsync → 1 次 | ✅ 完成（2c88fbb） |
| C4 | 设置页滑条 on_change 每刻度全量读写 config.yaml（拖一次=40 次磁盘 IO） | 持久化移到 on_change_end（Flet 0.85 slider.py:251 确认存在） | 消除拖动磁盘风暴 | ✅ 完成（0d329e0） |
| C5 | 全选逐 checkbox update（≤200 条 RPC）；Shift 勾选热路径 ctx.page.update() 全量刷新；library 全选整表重建+重查 DB | 循环赋值 + 一次列表 update；library 收集 _page_checkboxes 只翻当页 | 多选操作百次 RPC → 1 次 | ✅ 完成（1761243） |
| C6 | send_agent_message 双重刷新（list.update 后又 page.update 全量遍历含 overlay 累积对话框） | 去掉 page.update（保留 list.update），回归验证 | 高频消息路径减半刷新成本 | ✅ 完成（557d2af） |
| C7 | _show_detail_dialog 定义在每行循环内（每页造 100 个闭包）+ 行内 import json | 提到循环外/顶层 | 列表渲染减开销 | ✅ 完成（557d2af） |
| C8 | indexer 两处超时保护被 with 块 shutdown(wait=True) 抵消（超时后仍无界阻塞） | 改 shutdown(wait=False)（对齐 arxiv_source 既有范式） | 模型冷加载超时真正可退出 | ✅ 完成（557d2af） |
| C9 | 同一动作链重复 DB 查询（排序完成后 refresh_paper_list 再查一次） | refresh_paper_list 加可选 preloaded_papers 参数（向后兼容） | 排序链减半 DB IO | ✅ 完成（557d2af） |

## 批次 D：保守收敛（新增 helper，机械替换，行为不变）—— ✅ 全部完成

| 项 | 问题 | 修法 | 收益 | 状态 |
|---|---|---|---|---|
| D1 | page.overlay 15 处 append 仅 1 处 remove → 对话框对象累积泄漏 + 双击叠框 | components 加 open_dialog/close_dialog，替换 15 处 | 修泄漏 + 净删 ~80 行样板 | ✅ 完成（20ef707） |
| D2 | "DOI 优先→标题回退"写 pdf_path 规则 4 处重复实现 | library 新增 set_paper_pdf_path_smart，逐行对照替换 | 单一实现，消除规则漂移 | ✅ 完成（9bb793c） |
| D3 | conversation.py/repo_manager 的 JSON 落盘非原子 → 退出截断即历史/catalog 静默清零 | 统一 atomic_write_text（tmp + os.replace） | 数据健全性 | ✅ 完成（0672700） |
| D4 | repo_manager cache_index/catalog 读-改-写无锁 → 并发丢条目、索引损坏孤儿化 | 进程内 threading.Lock 盖住读-改-写单元 | 并发正确性 | ✅ 完成（0672700） |
| D5 | library 页 3 个文件选择 handler 同步阻塞事件循环最长 120s（flet 同步 handler 不走 executor，选择对话框打开期间整个应用冻结） | 照抄 search 页 _bg_pick 线程模式 | 消除全应用卡死点 | ✅ 完成（fcb6447） |
| D6 | PowerShell 对话框 5 份拷贝（library_page 已有 _run_ps_dialog helper 未复用） | 提升到 components 统一复用（run_ps_script + _PS_FOCUS_HELPER） | 净删 ~100 行 | ✅ 完成（fcb6447） |

## 明确不修清单（记录原因）

- **models.py 死列/死表**（embedding_id、Keyword 表、Feedback 表、score_* 四列、push_interval_days、init_db）——冻结文件需搭档评审 + SQLite 删列需表重建，仅记录；
- **config.py**（save_config 非原子写）——评审区，不动；
- **llm_client 每次读 yaml**——实测单次 0.5-2ms 且测试钉住 load_config 替换，收益/风险比不划算，不改；
- **PDF 五处全链 ensure_pdf 收敛、双 PDF 缓存合并、send_agent_message 线程投递改造、build_library_page(1751行)/build_search_page(889行) 拆分、keywords/indexer 重库惰性导入、arxiv 源缓存**——涉及行为/架构级变化或回归面过广，仅记录；
- 会话竞态（切课题可丢 AI 回复）的完整修复——原子写（D3）先消除文件损坏面，实例复用修复留待单独评审；
- 课题名净化正则 6 处、CJK helper 4 处收敛——churn 大收益低，记录。

## 执行与验证约定

1. 顺序：A → B → C → D，每批次独立完成并验证后 commit（refactor/fix/perf 分类，scope 按模块）；
2. 每批次门禁：py_compile 全部改动文件 + 全套本地测试（unit 68 / graph_service 44 / graph_window 52 / llm_client 37 / local_import 20 / epmc 35 必须全绿）+ E2E（62 通过、0 失败）+ 应用启动冒烟；
3. 高风险点单独验证：C1 新旧实现对同一数据集比对结果一致性（已做）；C3 验证删除计数与级联；D5 手工回归文件选择/上传/导出三条链；任何一项若发现被测试或隐藏调用点钉住即回退该项并记录；
4. 全部使用 `.venv/Scripts/python` 运行（系统 Anaconda python 缺依赖）；测试为本地 check() 风格脚本（gitignored，不提交）。
