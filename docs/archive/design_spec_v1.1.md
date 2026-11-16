# 基于 MCP 与 Multi-Agent 协同的研发数字员工系统 — 架构设计与技术规范 (Design & Spec)

**文档版本**：v1.1.0  
**状态**：Draft / 待 Review  
**一句话定位**：驻扎在企业飞书/企微里的 AI **研发 + 运维 + 测试 + 需求整理**数字员工。它能根据群里模糊的需求、报错、截图或会议纪要，自动建/读工单、查内部文档、出技术方案，并在后台改代码、跑通测试、调独立 Agent 完成 Review，最后把可合入的 PR 或文档推给人确认。

## 核心价值与卖点 (Key Value Propositions)

* **群聊碎片直接转工单与方案**：把飞书/企微群里零散的聊天、报错截图或会议纪要，自动整理成规范的 Jira 工单，并结合内部老文档生成第一版技术方案（Spec）。
* **常规 Bug 与小需求后台闭环**：接单后自己在隔离沙箱里改代码、跑编译和单元测试，报错了看日志自动改，直到测试通过才提 Draft PR，人只需花两分钟看眼 Diff 点确认。
* **独立 Agent 交叉 Review 把关**：写代码的和审代码的分开，由独立的审查 Agent 专门挑逻辑漏洞、安全隐患和规范问题，避免单一模型自己写错自己看不出来。
* **线上告警秒级定位与热修**：报警群一出消息，自动拉错误日志比对最近上线的代码提交，几分钟内给出排查结论并生成回滚或修复 PR，减少半夜人工查日志的时间。
* **上下游接口变更自动同步**：后端改了接口协议，自动识别受影响的前端或客户端仓库，在群里提醒对应负责人，并顺手生成适配新字段的改动 PR。
* **闲时自动清理技术债**：利用夜间自动扫描代码库，帮核心模块补齐缺失的单元测试、修复慢 SQL 或升级过期依赖，不占用白天的正常开发工时。
* **全套内网私有化部署**：直接对接国内团队常用的飞书/企微、Jira/ONES 和自建 GitLab，代码和文档不出公司内网，可按需接入主流云端 API 或本地开源模型。
* **算得清的研发提效与成本回报（ROI）**：花不到半个初级程序员的成本（Token 与部署费用），就能接管团队 30%~40% 的修常规 Bug、写文档、查告警和补单测工作，把核心研发从琐碎活里解放出来，且省下的工时、交付的 PR 数量和拦截的问题在后台全部可量化统计。

## 1. 项目背景与目标 (Background & Goals)

### 1.1 核心痛点
在日常研发流程中，工程师大量时间被碎片化的跨平台协同与重复性编码/文档工作占据：
* **多端上下文割裂**：任务触发在飞书群聊/IM，需求详情与验收标准在 Jira，历史架构规范在飞书云文档/Wiki，代码实现与评审在 GitHub/GitLab，人工来回切换与信息拼装耗时严重。
* **中低复杂度研发任务占比较高**：常规 Bug 修复、CRUD 接口开发、单元测试补齐以及标准技术方案（Spec Design）初稿撰写占据了 40%~60% 的日常工时。
* **单一 AI 编程工具缺乏闭环自检**：直接使用 IDE 插件或 CLI 工具需要人工全程盯屏引导，缺乏“独立第三方视角审查（Cross-Agent Review）”与“沙箱自动化验证（CI/单测自愈）”的端到端交付能力。

### 1.2 核心目标 (Goals)
1. **端到端无人值守交付**：实现从“飞书群消息感知 -> Jira 需求建单/拉取 -> 历史知识库检索 -> 代码修复/Spec 撰写 -> 沙箱单测验证 -> 多智能体交叉 Review -> 提交 GitHub Draft PR / 飞书文档 -> 推送审批通知”的全链路自动化。
2. **高可靠与低幻觉（Maker-Checker 闭环）**：要求所有生成的 PR 必须通过隔离沙箱编译、Linter 检查与单元测试，并通过独立 Reviewer Agent 评审后方可推送给人类开发者。
3. **合规与成本可控（降本增效）**：严格执行最小权限控制（RBAC）与企业数据防泄漏（DLP）规范，通过分级模型路由将单次任务 Token 成本控制在合理阈值内。

