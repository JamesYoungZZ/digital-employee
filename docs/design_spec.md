# AI 研发数字员工系统 — 架构设计与技术规范 (Design & Spec v3.0 · White-Box Kernel Edition)

**文档版本**：v3.0.0（Clean Start 基线：集成 `mewcode` 自研白盒 Agent 内核）  
**定位**：零业务代码侵入、跨仓库可复用、支持飞书免公网双向交互的**可信研发委托与 7×24 故障自愈系统**。

---

## 1. 核心架构分层 (System Architecture v3.0)

```mermaid
flowchart TB
    subgraph L1["1. 交互与接入层 (Bidirectional Control Plane)"]
        WebUI["React 19 现代工作台 (:8020 · 实时内核直播流 + 独立审查报告页)"]
        FeishuWS["飞书双长连接免公网通道 (收消息 im.message + 点卡片回调 card.action)"]
        CLITUI["`de` CLI & `de code` 终端 TUI / Headless / Remote UI"]
        Webhooks["GitHub 15s 跟单轮询器 & `/webhooks/alert` 运维告警入口"]
    end

    subgraph L2["2. 契约编排与持久化层 (Contract & Durable Orchestrator)"]
        Drafter["ContractDrafter (事实优先级路由 + 单模块原子切片)"]
        Store["SQLite WAL 状态机 & 外部动作幂等账本 & 断线自愈器"]
        LiveStream["JSONL 实时步骤直播流 (`live_stream.jsonl`)"]
    end

    subgraph L3["3. 自研白盒 Agent 内核 (`digital_employee.kernel` / `mewcode`)"]
        Exec["StreamingExecutor (只读并发 / 写串行分批调度)"]
        Ctx["双层上下文治理 (L1 超长溢写落盘 + L2 锚定摘要压缩 + RecoveryState)"]
        Session["有状态断点挂起与无损恢复 (`kernel_session.jsonl`)"]
        SubMCP["子智能体 (`explore`/`verification`) + 三态缓存感知 MCP + 双域记忆 (`MEMORY.md`)"]
        Sandbox["契约级路径沙箱 (`ContractPathSandbox`) + OS 内核沙箱 (`Seatbelt`/`Bubblewrap`)"]
    end

    subgraph L4["4. 证据门禁与跨模型审查层 (Verification & Approval Gate)"]
        F2P["多语言确定性 `Fail-to-Pass` 基线对比 (Python / Node / Go) + `No-Op` 校验"]
        AntiTamper["测试反作弊静态扫描 (拦截删 `assert` / 加 `skip` / 吞异常)"]
        CrossRev["跨模型独立 PR Reviewer (`pr-code-review` Skill · 五维核查矩阵 + `/reports/{id}/review`)"]
        GateMerge["双卡受控闭环：`BLOCK` 推一键自动修复卡 ｜ `APPROVE + CI 全绿` 推合入审批卡"]
    end

    L1 <--> L2
    L2 <--> L3
    L3 --> L4
    L4 --> L1
```

---

## 2. 四大核心子系统技术规范

### 2.1 契约先行与 Jira 权威工单双路径 (`JiraTicketDraft` & `TaskContract`)
1. **事实权威层级（Truth Hierarchy）**：
   `目标仓主干代码与测试 (100%) > 已审批绑定的真实 Jira 工单 (98%) > .ai/DECISIONS.yaml 生效团队策略 (95%) > 原始口头委托 (80%)`。
