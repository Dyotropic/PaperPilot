# CLAUDE.md — PaperPilot 项目协作铁律

## 项目概要
PaperPilot：面向课题攻关的可解释智能文献工作流系统。
技术栈：Python + Flet + FAISS + SQLAlchemy + DeepSeek API + Ollama


## 分支策略
```
main      ← 稳定版本，仅打 tag
develop   ← 日常开发主干
  ↑
feature/* ← 每人/每任务独立分支（如 feature/faiss-index）
hotfix/*  ← P0/P1 紧急修复分支
```

## 标准开发流程
1. **开工同步**：`git checkout develop` → `git pull` → `git checkout -b feature/xxx`
2. **开发**：在自己分支上写，不碰别人的文件；每日开工先在群里报今天要改的文件清单
3. **提交**：遵循提交规范（见下），小步提交，每次改动聚焦一个点
4. **合并前同步**：`git rebase develop`（冲突少）或 `git merge develop`（冲突多），解决完再提 PR
5. **合并到 develop**：GitHub PR `feature/xxx → develop`，搭档 review 后选 "Create a merge commit"
6. **清理**：合并后删除本地和远程的 feature 分支，不留垃圾

## 提交规范
```
类型: 简述（50字内，中文/英文统一）

- 改动点1
- 改动点2

Closes #Issue编号
```
类型：`feat` / `fix` / `refactor` / `docs` / `test` / `chore`

## Bug 处理

| 级别    | 定义                | 响应   | 流程                               |
| ------- | ------------------- | ------ | ---------------------------------- |
| P0 致命 | 崩溃/核心功能不可用 | 立即   | hotfix → develop，同步 main 打 tag |
| P1 严重 | 功能异常但可绕过    | 4h 内  | hotfix → develop                   |
| P2 一般 | UI 错位/性能下降    | 24h 内 | 当前 feature 分支顺手修            |
| P3 优化 | 体验改进/代码异味   | 排期   | 记 Backlog                         |

P0/P1 修复流程：`checkout -b hotfix/xxx` → 最小化修复 → 本地验证 → `merge --no-ff` 回 develop → 群里通知搭档 pull
修复后自检：复现通过 / 未引入新报错 / 搭档已同步 / P0 时更新 CHANGELOG

## Claude Code 守则
1. **独立会话**：两人各自的 Claude Code 会话不共享上下文，所以每次开工前先把当前任务和目标文件交代清楚
2. **修改前确认**：改代码之前先用 `git status` 确认当前分支和改动范围，不在错误分支上写代码
3. **关键算法人工设计**：打分公式、权重更新、排序策略等核心逻辑，由人先写伪代码，Claude 仅负责实现
4. **大改动前快照**：大规模重构或批量改名之前，先 `git commit` 保存当前状态
5. **生成代码需 review**：Claude 生成的代码必须由搭档快速过一眼再合并，不允许直接推到 main/develop
6. **接口变更必须通知**：如果改了 models.py 或函数签名，先在群里说，不要偷偷改
7. **硬编码零容忍**：API Key / 路径 / 配置项抽到 `config.yaml` 或环境变量，不写入业务代码

## 防冲突铁律
- 每天开工前在群里互报"今天要改的文件清单"
- 数据库模型（models.py）变更必须先发 PR 合并，再写依赖它的业务代码
- 合并时遇到不确定的冲突，拉搭档一起看，不自己硬解

## 开发顺序（Phase 1）
1. 一起定 models.py + 目录结构 + 关键函数签名 → 提 PR 合并到 develop
2. A 在 `feature/data-pipeline` 开发抓取 + 索引 + 测试数据
3. B 在 `feature/app-ui` 开发 Flet 框架 + KeyBERT + DataTable
4. A 先完成排序接口 → B 接入 DataTable → 两人联调

## 交付前检查
- [ ] `git status` 确认无遗漏文件
- [ ] 断网测试离线模式是否正常切换
- [ ] 主流程端到端跑通（课题输入 → 抓取 → 排序 → 展示）
- [ ] 搭档已 pull 最新 develop 并验证通过
