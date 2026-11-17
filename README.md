# AI 研发数字员工系统 (Dependable Engineering Delegation System v2.0)

**一句话定位**：一个**零业务代码侵入、可跨仓库复用、可独立容器化部署**的可信研发委托服务。  
它将自然语言群聊/工单请求转化为具备明确边界的**任务契约（`TaskContract`）**，在独立沙箱 `git worktree` 中完成交互式编码与调试，通过硬性回归测试证据门禁（**`Fail-to-Pass` + 反作弊扫描 + 单审查器盲区披露**）后自动推送 `ai-agent/*` 分支并创建 **GitHub Draft PR**，同时持续监听 PR 评论与线上 CI 状态完成下半场跟进。

完整架构设计见 [docs/design_spec.md](docs/design_spec.md)。

---

## 1. 核心特性一览 (Core Capabilities)

| 模块能力 | 说明 |
| :--- | :--- |
| **1. 一键接入任意仓库 (`Auto-Onboarding`)** | 支持网页表单、REST API 或 `de onboard <git_url_or_path>` 一键挂载本地或远端 Git 仓库，自动识别 Python / Node / Go 技术栈与测试命令，**对目标业务仓库零代码侵入**。 |
| **2. 契约先行与单模块自动切片 (`TaskContract`)** | 改代码前先读取目标仓代码结构与 `.ai/DECISIONS.yaml` 起草契约；遇到笼统大包干请求自动切分为原子级可审阅模块；仅在存在实质业务歧义时发起 **5 秒微确认**。 |
| **3. 权威事实层级与组织决策账本 (`DecisionLedger`)** | 严格遵循 `主干代码与单测 (100%) > .ai/DECISIONS.yaml 正式策略 (95%) > 近期合入 PR (85%) > 任务指令 (80%) > 旧文档 (仅供参考)`；支持在网页一键将 Review 结论沉淀为长期团队规则。 |
| **4. 自研白盒 Agent 内核 (`digital_employee.kernel`)** | 双态运行（`de code` 终端 TUI/Headless + 沙箱无人值守）：流式只读并发/写串行分批调度（`StreamingExecutor`）、双层上下文治理（L1 溢写落盘 + L2 锚定摘要与 `RecoveryState` 恢复）、缓存感知三态 MCP（`Eager`/`Native`/`Dispatch`）、`Explore` 子智能体与双层记忆、以及契约级 OS 内核沙箱（macOS `Seatbelt` / Linux `Bubblewrap`）+ 会话断点挂起/恢复。 |
| **5. 7×24 运维告警排障与多语言门禁 (`Dev + QA + Ops`)** | 内置 `/webhooks/alert` 告警入口、`ops-incident-triage` 技能与 `ops-diagnostics` MCP 诊断服务；门禁强制执行多语言 **`Fail-to-Pass`**（Python/Node/Go）、**测试反作弊扫描**与单审查器盲区披露。 |
| **6. 免公网 IP 下半场自动跟进 (`Follow-Through Poller`)** | 内置后台 GitHub 轮询器（每 30 秒巡检）与 Webhook 双通道：在 GitHub PR 页面直接留评论或遇线上 CI 失败时，自动唤醒原沙箱追改并推送新 Commit；人类合并 PR 后自动回写 `ACCEPTED_AND_CLOSED` 并核算净节省工时。 |
| **7. 物理级安全边界 & 缓存感知计费** | 代码层硬拦截向 `main`/`master`/`release` 推送及修改 `.github/workflows/`、`.env`；公有云推理强制经过 DLP 密钥脱敏与 `<untrusted_input>` 防提示词注入隔离；内置 KV-Cache 命中折扣精确计费与任务预算熔断，单次常规修复成本约 `$0.02~$0.06`。 |

---

## 2. 系统架构与部署拓扑

数字员工本体作为**独立服务**运行，可同时挂载并服务多个业务代码库：

```mermaid
flowchart LR
    subgraph Entry["接入与控制面"]
        WebUI["Web 控制台 (:8020)"]
        CLI["`de` CLI"]
        Feishu["飞书卡片 / WS 长连接"]
        GHPoller["GitHub PR 评论 & CI 轮询器"]
    end

    subgraph Core["AI 数字员工独立服务 (digital-employee)"]
        Drafter["契约起草与自动切片器"]
        Store["SQLite WAL 持久化状态 & 幂等账本"]
        Sandbox["隔离 Git Worktree 沙箱循环"]
        Gate["Fail-to-Pass + 反作弊门禁 + 单审查器"]
    end

    subgraph Targets["被服务的业务仓库 (零代码侵入)"]
        RepoA["业务仓 A (.ai/DECISIONS.yaml)"]
        RepoB["业务仓 B (.ai/DECISIONS.yaml)"]
        RepoN["任意新接入仓库 N"]
    end

    Entry <--> Core
    Core <--> Targets
```

---

## 3. 快速启动 (3 种运行方式)

### 方式一：本地源码一键启动（个人日常推荐）