2. **双路径 Jira 强治理闭环**：
   - **已有 Jira 直接开工**：传入 `PROJ-123` / `browse/PROJ-123` 或命中 `JiraIssuePoller`（`labels = "ai-delegate"`）时，跳过建卡审批，实时拉取 Jira 标题、正文（ADF/Markdown）与评论构建契约，流转工单至 `In Progress` 并直接进沙箱执行。
   - **新需求建卡先审后做**：无现成工单时先进入 `WAITING_JIRA_APPROVAL`，向飞书与控制台推送《📋 待你审核 Jira 工单》交互卡；经人工审核（可在线改标题/描述/验收标准或飞书回 `建单`）在真实 Jira 建卡后，将 `TaskContract` 锁定对齐至该工单（支持 `sync_contract_from_jira` 动态同步更新、PR 标题自动带 `[PROJ-123]`、提 PR 流转 `In Review`、合入流转 `Done`）。
3. **原子化切片与简洁中文契约**：
   开工前先探测目标仓目录结构与真实测试健康度，自动生成单模块 `TaskContract`；当目标条件在基线已 100% 满足时支持防偷懒双重校验的 **`No-Op` 正常结单**。

### 2.2 自研白盒编码内核 (`NativeKernelRuntime` / `mewcode`)
已彻底淘汰早期的简陋 Raw Tool Loop，统一由 `src/digital_employee/kernel/` 驱动沙箱执行：
1. **流式并发调度**：只读工具（`ReadFile`, `Grep`, `Glob`）自动并发批处理，修改与命令工具（`EditFile`, `WriteFile`, `Bash`）严格串行执行并记录 `FileHistory` 快照支持回滚。
2. **上下文与长程记忆**：
   - 单次工具输出超 `50,000` 字符自动溢写落盘并在上下文保留结构化预览（L1）；
   - 逼近模型上下文窗口时自动执行保留尾部工具配对的锚定摘要压缩（L2）；
   - 支持用户级（`~/.de/memory/`）与仓库级（`<repo>/.de/memory/`）独立 `.md` + `MEMORY.md` 索引记忆。
3. **有状态挂起/恢复（Stateful Pause & Resume）**：
   当内核调用 `request_clarification` 时，完整序列化当前推理历史与压缩边界至 `kernel_session.jsonl`；人类在飞书或控制台拍板后，直接从原会话断点无损注入决策继续执行，无需重头探测。
4. **缓存感知三态 MCP 与 7×24 运维排障**：
   内置 `ops-diagnostics` 只读诊断 MCP（近期提交排查、日志脱敏检索、HTTP 健康探针）与 `ops-incident-triage` 技能，根据工具规模自动决策 `Eager` / `Native` / `Dispatch` 加载模式以保护 KV-Cache 前缀命中率。

### 2.3 跨模型独立 Code Review 与双入口全量报告
1. **独立模型隔离**：支持配置异构模型（如 `deepseek-v4-pro` 编码 + `gemini-3.8-flash` / `claude-sonnet` 审查），加载 `skills/pr-code-review/SKILL.md` 零噪音审查协议（屏蔽格式/命名吹毛求疵，仅抓 `P0`/`P1` 实质缺陷）。
2. **双层透明报告**：
   - GitHub PR 评论包含「本次改动做了什么」人话概览、审查结论、展开式阻断问题（原因 + 建议改法）与 `<details>` 折叠五维矩阵；
   - 独立全量报告页 `/reports/{task_id}/review` 提供完整五维核查表、问题定位与逐行 Diff。

### 2.4 飞书与控制台「双卡受控闭环」（零静默合入）
1. **出现阻断问题或人类新 PR 评论 ➔ 推送《🛑 Review 待修复卡》**：
   点击卡片上的 **`🔧 一键自动修复`**（或飞书回复 `修复`），立即唤醒原沙箱携带审查意见追加提交并重跑复审。
2. **四道门禁全过（`Fail-to-Pass` + 反作弊 + Review `APPROVE` + 线上 GitHub Actions CI 100% 全绿）➔ 推送《🛎️ 待你合入卡》**：
   点击卡片上的 **`✅ 批准合入`**（或飞书回复 `批准`），后端校验 `expected_head_sha` 锁后自动执行 GitHub Squash Merge 并结单为 `ACCEPTED_AND_CLOSED`。
