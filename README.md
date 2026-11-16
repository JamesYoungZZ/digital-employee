# AI 研发数字员工系统 (Dependable Engineering Delegation System v2.0)

基于 [Design & Spec v2.0](docs/design_spec.md) 构建的首版（Stage 1 + Stage 2 核心基座）实现。
它将碎片化的研发请求转化为**有明确边界的任务契约（`TaskContract`）**，在独立沙箱 `git worktree` 中完成交互式探查与修复，并通过**硬性回归测试证据门禁（`Fail-to-Pass` + 反作弊扫描 + 单审查器盲区披露）**交付低审查负担的代码变更。

---

## 1. 核心架构与目录结构

```text
digital-employee/
├── configs/
│   └── repositories.yaml                  # 已注册目标仓（modular-rag / livestream-copilot）与沙箱配置
├── docs/
│   └── design_spec.md                     # 架构设计与技术规范 v2.0
├── src/digital_employee/
│   ├── models/                            # TaskContract, DurableTaskState, VerificationEvidence, HandoffBundle
│   ├── security/dlp.py                    # DLP 密钥脱敏、不可信输入 XML 隔离、物理分支/目录边界拦截
│   ├── storage/durable_store.py           # SQLite WAL 持久化检查点、外部副作用幂等账本、漏斗指标统计
│   ├── context/decision_ledger.py         # 事实优先级路由 (Truth Hierarchy) 与 .ai/DECISIONS.yaml 加载器
│   ├── sandbox/worktree_runner.py         # 独立 ai-agent/{task_id} Worktree 与脱敏环境变量执行器
│   ├── verification/evidence_gate.py      # Fail-to-Pass 基线/Head 双向验证 + 测试反作弊扫描 + Linter
│   ├── agents/                            # ContractDrafter + SandboxCodingAgent + FocusedReviewer
│   ├── integrations/                      # GitHub Draft PR 幂等对账器 + 飞书交互卡片通知器
│   ├── orchestrator.py                    # 端到端状态编排、自愈循环（上限 3 次）与结构化交接（Handoff）
│   ├── web/app.py                         # 轻量 Web 任务队列控制台 + 飞书/GitHub Webhook 接入点
│   └── cli.py                             # `de` 命令行工具
└── tests/                                 # 安全边界、持久化幂等、反作弊与 Fail-to-Pass 自动化测试
```

---

## 2. 快速上手 (Local Quickstart)

### 查看已接入的目标仓库与决策账本策略
```bash
.venv/bin/de repos
```

### 委托一个真实 Bug 修复任务
```bash
.venv/bin/de delegate --repo modular-rag \
  "Fix the KeyError bug in TraceService.get_stage_timings where extra stage fields are missing under data."
```

常用参数：
- `--draft-only`：仅起草 `TaskContract` 并落盘，不立即进入沙箱改代码
- `--accept-defaults`：若存在轻微歧义，直接采用 `default_assumption` 继续执行
- `--publish-pr`：在配置了 `DE_GITHUB_TOKEN` 时，自动推送 `ai-agent/{task_id}` 分支并创建 **GitHub Draft PR**

### 查看任务队列、证据详情、微确认与评论追改
```bash
# 查看任务队列
.venv/bin/de list

# 查看任务契约、验证证据、盲区披露与 Git Diff
.venv/bin/de show <task_id>

# 5 秒微确认继续执行
.venv/bin/de clarify <task_id> --use-defaults

# 模拟 PR Review 评论追改（唤醒原 worktree 增量修改并重跑证据门禁）
.venv/bin/de followup <task_id> "同时补充空字典输入场景的断言"

# 标记结果采纳 / 中断生成结构化 Handoff 交接包
.venv/bin/de control <task_id> accept
.venv/bin/de control <task_id> cancel --note "需求范围调整"

# 查看漏斗转化率与净节省人工耗时（Net Human Time Saved）
.venv/bin/de metrics
```

### 启动轻量 Web 任务控制台 & Webhook 服务
```bash
.venv/bin/de serve --port 8020
```
浏览器访问 `http://127.0.0.1:8020` 即可可视化查看任务队列、在线发起委托、一键确认歧义、查看 Diff 补丁与持久化 Checkpoint 轨迹。
- 飞书事件回调地址：`POST /webhooks/feishu`
- GitHub PR 评论回调地址：`POST /webhooks/github`

---

## 3. 物理级安全护栏（非 Prompt 约束）

1. **受保护分支硬拦截**：`validate_branch_target` 强制仅允许写入 `ai-agent/*` 分支；向 `main`/`master`/`release` 写入或创建非 Draft PR 在代码层直接抛出 `PolicyViolationError`。
2. **禁止越界篡改**：默认硬编码拦截对 `.github/workflows/`、`.env`、`.ai/DECISIONS.yaml` 及 `TaskContract.permitted_scopes` 之外目录的修改。
3. **沙箱凭据剥离**：沙箱子进程运行 `pytest` / `ruff` 时自动剥离所有 `DE_*`、`DEEPSEEK_*`、`OPENAI_*`、`GITHUB_*`、`FEISHU_*` 密钥环境变量。
4. **反作弊测试校验**：自动扫描测试文件 Diff，严禁通过删除已有 `assert`、添加 `@pytest.mark.skip` / `xfail` 或吞异常来伪造绿灯。
