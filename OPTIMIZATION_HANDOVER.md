# PaperPilot 代码质量优化——交接文档

> 撰写日期：2026-08-29 ｜ 分支：develop ｜ 配套计划：[OPTIMIZATION_PLAN.md](OPTIMIZATION_PLAN.md)
> 更新：**2026-08-30 全部批次完成并推送**（C1-C9 与批次 D 已由继任执行完毕，commit 见下表）

---

## 一、当前状态速览

| 批次 | 状态 | Git 位置 |
|------|------|---------|
| A 死代码删除（-745 行） | ✅ 完成已推送 | commit `412aaf2` |
| B 真 bug 修复 16 项 | ✅ 完成已推送 | commit `99b5833` |
| C 性能优化 9 项 | ✅ 完成已推送 | C1 `f625f90`、C2 `225f3ae`、C3 `2c88fbb`、C4 `0d329e0`、C5 `1761243`、C6-C9 `557d2af` |
| D 保守收敛 6 项 | ✅ 完成已推送 | D1 `20ef707`、D2 `9bb793c`、D3-D4 `0672700`、D5-D6 `fcb6447` |

**交接第一件事**（已完成，2026-08-30）：确认工作区 C1/C2 完好后按门禁提交，并依次完成 C3-C9 与 D1-D6。全部批次已验证（六套件 unit 68 / epmc 35 / llm_client 37 / local_import 20 / graph_service 44 / graph_window 52 全绿）并推送 develop。

**环境注意**：一律用 `.venv/Scripts/python`（系统 Anaconda python 缺 openai/anthropic）；测试文件 `test*.py` 均为本地 check() 风格脚本且被 .gitignore 忽略，**不提交**；LF→CRLF warning 无害。

**验证命令**（各测试文件的作用/时效性/耦合点详见 [TESTING.md](TESTING.md)）：
```bash
.venv/Scripts/python -m py_compile <改动文件>
for t in test_unit_core.py test_fetcher_europepmc.py test_llm_client.py test_local_import.py test_graph_service.py test_graph_window.py; do .venv/Scripts/python $t 2>&1 | tail -1; done
.venv/Scripts/python test_e2e_pipeline.py 2>&1 | grep "E2E 总计"   # 期望 62 通过 0 失败（OpenAlex 限流日会有 1 条已知告警，不算失败）
.venv/Scripts/python app.py    # 启动冒烟：无 error/traceback，三页可切换
```

---

## 二、已完成工作明细

### 批次 A（commit 412aaf2，净删 741 行）
全部经全仓调用点计数验证为零调用后删除：ai_service 流式死链与问答死簇、pdf_viewer 旧 HTML 窗口方案死簇与 `__PDF_DATA__` 永假分支、indexer FAISS 全家桶、library 三个死 CRUD、keywords/merge_keywords_weighted、fetcher/_extract_text_from_page、context 死字段、若干未使用 import。**执行中的一次失误与修复**：截断 pdf_viewer 死簇时误删了活函数 `is_full_reader_available`（test_graph_window 抓住），已恢复——教训：范围截断删除必须先 `git diff` 核对区间内全部 def。保留项及理由见计划文档。

### 批次 B（commit 99b5833，16 项）
对比分析按钮解禁（原为全仓无解禁点的死键）、检索防重入（复活 is_searching）、_sidebar_busy try/finally、_score_chunk 单条容错、RLM 滑窗向上取整、rerank 空列表守卫、_DEEP_READ_DIR 锚定、Timer 裸 update→safe_update、后台线程不读控件、保存后计数刷新、改名/删除失败提示与中止、import_pdf 失败不写坏 catalog、extract_all_keywords 去重复 LLM 调用、**OpenAlex API Key 保存即生效**（改现读 config）、两处 docstring/计数语义修正、clean_recycle 接线（启动后台静默清理，经用户确认）。门禁：全套测试全绿 + E2E 62/0/0 + 启动冒烟通过。