### 1.3 非目标 (Non-Goals)
* **不追求 100% 替代人类直接合入主干**：禁止 Agent 直接向 `main` / `master` / `release` 分支 Merge 代码或直接触发生产环境部署，所有产出必须经过 Human-in-the-Loop（人工最终确认）。
* **不违反企业安全红线**：不在个人微信通道传输任何源码片段、内部 Jira 原文或敏感业务数据（个人微信仅做脱敏状态提醒通道）。

## 2. 系统总体架构设计 (System Architecture Design)

系统采用**事件驱动（Event-Driven）+ 状态机编排（LangGraph StateGraph）+ 标准化工具协议（MCP）**的分层架构，共分为五层：

### 2.1 五层架构拓扑

| 架构分层 | 核心组件 | 职责描述 | 技术选型建议 |
| :--- | :--- | :--- | :--- |
| **1. 接入与感知层 (Trigger & Gateway)** | 飞书 Event Webhook、多模态意图解析器、幂等队列 | 监听飞书群 `@提及` 消息、报错截图、妙记与 Jira 事件；过滤群聊噪音，提取或创建工单并投递至异步任务队列。 | FastAPI + Feishu OpenAPI + Redis Queue |
| **2. 状态编排与中枢层 (Orchestrator)** | LangGraph 状态机、路由器、预算控制器 | 维护全局任务状态 `AgentTaskState`，负责节点流转、异常重试、循环终止判断（Max Retry）及分级模型调用。 | LangGraph / Python Asyncio + LiteLLM |
| **3. 标准化上下文层 (MCP Context Layer)** | Jira MCP、Feishu Doc/Wiki MCP、GitHub MCP、Codebase RAG | 通过统一 Model Context Protocol 协议对外屏蔽 API 差异，提供工单读写、历史技术文档检索、AST 代码树检索与 PR 操作能力。 | Official MCP Python SDK + Tree-sitter + VectorDB |
| **4. 专职智能体执行层 (Execution & Review Agents)** | PMO Agent、Coding Agent (Codex)、Spec Writer、Sandbox Runner、MoE Review Panel | 分工完成需求拆解、代码编写、容器沙箱单测自愈、技术文档撰写，以及安全/性能/规范多维度交叉审查。 | OpenAI Codex / Claude 3.7 Sonnet + Docker Sandbox |
| **5. 人机协同与触达层 (HITL & Notification)** | 飞书交互式卡片服务、微信脱敏通知通道、偏好反馈收集器 | 向开发者推送含 Diff 摘要与 Review 报告的飞书审批卡片（支持一键 Merge/打回重写），并记录人工修改 Diff 反哺知识库。 | 飞书消息卡片 (CardKit) + 企业微信/服务号模板消息 |

### 2.2 端到端数据流向 (End-to-End Workflow)
1. **事件触发**：飞书研发群中发送 `@数字员工 请跟进修复 PROJ-1024 订单状态回滚异常`（或直接发报错截图/会议纪要），飞书网关接收事件并通过 Redis `event_id` 去重。
2. **意图识别与前置确认**：轻量级 Triage 模型解析消息意图、提取或创建 Jira 工单；若置信度高于阈值（或用户在飞书卡片点击“确认执行”），正式初始化状态机。
3. **全量上下文组装 (MCP Hydrate)**：并发调用 `jira-mcp` 获取工单描述与复现步骤，调用 `feishu-mcp` 检索关联的订单模块架构文档，调用 `code-rag-mcp` 定位涉及的微服务仓库与核心文件列表（Repo Map）。
4. **任务分流执行 (Dual-Track Execution)**：
   * **Track A（代码开发/Bug 修复）**：Planner 制定修改计划 -> Coding Agent (Codex) 在独立 Git 分支编写代码与单测 -> Sandbox Runner 在 Docker 容器内运行编译与 `pytest`/`go test` -> 若报错则将日志回传 Coding Agent 自愈修复。
   * **Track B（Spec Design 撰写）**：Planner 拉取团队 Spec 模板 -> Spec Writer Agent 结合业务上下文撰写背景、架构图（Mermaid）、接口定义与数据库变更方案。
