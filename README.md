# AI 研发数字员工系统 (AI Digital Employee v3.0)

**一句话定位**：基于自研白盒编码内核（`mewcode` / `digital_employee.kernel`）构建的**零业务侵入、跨仓库复用、飞书免公网双向受控**的异步研发委托与 7×24 运维排障系统。

它将一句自然语言需求、线上告警或业务隐患转化为边界明确的 **`TaskContract`**，在隔离 `git worktree` 中通过白盒 Agent 内核自主探索、编码与验证，经由**确定性 `Fail-to-Pass` 门禁 + 跨模型独立 Code Review + 线上 GitHub Actions CI** 后，向你的**飞书与 Web 工作台**推送交互卡片——**点一下「🔧 一键自动修复」完成返修，点一下「✅ 批准合入」自动 Squash Merge 入主干**。

完整架构规范见 [docs/design_spec.md](docs/design_spec.md)。

---

## 1. 核心能力矩阵

| 模块 | 核心能力 |
| :--- | :--- |
| **1. 自研白盒 Agent 内核 (`mewcode`)** | 内置 `digital_employee.kernel`（支持 `de code` 终端 TUI / Headless / Remote Web UI 与无人值守沙箱双态）：只读并发/写串行流式调度（`StreamingExecutor`）、L1 溢写 + L2 锚定压缩、`Explore`/`Verification` 子智能体、三态缓存感知 MCP、有状态断点挂起/恢复（`kernel_session.jsonl`）与契约级/OS 级沙箱（`Seatbelt`/`Bubblewrap`）。 |
| **2. 实时内核直播流 & 断线自愈** | 打破后台黑盒：Web 工作台每 `1.8s` 实时直播每一步内核推理、代码读写与终端测试输出；内置线程存活探针与服务重启孤儿任务自动断点续跑。 |
| **3. 多语言证据门禁 & `No-Op` 防偷懒校验** | 自动创建脱离主干基线快照执行 Python / Node / Go **`Fail-to-Pass`** 验证，静态拦截删 `assert` / 加 `@skip` 作弊；若基线已满足条件则经独立复测后干净走 `No-Op` 结单。 |
| **4. 跨模型独立 Code Review & 全量报告** | 支持异构模型互审（如 DeepSeek 编码 + Gemini 3.8 Flash / Claude 审查），遵循 `skills/pr-code-review/SKILL.md` 仅报 `P0`/`P1` 阻断缺陷，自动生成人话概览、GitHub 行级评论与 `/reports/{id}/review` 独立五维全量报告页。 |
| **5. 飞书免公网双长连接受控闭环** | 无需云服务器与内网穿透，基于 `lark-oapi` 独立事件循环同时承载**收发消息**与**卡片按钮回调**：`BLOCK` 或有新 PR 评论时推《🔧 一键自动修复卡》，Review 与线上 CI 全绿后推《✅ 批准合入卡》（带 SHA 防篡改锁 Squash Merge）。 |
| **6. 7×24 运维告警诊断 (`Ops MCP`)** | 内置 `/webhooks/alert` 告警接入、`ops-incident-triage` 技能与 `ops-diagnostics` 只读诊断 MCP 服务（近期变更排查、DLP 脱敏日志检索、HTTP 探活）。 |

---

## 2. 快速启动

### 2.1 本地一键启动（Web 工作台 + 飞书长连接 + GitHub 跟单轮询器）

```bash
# 1. 创建虚拟环境并安装依赖（Python 3.12+ & uv）
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 2. 配置环境变量
cp .env.example .env
# 在 .env 中填入 DE_LLM_API_KEY 与 DE_GITHUB_TOKEN（飞书与跨模型 Reviewer 可直接在网页控制台可视化配置）

# 3. 启动常驻后台服务
./dectl.sh start
./dectl.sh status
```

打开 **http://127.0.0.1:8020** 即可进入可视化工作台。

### 2.2 命令行与交互式内核模式 (`de` CLI)

```bash
# 接入本地或远端 GitHub 仓库（零业务代码侵入，配置保存在 .local/repositories.yaml）
.venv/bin/de onboard /path/to/repo --id my-repo --branch main

# 直接拉起 mewcode 交互式终端编程内核（支持 TUI / -p Headless / --remote 网页端）
.venv/bin/de code
.venv/bin/de code -p "分析当前仓库架构" --output-format stream-json

# 命令行发起异步委托任务 / 查看队列与度量漏斗
.venv/bin/de delegate --repo my-repo --publish-pr "在 reply_safety.py 中新增微信号公屏隐私拦截"
.venv/bin/de list
.venv/bin/de metrics
```

### 2.3 Docker Compose 容器化部署

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8020/api/health
```

---

## 3. 主要接口速查

| 路径 | 说明 |
| :--- | :--- |
| `GET /api/health` | 运行内核模式（`kernel`）、主模型/审查模型、飞书长连接与仓库状态 |
| `GET /api/tasks` & `POST /api/tasks` | 查询队列（含实时 `worker_alive` 与 `current_activity`）/ 创建委托任务 |
| `GET /api/tasks/{id}` | 任务契约、实时内核事件流（`live_events`）、证据门禁与飞书卡片预览 |
| `POST /api/tasks/{id}/control` | 受控动作：`approve_merge` / `autofix_review` / `retry` / `cancel` / `abandon` |
| `GET /reports/{id}/review` | 独立可分享的全量五维 Code Review 审查报告与逐行 Diff 页面 |
| `POST /webhooks/alert` | 7×24 生产告警自动排障与热修复委托入口 |
| `POST /webhooks/feishu` & `/webhooks/github` | 飞书与 GitHub Webhook 回调入口（配合免公网长连接/Poller 双通道） |

---

## 4. 开发与验证

```bash
# 静态检查（0 警告）与全量单元测试（720+ 内核与编排闭环测试）
.venv/bin/ruff check src tests
.venv/bin/pytest -q

# 前端控制台构建（如修改了 frontend/）
cd frontend && npm install && npm run build
```