### 批次 C 已完成部分（**工作区未提交**）
- **C1 fetcher.deduplicate**：归一化每篇只算一次 + 相同标题集合 O(1) 判重 + SequenceMatcher 前长度预筛。预筛阈值经数学推导：`ratio = 2·min/(l1+l2) ≥ 0.9 ⇔ 长串 ≤ (11/9)·短串`，跳过条件用 `b*9 > a*11`（整数安全，边界比恰 11:9 时不跳过）。**验证**：400 篇随机数据集新旧结果顺序逐项一致、旧 4.9s → 新 1.7s；空列表/全同/同长真重复/11:9 边界对全部等价。
- **C2 graph_service**：引用边构建由"对每篇论文扫全表"改为 W-id→paper_id 反转映射查表；共现边 lower2disp 映射由内层循环每对重建改为每篇建一次。**验证**：test_graph_service 44/44（含 URL 形态 W-id 引用边用例）。

---

## 三、待办任务（C3-C9、D1-D6 —— ✅ 全部于 2026-08-30 完成，commit 见 §一表）

### C3 library.py 批量操作（3 个子项）

**C3a 批量删除 N+1**
- 问题：`remove_papers_from_project`（library.py，grep `def remove_papers_from_project` 定位）循环调用 `remove_paper_from_project`，每篇新开 Session + 一次 commit（= 一次磁盘 fsync）。删 50 篇 = 50 次 fsync，且在主线程执行。
- 做法：单 Session 实现：
  ```python
  def remove_papers_from_project(project_paper_ids: list[int]) -> int:
      ids = [i for i in (project_paper_ids or []) if i is not None]
      if not ids:
          return 0
      session = _get_session()
      try:
          # 先删级联的 Feedback（与单篇版 remove_paper_from_project 语义一致）
          session.query(Feedback).filter(
              Feedback.project_paper_id.in_(ids)).delete(synchronize_session=False)
          n = session.query(ProjectPaper).filter(
              ProjectPaper.id.in_(ids)).delete(synchronize_session=False)
          session.commit()
          return n
      except Exception:
          session.rollback()
          raise
      finally:
          session.close()
  ```
  返回值语义 = 实际删除条数（rowcount），调用方（library_page on_batch_delete）只用计数。
- 测试：临时进程脚本——建内存/临时库课题 + 3 篇论文 → 批删 2 篇 → 断言返回 2、DB 中 ProjectPaper 剩 1、Feedback 无残留；调用 remove_paper_from_project 单篇版确认未破坏。
- 验收：批量删除 UI 正常、计数正确、全套测试不回退。

**C3b update_paper_scores 循环单查**
- 问题：循环内每篇执行一次 ProjectPaper join Paper 查询（400 篇 = 400 条 SQL，主线程 CE 排序路径）。
- 做法：循环前一次 `session.query(ProjectPaper, Paper).join(Paper).filter(ProjectPaper.project_id == project_id).all()`，建两个字典：`doi → pp`、`(title.lower(), year) → pp`；循环内按原三级匹配语义（DOI 优先 → 标题+年份）查表更新。**匹配语义必须与原逐条查询完全一致**（原代码见该函数体内的 if doi / else 分支）。
- 测试：CE 排序后抽查 DB total_score/score_similarity 与优化前一致（可先 dump 再对比）。
- 验收：排序功能正常、DB 值逐行一致、测试全绿。

**C3c update_paper_ai_scores 回退分支**
- 问题：paper_dicts 缺失时的回退分支在循环内执行全量查询（O(结果数×课题论文数)）。当前调用方都传 paper_dicts，路径未触发——属排雷。
- 做法：把循环内的 query 提到循环外一次取全，建 `id → pp` 字典循环内索引。
- 测试：构造一次不传 paper_dicts 的调用（临时脚本），断言更新数与原实现一致。
- 验收：两条路径（带/不带 paper_dicts）行为不变。

### C4 设置页滑条持久化移到 on_change_end
- 问题：settings_page 三个 Slider（max_results/top_k/ce_candidates，grep `on_slider_saved` 定位）绑 on_change → save_config（读 yaml + 全量写回）。拖一次最多 40 次磁盘 IO，发生在 UI 事件循环线程。
- 做法：Flet 0.85 Slider 存在 `on_change_end`（flet/slider.py:251 已确认）。把持久化回调绑到 on_change_end；on_change 留空即可（Slider 自带 label="{value} 篇" 显示）。
- 测试：拖动滑条 → 松手后 config.yaml 只变化一次且值正确；重启应用滑条恢复该值。
- 验收：拖动过程零写盘（可在 save_config 临时加计数打印验证），松手持久化正确，设置页其它保存逻辑不回归。