5. **多智能体交叉评审 (Cross-Agent Review)**：唤醒独立 Prompt 与温度配置的 **Reviewer Agent Panel**（逻辑完整性、安全漏洞、代码规范/文档规范），输出结构化评分与修改意见；若结果为 `REJECTED` 且 `retry_count < 3`，打回执行层自动修正。
6. **产物交付与推送审批**：评审通过后，调用 `github-mcp` 创建 **Draft PR**（或调用 `feishu-mcp` 创建云文档），将可点击链接、变更摘要、单测结果与 Review 报告封装为飞书交互卡片推给本人，同时通过微信推送一行脱敏提醒。

## 3. 核心技术规范 (Technical Specification)

### 3.1 全局状态机 Schema 定义 (`AgentTaskState`)

编排引擎使用强类型状态字典在各个 Agent 节点之间传递上下文，杜绝长链路对话历史导致的 Token 爆炸：

```python
from typing import TypedDict, List, Literal, Optional, Dict, Any

class ReviewComment(TypedDict):
    file_path: str
    line_number: Optional[int]
    severity: Literal["BLOCKER", "WARNING", "SUGGESTION"]
    category: Literal["SECURITY", "PERFORMANCE", "LOGIC", "STYLE"]
    comment: str

class ReviewReport(TypedDict):
    decision: Literal["APPROVED", "REJECTED"]
    score: int  # 0 - 100
    summary: str
    comments: List[ReviewComment]

class AgentTaskState(TypedDict):
    # 1. 基础标识与追踪
    task_id: str
    trace_id: str
    source_channel: Literal["feishu_group", "feishu_dm", "jira_webhook"]
    trigger_message_id: str
    requester_open_id: str
    
    # 2. 需求与上下文数据
    task_type: Literal["BUG_FIX", "FEATURE_DEV", "SPEC_DESIGN", "ONCALL_DIAGNOSIS", "UNKNOWN"]
    jira_key: str
    jira_context: Dict[str, Any]         # 标题、描述、验收标准(AC)、优先级、评论
    related_wiki_docs: List[str]         # 检索到的关联架构文档摘要
    target_repo: str
    target_branch: str
    repo_map_snippets: Dict[str, str]    # AST 检索出的相关源码文件及签名
    
    # 3. 执行过程产物
    implementation_plan: str             # Planner 产出的分步执行计划
    git_patch_diff: Optional[str]        # Coding Agent 生成的 Git Diff
    spec_markdown_content: Optional[str] # Spec Writer 生成的设计文档内容
    sandbox_test_passed: bool            # 沙箱编译与单测是否通过
    sandbox_exec_logs: str               # 沙箱标准输出/错误日志（截断后）
    
    # 4. 审查与控制流指标
    review_report: Optional[ReviewReport]
    retry_count: int                     # 当前打回重写次数（上限默认 3 次）
    token_consumed: int                  # 累计消耗 Token 数
    cost_usd: float                      # 累计消耗成本
    
    # 5. 最终交付产物
    deliverable_url: Optional[str]       # GitHub Draft PR URL 或 飞书云文档 URL
    status: Literal["PENDING_CONFIRM", "RUNNING", "AWAITING_HUMAN_APPROVAL", "COMPLETED", "ESCALATED_FAILED"]
```

### 3.2 MCP Server 接口契约规范 (MCP Interfaces)

