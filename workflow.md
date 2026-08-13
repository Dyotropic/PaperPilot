# PaperPilot 双人协作开发工作流

## 分支策略
```
main      ← 稳定版本，仅打 tag
develop   ← 日常开发主干
  ↑
feature/* ← 每人/每任务独立分支
hotfix/*  ← 紧急修复分支
```
命名：`feature/模块-简述`，如 `feature/faiss-index`

## 标准开发流程

### 1. 开工同步
git checkout develop
git pull origin develop
git checkout -b feature/xxx

### 2. 开发（Claude Code 分工建议）
- A：数据层+算法层（FAISS/SQLAlchemy/KeyBERT/Cross-Encoder/打分逻辑）
- B：应用层+展示层（Flet UI/雷达图/托盘/打包）
- 复杂算法（打分公式/权重更新）必须人工设计，Claude 仅辅助编码
- Claude 修改前先用 `git status` 确认，关键文件手动备份

### 3. 提交
git add .
git commit -m "feat: FAISS向量索引构建

- IndexFlatIP索引
- DeepSeek/MiniLM双源Embedding
- diskcache缓存

Closes #3"

### 4. 合并前同步 develop（防冲突）
git checkout develop
git pull origin develop
git checkout feature/xxx
git rebase develop        # 冲突少时用rebase；冲突频繁改用merge
# 解决冲突后
git push -f origin feature/xxx

### 5. 合并到 develop
GitHub 发起 PR：`feature/xxx → develop`
搭档 Code Review 后合并，选 "Create a merge commit"
（信任度高时可本地 `git merge --no-ff`）

### 6. 清理
git branch -d feature/xxx
git push origin --delete feature/xxx

## 冲突预防
- 每日站会（5min）：同步今天要改的文件清单
- 数据库模型变更：必须先发PR合并，再改业务代码
- 配置抽离到 `config.yaml` 或环境变量，不硬编码提交

## 提交规范
```
类型: 简述（50字内）

- 改动点1
- 改动点2

Closes #Issue编号
```
类型：`feat` `fix` `refactor` `docs` `test` `chore`

## Claude Code 守则
1. 每人独立会话，不共享上下文
2. Claude 生成代码需另一人快速 review 后合并
3. 关键算法人工设计伪代码，Claude 仅实现
4. 大规模重构前先 `git commit` 快照
