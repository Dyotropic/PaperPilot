# PaperPilot 项目工程约定

本文件只保留项目特有、仍适用的约束。通用工作方式遵循当前有效的全局 `AGENTS.md` 与用户本次明确要求；历史计划和交接记录用于理解背景，不自动成为现行协作流程。

## 项目与接口

- PaperPilot 是 Python + Flet 桌面文献工作流，含检索、精排、文献库、AI 精读/打分/对话等模块。改动前先看实际调用链和当前源码，不照搬旧阶段文档中的文件位置或函数签名。
- 文献库公共入口见 `paperpilot/library.py`；AI 服务入口见 `paperpilot/ai_service.py`；Agent 面板入口见 `pages/agent_panel.py`。已有调用方依赖的参数、返回字段和语义应保持兼容；确需变更时同步调用方、文档并说明迁移方式。
- Agent 附件入口见 `paperpilot/agent_attachments.py` 与 `pages/agent_attachments_ui.py`，科研团队入口见 `paperpilot/agent_team.py` 与 `pages/agent_team_ui.py`。当前权限约定为子 Agent 只读分析、主 Agent 审查并统一执行修改；扩展工具时保持会话归属、取消传播及原始记录保存契约。
- 修改 `paperpilot/models.py` 要考虑已有 SQLite 数据的迁移与回滚，不直接删改旧字段；新增配置同步更新 `config.example.yaml`，提供缺省值，密钥不得入库、入日志或硬编码。
- 核心打分、排序策略先核对业务目标与已有算法，不擅自改权重或把历史规划当作当前实现。

## 验证与交付

- 应用版本统一维护在 `paperpilot/__init__.py` 的 `__version__`；版本变更须同步导航、窗口显示及 README、用户指南、架构、发展记录和 Word 项目文档。任务、团队及会话 JSON 的格式版本独立维护，不能随应用版本号递增。
- `PaperPilot_项目文档.docx` 是随版本发布的现行项目说明；本地 Phase 1/Phase 2 报告和答辩 PPT 属于历史材料。Word 按全局约定统一微软雅黑及同级样式，不将已移除的中期 Word 报告重新生成。
- 以受影响的用户业务链路为验收主线，按风险选择必要的自动化检查；单测、mock 或历史报告不能代替真实 UI、数据源、模型或端到端链路的验证。不得为了测试数字阻碍主流程。
- 改动前核对分支和工作区，保护未跟踪材料及用户数据库；提交、合并、推送、保留或删除分支均按本次用户要求与当前仓库状态执行，不套用过时流程。
- `validation_evidence/` 只保存本地验收报告、计数和截图，不提交或推送，也不强制添加绕过 `.gitignore`。取消 Git 跟踪时必须保留本地文件；验证工具与长期项目文档仍正常维护。
- 当前架构与接口定位见 `ARCHITECTURE.md`，用户操作见 `USER_GUIDE.md`，验证入口见 `TESTING.md`；修改行为或配置时同步更新相关长期文档。`PHASE3_PLAN.md` 只记录发展脉络与未实现方向，阶段报告属于历史资料；所有接口仍须与现行源码逐项核对。