| MCP Server | Tool 名称 | 输入参数 (`Input Schema`) | 输出内容 (`Output`) | 权限级别 |
| :--- | :--- | :--- | :--- | :--- |
| **`jira-mcp`** | `get_or_create_issue` | `issue_key: Optional[str]`, `summary: str`, `description: str` | 工单详情 JSON（Summary, Description, AC, Status, Comments） | Read/Write |
| **`jira-mcp`** | `add_issue_comment` | `issue_key: str`, `comment_body: str` | 评论 ID 与状态（用于回写 PR 链接与进度） | Write (受限) |
| **`feishu-mcp`** | `search_wiki_and_docs` | `query: str`, `top_k: int` | 匹配度最高的内部技术规范、历史 Spec 文档片段及 URL | Read-Only |
| **`feishu-mcp`** | `create_spec_doc` | `title: str`, `folder_token: str`, `markdown: str` | 创建成功的飞书云文档 `doc_url` 与 `doc_token` | Write |
| **`github-mcp`** | `search_code_ast` | `repo: str`, `symbols: List[str]`, `keywords: str` | 基于 Tree-sitter 解析的函数签名、调用链及文件相对路径 | Read-Only |
| **`github-mcp`** | `create_draft_pr` | `repo: str`, `base: str`, `head: str`, `title: str`, `body: str` | GitHub Draft PR 编号与 `html_url`（硬性锁定 `draft=True`） | Write (Branch) |

### 3.3 双轨执行工作流规范 (Execution Specs)

* **轨道一：Bug 修复与 Feature 开发 (Codex + Sandbox Loop)**
  1. **工作区隔离**：每次任务启动时，基于目标仓库最新基线分支 `git worktree add` 创建独立临时目录，分支命名规范为 `ai-agent/{jira_key}-{short_hash}`。
  2. **上下文精准注入**：通过 `search_code_ast` 生成精简版 **Repo Map**（仅包含目录结构、类/函数定义签名及核心关联文件源码），避免全量喂入代码库。
  3. **沙箱验证硬性门禁**：Coding Agent 修改代码后，自动在隔离 Docker 容器中执行 `Linter 检查 -> 增量编译 -> 关联单元测试`。若单测失败，提取失败堆栈回传给 Coding Agent 自愈修复；连续 3 次未通过则中止提 PR，转交人工。
* **轨道二：Spec Design 自动化撰写 (Spec Writer Workflow)**
  1. **标准模板对齐**：按照团队标准 Technical Spec 结构输出（背景与问题定义、目标与非目标、架构设计与 Mermaid 时序图、接口协议与 DB 变更、边界异常与灰度计划）。
  2. **个人风格 Few-Shot 注入**：从向量库中检索本人过往编写的 2~3 篇 Spec 文档段落作为 Style Reference，保持术语习惯与行文逻辑一致。

### 3.4 多智能体交叉评审规范 (Multi-Agent Review Panel Spec)

Reviewer Agent 遵循**异构模型或独立 System Prompt 隔离**原则：
* **Logic & AC Reviewer**：逐条核对 Jira 验收标准是否在代码 Diff 或 Spec 章节中全部闭环，检查空指针、并发竞态、事务边界。
* **Security & Compliance Reviewer**：扫描是否存在硬编码密钥、SQL 拼接注入、越权访问接口、敏感日志打印。
* **Style & Maintainability Reviewer**：检查函数圈复杂度、命名规范、是否补充了对应的单元测试用例。
* **准出判定公式**：仅当 `BLOCKER == 0` 且综合评分 `score >= 85` 时，状态置为 `APPROVED`；否则汇总 `ReviewComment` 列表打回给执行 Agent。

## 4. 安全合规、幂等容错与成本控制 (Reliability & FinOps)

1. **权限安全红线 (Security Guardrails)**：GitHub Token 仅授予创建 `ai-agent/*` 分支及提交 Draft PR 权限，物理禁止 `merge`、`force push` 到受保护分支；沙箱容器禁止访问生产数据库。
2. **幂等与防死循环 (Idempotency & Circuit Breaker)**：基于飞书 `event_id` + `jira_key` 设置 Redis 分布式锁；硬性约束 `max_sandbox_retries = 3`、`max_review_loops = 3`、`max_cost_per_task = $1.50`，超限立即熔断转交人工。
3. **分级模型路由降本 (Model Tiering)**：群聊意图分类与日志提取使用轻量高速模型（占 70% 请求量）；核心代码生成与复杂逻辑 Review 使用旗舰编程与推理模型。

## 5. 产品版本演进矩阵 (Product Version Matrix: V1.0 - V6.0)