### C5 全选/勾选热路径 RPC 收敛（2 个子项）
**C5a search_page**
- 问题：`_on_search_select_all` 循环内每 checkbox 一次 `.update()`（top_k≤200 → ≤200 条 RPC）；Shift 范围勾选分支用 `ctx.page.update()` 全量刷新。
- 做法：循环内只赋值 `cb.value = checked`，循环后一次 `_search_list.update()`；Shift 分支的 `ctx.page.update()` 改 `_search_list.update()`。
**C5b library_page**
- 问题：`on_select_all` 调 refresh_paper_list()（全量 DB 查询 + 重建 100 行）只为翻勾选态。注意原语义：全选 = **全部筛选后论文**（`_selected_ids.update(p["project_paper_id"] for p in _project_papers)`，不分页），UI 复选框只显示当页。
- 做法：refresh_paper_list 构建行时把当页 checkbox 收进列表（如 `_page_checkboxes: list[ft.Checkbox]`，与 `_page_pp_ids` 平行）；on_select_all 改为：更新 _selected_ids（保持全集语义）→ 循环翻当页 checkbox.value → 一次 `_library_list.update()` → update_count()。**不要**再调 refresh_paper_list。
- 测试：全选/取消全选后：计数文本、对比按钮（含 disabled 态）、图谱菜单"仅选中（M 篇）"、select_all_cb 勾选态（原判断 `len(_selected_ids)==len(papers)`）全部正确；翻页后新页勾选态正确反映 _selected_ids。
- 验收：一次全选只发一次列表级 update（临时计数验证）；跨页/筛选/换课题行为与原先等价。

### C6 send_agent_message 双重刷新
- 问题：agent_panel send_agent_message（grep 定位）先 `_agent_msg_list.update()` 又 `ctx.page.update()`——后者全量遍历控件树（含 overlay 里累积的对话框），消息是高频路径。
- 做法：删除 page.update() 块，保留 list.update()（ListView 是页面常驻控件，子树 update 足以推送）。
- 测试：在文献页/检索页/设置页分别触发 Agent 消息（发消息、精读进度、系统提示），气泡即时出现；切回各页面无渲染异常。
- 验收：消息实时显示无丢失；全套测试 + 启动冒烟通过。

### C7 _show_detail_dialog 移出循环
- 问题：refresh_paper_list 的行构建循环体内每页定义 100 个相同的 `_show_detail_dialog` 闭包，且行内有 `import json as _json`。
- 做法：该函数通过参数接收 paper、无行级依赖（已核实 1116 行 `lambda e, p=p: _show_detail_dialog(p)`），整体提到 refresh_paper_list 函数之外（build_library_page 闭包内）；`import json as _json` 提到模块顶层（检查文件内其它 `_json` 局部导入点统一）。
- 测试：点任意行弹详情对话框（标题/作者/摘要/AI 四维理由/精读笔记/用户批注渲染正常）。
- 验收：循环体内不再有 def；详情功能手测通过。

### C8 indexer 超时保护修复
- 问题：`_get_cross_encoder` 与 predict 的超时用 `with ThreadPoolExecutor(...)` 包裹——`future.result(timeout=N)` 抛 TimeoutError 后，with 退出触发 `shutdown(wait=True)` 无界阻塞等待线程跑完，180s/300s 超时形同虚设（模型 942MB 冷加载可达数分钟）。
- 做法：两处（grep `future.result(timeout` 定位）改为显式管理：
  ```python
  executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
  try:
      future = executor.submit(_load)
      _cross_encoder = future.result(timeout=180)
      ...
  except concurrent.futures.TimeoutError:
      _cross_encoder = None
      return None
  except Exception as e:
      ...
  finally:
      executor.shutdown(wait=False)
  ```
  （范式参照 arxiv_source 的既有实现）
- 测试：正常路径加载 CE 成功不受影响（跑一次检索触发模型加载）；超时路径写 5 分钟级 mock 单测可选，至少代码推演 + 启动冒烟。
- 验收：正常加载/预测无回归；推演确认超时立即返回。

