# PaperPilot Bug 处理工作流

## Bug 分级

| 级别 | 定义 | 响应时间 | 处理方式 |
|------|------|---------|---------|
| P0 致命 | 程序崩溃/核心功能完全不可用 | 立即 | 停下手头工作，全员修 |
| P1 严重 | 功能异常但可绕过/数据错误 | 4h内 | 从develop切hotfix，快速修复 |
| P2 一般 | UI错位/提示错误/性能下降 | 24h内 | 记入Issue，当前feature完成后顺手修 |
| P3 优化 | 体验改进/代码异味 | 排期 | 记入Backlog，集中处理 |

## P0/P1 快速修复流程

### 1. 创建 hotfix 分支
git checkout develop
git pull origin develop
git checkout -b hotfix/简述    # 如 hotfix/api-timeout

### 2. 最小化修复
- 仅改必要文件，不重构不优化
- 修复后本地快速验证（跑通核心流程即可）

### 3. 提交
git add .
git commit -m "fix: 修复API超时导致程序卡死

- 添加requests超时参数(10s)
- 失败时降级到本地MiniLM

Fixes #7"

### 4. 提交 PR 并由搭档 review
将 hotfix 分支推送后创建到 develop 的 PR，附复现步骤与验证结果。搭档 review 后合并，不直接推送 main/develop。

P0 修复还须经审核同步到 main 并打紧急版本 tag；发布与合并由负责人执行。

### 5. 通知搭档
git branch -d hotfix/简述
在群里发：`[FIXED] hotfix/api-timeout 已合并到develop，请pull`

## P2 常规修复流程
- 不单独切分支，在当前 feature 分支顺手修复
- commit 类型用 `fix:`，与 feat 区分开
- 合并时正常走 PR

## Bug 记录模板（GitHub Issue）
```
## 问题描述
一句话概括

## 复现步骤
1. 打开...
2. 点击...
3. 报错...

## 期望行为
...

## 实际行为
...

## 环境
- OS: Windows 11 / macOS 14
- Python: 3.11
- 分支: develop (commit abc123)

## 截图/日志
```

## 自检清单（修复后必做）
- [ ] 修复的问题本地可复现且修复后不再出现
- [ ] 未引入新报错（跑一遍主流程）
- [ ] 搭档已同步最新develop
- [ ] 若为P0/P1，已更新CHANGELOG.md