| 版本号 | 版本代号与核心卖点 (Key Selling Point) | 核心新增能力与技术动作 | 验收指标与业务 ROI (DoD) |
| :--- | :--- | :--- | :--- |
| **V1.0** | **基座版：端到端研发交付闭环** | 打通飞书 `@` 监听 + Jira 建单/拉取 + Codex 沙箱编码/单测自愈 + Spec 撰写 + 多维 Reviewer Agent + GitHub Draft PR 与飞书卡片。 | 常规 Bug 修复与 Spec 初稿撰写耗时从 2h 缩短至 15min，PR 一次编译与单测通过率 > 85%。 |
| **V2.0** | **卖点 1：线上告警秒级定位与热修** | 接入 Prometheus / Sentry / ELK / 飞书告警群；告警触发时拉取报错堆栈 + 比对最近 2 小时上线 Commit，自动生成回滚或修复 PR。 | 线上故障平均定位时间 (MTTR) 从 30min 降至 3min 以内，大幅减少夜间人工查日志时间。 |
| **V3.0** | **卖点 2：上下游接口变更自动同步** | 自动解析 Protobuf / OpenAPI / Swagger 变更 Diff；识别受影响的前端/客户端仓库，在飞书提醒对应负责人并生成适配新字段的 PR。 | 跨端接口变更漏通知率降至 0%，前后端联调对齐周期缩短 50%。 |
| **V4.0** | **卖点 3：闲时自动清理技术债** | 引入夜间 Cron 主动巡检引擎；利用闲时算力扫描慢 SQL、过期依赖、单测覆盖率低于 60% 的核心模块，每早静默产出 1~2 个补单测/重构 PR。 | 核心仓库单测覆盖率稳步提升，历史技术债清理不占用白天的正常开发工时。 |
| **V5.0** | **卖点 4：会议纪要直接转工单与方案** | 接入飞书妙记（Minutes）MCP；从评审会录音转写中提取关联 Action Items，自动在 Jira 建子任务并同步生成 Spec 初稿或代码骨架。 | 实现“散会即出初稿”，消除会后整理纪要、建卡、启动任务的等待延迟。 |
| **V6.0** | **卖点 5：研发效能与 ROI 量化统计** | 自动聚合周期内所有 Jira 闭环、PR 交付、Review 拦截、告警排查与技术债清理数据，生成团队效能看板与个人量化双周报/OKR 总结。 | 周报整理耗时归零，省下的工时、交付的 PR 数量和 Token 投入产出比全部可量化统计。 |

## 6. 双阶段平台化与商业化迁移规划 (Two-Stage Platform & Commercialization)

### 6.1 Stage 1：大厂内部多租户平台化（Multi-Tenant Internal Platform）
1. **租户配置中心 (`Tenant Config Service`)**：抽离硬编码配置，支持组内研发扫码绑定各自的飞书、Jira Token、GitHub 账号与个人编码/文档风格偏好，克隆各自的专属数字分身。
2. **资源隔离与并发调度 (`Workspace & Quota Isolation`)**：容器沙箱池化，按 `tenant_id + task_id` 动态分配隔离环境并控制单租户每日 Token 预算。
3. **团队效能看板 (`ROI Dashboard`)**：实时统计团队自动修复 Bug 占比、PR 采纳率、节省研发工时与 Token 成本回报率。

### 6.2 Stage 2：外部商业化迁移（面向中小科技企业的私有化“AI 研发数字员工底座”）
1. **本土化连接器矩阵**：无缝对接 飞书/企业微信/钉钉 + Jira/ONES/飞书项目/TAPD + GitHub/GitLab/Gitee/云效。
2. **一小时私有化部署套件**：提供 `Docker Compose` 与 `Helm Chart` 一键安装包，代码、文档与向量库 100% 留在客户内网；支持按需切换主流云端 API 或本地开源模型（DeepSeek / Qwen-Coder）。
3. **企业级权限与审计后台**：提供 RBAC 角色权限管控、敏感代码防泄漏审计日志（DLP）与 CTO 视角的 ROI 效能统计报表。