### C9 refresh_paper_list 跳过重复查询
- 问题：on_sort_click / on_ai_sort_click 完成后的刷新会重新 `library.get_project_papers(...)` 全量查询——排序函数自身刚查过同一数据。
- 做法：签名加可选参数（向后兼容）：`def refresh_paper_list(preloaded_papers: list[dict] | None = None)`，非 None 时跳过内部查询直接用；两个排序完成处把已持有的 all_papers 传入。
- 测试：CE/AI 排序完成后列表分数与顺序正确；翻页/筛选/换课题/Shift 勾选不受影响（这些调用点不传参数走原路径）。
- 验收：排序完成链 DB 查询减半（临时计数验证）；全部回归通过。

### D1 对话框 helper（修 overlay 泄漏）
- 问题：page.overlay 15 处 append、仅 1 处 remove——对话框关闭只 `dlg.open=False`，对象永久累积；双击保存会叠加两个真对话框。
- 做法：components.py 新增：
  ```python
  def open_dialog(page, dlg):
      """append + open + update 的统一入口。"""
      page.overlay.append(dlg)
      dlg.open = True
      page.update()

  def close_dialog(page, dlg):
      """关闭并从 overlay 移除（修复泄漏）。"""
      dlg.open = False
      try:
          page.overlay.remove(dlg)
      except ValueError:
          pass
      page.update()
  ```
  替换 15 处（grep `overlay.append` 全清单；现有 1 处 remove 的图谱进度框也统一）。注意各处现有 `close_dlg` 局部函数改为调 close_dialog。
- 测试：每个对话框（详情/确认删除/导出/图谱进度/对比/项目更新提示/保存到文献库）开关一遍；同一对话框连开 20 次后 `len(ctx.page.overlay)` 恒定。
- 验收：overlay 长度不随使用增长；对话框功能与视觉无回归。

### D2 set_paper_pdf_path_smart（pdf_path 规则收敛）
- 问题："DOI 优先 → 标题+年份回退"写 pdf_path 规则在 4 处重复：search_page `_update_db_pdf`（do_save 内定义）、`_auto_download` 内两处内联、agent_panel 导入/后台下载两处。
- 做法：library.py 新增公开函数（非冻结接口，允许新增）：
  ```python
  def set_paper_pdf_path_smart(paper: dict, pdf_path: str) -> bool:
      """DOI 优先、标题+年份回退地写入 pdf_path。"""
      doi = paper.get("doi") or ""
      if doi and set_paper_pdf_path(doi=doi, pdf_path=pdf_path):
          return True
      title = paper.get("title") or ""
      if title:
          return set_paper_pdf_path_by_title(title, pdf_path, paper.get("year"))
      return False
  ```
  4 处调用方逐行对照替换（`_update_db_pdf` 函数体直接委托为 return set_paper_pdf_path_smart(...)）。
- 测试：带 DOI 论文与无 DOI（纯标题）论文各保存一次，DB pdf_path 写入成功；对比优化前后 DB 值一致。
- 验收：检索导入链与 Agent 导入链各手测一次；行为与原先完全一致。

### D3 JSON 原子写
- 问题：conversation.py 的 `_save`、repo_manager 的 `save_catalog` / `_save_cache_index` 直接 `write_text`——进程退出/断电截断 → 下次读抛 JSONDecodeError 被吞 → **对话历史/catalog/缓存索引静默清零**。
- 做法：公共处（建议 paperpilot/ 下新建小工具或放 repo_manager 顶部导出）加：
  ```python
  def atomic_write_text(path, text: str) -> None:
      tmp = Path(str(path) + ".tmp")
      tmp.write_text(text, encoding="utf-8")
      os.replace(str(tmp), str(path))  # Windows 同目录原子
  ```
  三处替换。tmp 残留文件在下次写入自然覆盖，无清理负担。
- 测试：写后读一致；大文件写入中途 kill 进程（手工模拟）后原文件仍完整可读。
- 验收：正常读写无回归；推演确认任意时刻磁盘上只有完整旧版或完整新版。

### D4 repo_manager 并发锁
- 问题：cache_index/catalog 的读-改-写（load→dict 修改→save）无锁；后台下载线程与主线程导入并发时丢条目、total_size 失准；索引损坏后旧缓存文件全部孤儿化。
- 做法：repo_manager 模块级 `_cache_lock = threading.Lock()`；用 `with _cache_lock:` 包住 get_cached_pdf、cache_pdf、import_pdf 中"load→改→save"整段（粒度到整个读改写单元，不要只锁 save）。catalog 同理评估（写点多的话先只锁 cache_index——最高频冲突点）。
- 测试：双线程并发调 cache_pdf/import_pdf 的压测临时脚本（同一论文 ×2 线程 ×50 次），断言索引条目数与 total_size 正确。
- 验收：并发一致；单线程全部链路手测无回归。