```bash
# 1. 安装依赖（需 Python 3.12+ 与 uv）
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"

# 2. 复制并填写配置
cp .env.example .env
# 编辑 .env 填入 DE_LLM_API_KEY（如 DeepSeek / OpenAI）与 DE_GITHUB_TOKEN

# 3. 后台启动 Web 控制台与 PR 自动跟进轮询器
./dectl.sh start
# 查看运行状态 / 日志 / 停止
./dectl.sh status
./dectl.sh logs
./dectl.sh stop
```

启动后浏览器打开 **http://127.0.0.1:8020** 即可使用可视化工作台。

---

### 方式二：Docker Compose 独立微服务部署（团队共享 / 私有化交付）

无需在宿主机安装 Python 环境，一条命令拉起常驻服务：

```bash
cp .env.example .env
# 编辑 .env 填入模型 API Key、GitHub Token（及可选飞书配置）

docker compose up -d --build
curl http://127.0.0.1:8020/api/health
```

所有持久化状态库（`state.db`）、受管克隆仓库（`repos/`）、沙箱工作树（`worktrees/`）与验证日志产物（`artifacts/`）均安全保存在持久化卷 `digital-employee-data`（容器内 `/data`）中。

---

### 方式三：纯命令行极客模式 (`de` CLI)

```bash
# 1. 一键接入任意新仓库（支持 GitHub owner/repo、SSH/HTTPS URL 或本地路径）
.venv/bin/de onboard JamesYoungZZ/MODULAR-RAG-MCP-SERVER --id modular-rag --branch dev
.venv/bin/de onboard /path/to/your/local-repo

# 2. 查看已接入仓库与生效中的团队决策规则数
.venv/bin/de repos

# 3. 一句话发起研发委托（验证通过自动推分支并建 GitHub Draft PR）
.venv/bin/de delegate --repo modular-rag --publish-pr \
  "修复 tests/unit/test_list_collections.py 中 include_stats 参数透传失败的 Bug"

# 4. 查看任务队列、证据报告与代码补丁
.venv/bin/de list
.venv/bin/de show <task_id>

# 5. 5 秒微确认 / 追加评论修改 / 状态控制 / 度量漏斗报表
.venv/bin/de clarify <task_id> --use-defaults --publish-pr
.venv/bin/de followup <task_id> "再补一个空列表边界断言" --publish-pr
.venv/bin/de control <task_id> accept
.venv/bin/de metrics
```

---

## 4. 外部平台配置指南

### 4.1 GitHub Token 配置（自动提 Draft PR + 评论自动追改）
1. 前往 `GitHub Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens`；
2. **Repository access**：勾选需要托管的目标业务仓库；
3. **Repository permissions**（必须在同一次保存中添加）：
   - `Contents`：`Read and write`
   - `Pull requests`：`Read and write`
4. 将生成的 Token 填入 `.env` 的 `DE_GITHUB_TOKEN=`。

### 4.2 飞书接入（两种免公网方案任选）
- **方案 A（最快通知卡）**：在飞书群添加“自定义机器人”，将 Webhook 填入 `DE_FEISHU_WEBHOOK_URL=`，每次需微确认、过门禁提 PR 或交接时自动推送飞书交互卡片。
- **方案 B（手机双向派活，免公网 IP）**：安装可选依赖 `uv pip install lark-oapi`，在飞书开放平台创建企业自建应用并开启“长连接接收事件（`im.message.receive_v1`）”，将 `DE_FEISHU_APP_ID` 与 `DE_FEISHU_APP_SECRET` 填入 `.env` 重启服务即可。

---

## 5. REST API 接口速查（供其他内部系统集成调用）

| 方法与路径 | 用途 |
| :--- | :--- |
| `GET /api/health` | 服务健康检查、推理数据流模式、GitHub 轮询器与已注册仓库状态 |
| `GET /api/metrics` | 委托转化漏斗、累计成本与净节省人工分钟数（`Net Human Time Saved`） |
| `GET /api/repositories` | 查询所有已接入的目标代码仓库配置 |
| `POST /api/repositories/onboard` | 动态接入新仓库（自动 Clone、探测测试命令并持久化注册） |
| `GET /api/tasks` | 查询任务队列（支持 `?status=PR_OPEN_FOLLOWING_UP` 过滤） |
| `POST /api/tasks` | 异步创建新委托任务（同步返回 `TaskContract` 与待确认问题） |
| `GET /api/tasks/{task_id}` | 获取任务完整状态、回归验证证据与持久化 Checkpoint 历史 |
| `POST /api/tasks/{task_id}/clarify` | 提交微确认答案并唤醒后台沙箱执行 |
| `POST /api/tasks/{task_id}/followup` | 提交增量追改指令并自动更新远端 Draft PR |
| `POST /api/tasks/{task_id}/control` | 执行 `accept` / `abandon` / `cancel`（生成 Handoff 包）/ `publish_pr` |
| `POST /webhooks/feishu` | 飞书事件回调入口 |
| `POST /webhooks/github` | GitHub PR Review / Issue Comment Webhook 实时入口 |

---

## 6. 开发与测试验证

```bash
# 静态代码检查
.venv/bin/ruff check src tests

# 运行完整安全护栏、持久化幂等、反作弊与 Fail-to-Pass 门禁单测套件
.venv/bin/pytest -v
```