### D5 library 文件选择异步化
- 问题：library_page 三个菜单（上传单个/多个/文件夹）on_click 直接调 `_pick_single_file()` 等 → 内部 `subprocess.run(["powershell",...], timeout=120)` **同步执行在 Flet 事件循环里**——文件对话框打开期间整个应用（含 Agent 面板、页面切换）冻结最长 120s。flet 0.85 同步 handler 不走 executor（源码已核实）。
- 做法：照抄 search_page `_bg_pick` 既有模式（grep 定位）：threading.Thread 执行 + Event 置结果 + `ctx.page.run_task` 轮询取结果继续原导入流程。三个 handler 同构处理，结果处理逻辑原样搬进 poll 回调。
- 测试：手工回归三条链——上传单文件/多文件/文件夹导入，选择期间尝试切换页面/发 Agent 消息（应可响应）；导入结果与原先一致。
- 验收：对话框打开期间 UI 不冻结；三条导入链功能等价。

### D6 PowerShell 对话框 helper 收敛
- 问题：整段 FH/SetForegroundWindow/keybd_event 焦点修复 + tempfile + subprocess 样板存在 5 份拷贝（library_page `_run_ps_dialog`+`_FOCUS_HELPER` 已是现成实现但 `_do_export` 的 `_bg_dialog` 未复用；search_page `_bg_pick` 又独立复制一份）。
- 做法：把 `_run_ps_dialog(script)` 与 `_FOCUS_HELPER` 提升到 pages/components.py；library 两处、search 一处改传脚本模板调用。注意 helper 无 paperpilot 依赖，纯 Win32+subprocess，放 components 安全。
- 测试：导出 BibTeX/CSV（另存为对话框）、检索页导入选择、library 上传三条链手测。
- 验收：行为不变；重复样板净删 ~100 行。

---

## 四、不做清单（保持现状，理由见计划文档）

models.py 死列/死表（冻结）、config.py 原子写（评审区）、llm_client yaml 重读缓存（收益/风险比不划算且测试钉住 load_config）、PDF 五处全链 ensure_pdf 合并、双 PDF 缓存合并、send_agent_message 线程投递改造、build_library_page(1751行)/build_search_page(889行) 拆分、keywords/indexer 重库惰性导入、arxiv 源缓存、会话竞态完整修复（D3 已消除文件损坏面）、课题名净化正则 6 处与 CJK helper 4 处收敛。

---

## 五、批次门禁与提交规范（每个批次照此执行）

1. **门禁**：`py_compile` 全部改动文件 → 全套本地测试全绿（unit 68 / epmc 35 / llm_client 37 / local_import 20 / graph_service 44 / graph_window 52）→ E2E 62 通过 0 失败 → `app.py` 启动冒烟（日志无 error/traceback）→ 核心流程手测（检索→保存→多选勾选→导出→知识图谱打开）；
2. **提交**：Conventional Commits（refactor/fix/perf + scope），一批次一个 commit；测试文件与临时脚本不提交（gitignored / 用后删）；
3. **回退纪律**：任何一项在验证中发现被测试钉住或有隐藏调用点，立即回退该项并在交接文档追加记录；
4. **完成后**：在 OPTIMIZATION_PLAN.md 把对应项状态改 ✅，最终汇报包含每项前后对照与测试结果，推送 develop 供搭档 Review（本轮全程未动 models.py/config.py/冻结接口）。

## 六、commit 信息模板（供直接使用）

```
perf(graph): 图谱构建内循环 O(n²)→O(n)（C2）

- 引用边改 W-id 反转映射查表；共现边 lower2disp 每篇只建一次
- 44 项单测回归通过，输出不变
```
```
perf(fetcher): deduplicate 预归一化+长度预筛（C1）

- 相同标题集合 O(1) 判重；SequenceMatcher 前按 hi ≤ 11/9·lo 数学界预筛
- 400 篇随机集新旧结果逐项一致，2.9× 提速
```
