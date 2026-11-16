// AI Digital Employee v2.0 — Zero-Flicker Control Console Logic
let currentView = "workbench";
let currentFilter = "ALL";
let selectedTaskId = null;
let selectedSubtab = "contract";
let cachedTasks = [];

function showToast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.style.background = isError ? "#dc2626" : "#0f172a";
  el.style.display = "block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { el.style.display = "none"; }, 3600);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function switchView(view) {
  currentView = view;
  document.querySelectorAll(".nav-item").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  ["workbench", "repos", "feishu"].forEach(v => {
    document.getElementById(`view-${v}`).classList.toggle("hidden", v !== view);
  });
  if (view === "repos") loadRepositoriesView();
  if (view === "feishu") loadFeishuConfig();
}

function fillPrompt(text) {
  document.getElementById("del-request").value = text;
  document.getElementById("del-request").focus();
}

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll(".filter-pill").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.filter === f);
  });
  renderTasksTable();
}

async function fetchJson(url, opts = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || `HTTP ${resp.status}`);
  }
  return data;
}

// ==================== HEALTH, METRICS & REPO SELECTOR ====================

async function loadHealthAndMetrics() {
  const [health, metrics, repos] = await Promise.all([
    fetchJson("/api/health"),
    fetchJson("/api/metrics"),
    fetchJson("/api/repositories"),
  ]);

  document.getElementById("kpi-total").textContent = metrics.total_delegated;
  document.getElementById("kpi-passed").textContent = metrics.evidence_gate_passed;
  document.getElementById("kpi-accepted").textContent = metrics.accepted_without_rework;
  document.getElementById("kpi-saved").textContent = `${metrics.estimated_net_human_minutes_saved} min`;
  document.getElementById("kpi-cost").textContent = `$${Number(metrics.total_cost_usd || 0).toFixed(4)}`;

  const hb = document.getElementById("health-box");
  hb.innerHTML = `
    <div><span class="dot ok"></span>推理: <b>${escapeHtml(health.reasoning_model)}</b></div>
    <div><span class="dot ${health.github_poller_enabled ? 'ok' : 'warn'}"></span>GitHub 轮询: <b>${health.github_poller_enabled ? '30s 自动跟进' : '未启用'}</b></div>
    <div><span class="dot ${health.feishu_configured ? 'ok' : 'warn'}"></span>飞书通道: <b>${health.feishu_configured ? '已连通' : '待配置'}</b></div>
  `;

  const sel = document.getElementById("del-repo");
  const prev = sel.value;
  sel.innerHTML = repos.map(r =>
    `<option value="${escapeHtml(r.repo_id)}">${escapeHtml(r.repo_id)} (${escapeHtml(r.default_base_branch)})</option>`
  ).join("");
  if (prev && repos.some(r => r.repo_id === prev)) sel.value = prev;
}

// ==================== WORK QUEUE & INSPECTOR ====================

async function loadTasks() {
  cachedTasks = await fetchJson("/api/tasks?limit=50");
  renderTasksTable();
  if (selectedTaskId) {
    const active = cachedTasks.find(t => t.task_id === selectedTaskId);
    // Auto-refresh inspector only when no input inside inspector is focused
    const focusedTag = document.activeElement ? document.activeElement.tagName : "";
    if (active && !["INPUT", "TEXTAREA"].includes(focusedTag)) {
      await openTaskInspector(selectedTaskId, false);
    }
  }
}

function matchesFilter(t) {
  if (currentFilter === "ALL") return true;
  if (currentFilter === "ACTIVE") {
    return ["DRAFTING_CONTRACT", "QUEUED", "RUNNING_IN_SANDBOX", "VERIFYING_EVIDENCE"].includes(t.lifecycle_status);
  }
  if (currentFilter === "READY") {
    return ["PR_OPEN_FOLLOWING_UP", "DELIVERED_LOCAL_READY"].includes(t.lifecycle_status);
  }
  return t.lifecycle_status === currentFilter;
}

function renderTasksTable() {
  const tbody = document.getElementById("tasks-tbody");
  const filtered = cachedTasks.filter(matchesFilter);
  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:#64748b;padding:26px;">当前筛选下暂无任务</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map(t => {
    const ev = t.verification_evidence;
    const f2p = ev
      ? (ev.repro_test_failed_on_base && ev.repro_test_passed_on_head ? "✅ Fail→Pass" : "❌ 未通过")
      : "—";
    let prCell = "—";
    if (t.deliverable_pr_url) {
      const label = t.deliverable_pr_number ? `PR #${t.deliverable_pr_number} ↗` : "对比建单 ↗";
      prCell = `<a href="${escapeHtml(t.deliverable_pr_url)}" target="_blank" onclick="event.stopPropagation()" style="color:#2563eb;font-weight:600;">${label}</a>`;
    }
    const saved = t.estimated_net_minutes_saved != null ? `${t.estimated_net_minutes_saved}m` : "—";
    const chMap = { web: "💻 Web", cli: "⌨️ CLI", feishu: "💬 飞书", feishu_ws: "💬 飞书WS", feishu_sim: "🧪 飞书沙盒" };
    return `
      <tr class="task-row ${t.task_id === selectedTaskId ? 'selected' : ''}" onclick="openTaskInspector('${escapeHtml(t.task_id)}', true)">
        <td style="font-weight:600;color:#2563eb;">${escapeHtml(t.task_id)}</td>
        <td style="font-size:12px;color:#475569;">${chMap[t.source_channel] || escapeHtml(t.source_channel)}</td>
        <td><code>${escapeHtml(t.contract.target_repo)}</code></td>
        <td>${escapeHtml((t.contract.objective || "").slice(0, 68))}</td>
        <td><span class="badge st-${escapeHtml(t.lifecycle_status)}">${escapeHtml(t.lifecycle_status)}</span></td>
        <td>${f2p}</td>
        <td>${prCell}</td>
        <td>$${Number(t.total_cost_usd || 0).toFixed(4)}</td>
        <td>${saved}</td>
      </tr>
    `;
  }).join("");
}

function formatColorizedDiff(diffText) {
  if (!diffText || !diffText.trim()) return "(暂无代码变更补丁)";
  return diffText.split("\n").map(line => {
    const safe = escapeHtml(line);
    if (line.startsWith("+") && !line.startsWith("+++")) return `<span class="diff-add">${safe}</span>`;
    if (line.startsWith("-") && !line.startsWith("---")) return `<span class="diff-del">${safe}</span>`;
    if (line.startsWith("@@")) return `<span class="diff-hunk">${safe}</span>`;
    return safe;
  }).join("\n");
}

function renderFeishuCardHtml(cardWrapper) {
  if (!cardWrapper || !cardWrapper.card) return "";
  const card = cardWrapper.card;
  const tpl = (card.header && card.header.template) || "blue";
  const title = (card.header && card.header.title && card.header.title.content) || "飞书交互卡片";
  let mdContent = "";
  let buttonsHtml = "";

  (card.elements || []).forEach(el => {
    if (el.tag === "markdown") {
      mdContent += escapeHtml(el.content)
        .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
        .replace(/`([^`]+)`/g, "<code style='background:#f1f5f9;padding:1px 5px;border-radius:4px;'>$1</code>") + "\n";
    } else if (el.tag === "action") {
      buttonsHtml += (el.actions || []).map(act => {
        const txt = (act.text && act.text.content) || "操作";
        const cls = act.type === "primary" ? "btn btn-primary" : "btn btn-outline";
        if (act.url) {
          return `<a class="${cls}" href="${escapeHtml(act.url)}" target="_blank">${escapeHtml(txt)} ↗</a>`;
        }
        return `<span class="${cls}" style="cursor:default;">${escapeHtml(txt)}</span>`;
      }).join("");
    }
  });

  return `
    <div class="feishu-card">
      <div class="feishu-card-header feishu-tpl-${escapeHtml(tpl)}">${escapeHtml(title)}</div>
      <div class="feishu-card-body">${mdContent.trim()}</div>
      ${buttonsHtml ? `<div class="feishu-card-actions">${buttonsHtml}</div>` : ""}
    </div>
  `;
}

async function openTaskInspector(taskId, scrollIntoView = false) {
  selectedTaskId = taskId;
  window.location.hash = `task=${taskId}`;
  renderTasksTable();

  const detail = await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}`);
  const t = detail.task;
  const c = t.contract;
  const ev = t.verification_evidence;
  const box = document.getElementById("task-inspector");
  box.classList.remove("hidden");

  const isRunning = ["DRAFTING_CONTRACT", "QUEUED", "RUNNING_IN_SANDBOX", "VERIFYING_EVIDENCE"].includes(t.lifecycle_status);

  // Top Status / Action Banner
  let bannerHtml = "";
  if (isRunning) {
    bannerHtml = `
      <div style="background:#eff6ff;border:1px solid #93c5fd;padding:12px 16px;border-radius:8px;margin-bottom:14px;color:#1e40af;font-size:13px;">
        ⏳ <b>沙箱正在异步执行中 (${escapeHtml(t.lifecycle_status)} · 第 ${t.sandbox_attempt_count || 1} 轮尝试)</b> — 面板每 3 秒无感同步最新步骤，无需手动刷新网页。
      </div>
    `;
  } else if (t.lifecycle_status === "WAITING_CLARIFICATION" && c.unresolved_decisions && c.unresolved_decisions.length) {
    const qBlocks = c.unresolved_decisions.map((d, idx) => {
      const opts = (d.options || []).map((opt, oIdx) => `
        <label style="font-weight:400;display:flex;align-items:center;gap:8px;margin:5px 0;cursor:pointer;">
          <input type="radio" name="clarify_q_${idx}" value="${escapeHtml(opt)}" ${oIdx === 0 ? "checked" : ""}/>
          <span>${escapeHtml(opt)}</span>
        </label>
      `).join("");
      return `
        <div style="margin-bottom:12px;padding:10px 12px;background:#fff;border-radius:8px;border:1px solid #fde68a;">
          <div style="font-weight:600;margin-bottom:4px;">Q${idx + 1} (${escapeHtml(d.impact_scope)}): ${escapeHtml(d.question)}</div>
          ${opts}
          <div style="font-size:12px;color:#92400e;margin-top:4px;">默认假设：<code>${escapeHtml(d.default_assumption)}</code></div>
        </div>
      `;
    }).join("");

    bannerHtml = `
      <div style="background:#fffbeb;border:1px solid #f59e0b;padding:16px;border-radius:10px;margin-bottom:16px;">
        <h3 style="margin:0 0 10px;color:#b45309;">⏸️ 需你 5 秒微确认 (选择具体方案或按默认假设继续)</h3>
        ${qBlocks}
        <div style="display:flex;gap:10px;">
          <button class="btn btn-primary" onclick="submitClarification('${escapeHtml(t.task_id)}', false, ${c.unresolved_decisions.length})">✅ 提交所选答案并启动沙箱</button>
          <button class="btn btn-outline" onclick="submitClarification('${escapeHtml(t.task_id)}', true, 0)">⚡ 直接按默认假设继续</button>
        </div>
      </div>
    `;
  } else if (t.handoff_bundle) {
    const hb = t.handoff_bundle;
    bannerHtml = `
      <div style="background:#fef2f2;border:1px solid #fca5a5;padding:14px 16px;border-radius:10px;margin-bottom:14px;font-size:13px;">
        <div style="font-weight:700;color:#b91c1c;margin-bottom:6px;">🤝 结构化交接包 (${escapeHtml(hb.failure_class)})：${escapeHtml(hb.reason)}</div>
        <div style="color:#7f1d1d;">建议下一步：${escapeHtml(hb.recommended_next_step)}</div>
      </div>
    `;
  }

  // Header Action Buttons
  let prBtn = "";
  if (t.deliverable_pr_url) {
    const label = t.deliverable_pr_number ? `打开 GitHub Draft PR #${t.deliverable_pr_number} ↗` : "前往 GitHub 建单 ↗";
    prBtn = `<a class="btn btn-primary" href="${escapeHtml(t.deliverable_pr_url)}" target="_blank">${label}</a>`;
  } else if (ev && ev.passed_all_hard_gates) {
    prBtn = `<button class="btn btn-primary" onclick="controlTask('${escapeHtml(t.task_id)}', 'publish_pr')">🚀 推送并创建 GitHub Draft PR</button>`;
  }

  // Sub-tab Content
  let tabBody = "";
  if (selectedSubtab === "contract") {
    tabBody = `
      <div style="font-size:13.5px;line-height:1.7;">
        <p><b>一句话目标</b>：${escapeHtml(c.objective)}</p>
        <p><b>锁定基线与工作树</b>：<code>${escapeHtml(c.target_repo)}</code> (<code>${escapeHtml(c.base_branch)}</code> @ <code>${escapeHtml((c.base_commit_sha || "").slice(0, 10))}</code>) → 隔离分支 <code>${escapeHtml(t.branch_name)}</code></p>
        <p><b>允许修改白名单 (Permitted Scopes)</b>：${(c.permitted_scopes || []).map(s => `<code style="background:#eff6ff;color:#1d4ed8;padding:2px 6px;border-radius:4px;margin-right:6px;">${escapeHtml(s)}</code>`).join("")}</p>
        <p><b>明确排除边界 (Out of Scope)</b>：${(c.out_of_scope || []).map(s => `<code style="background:#f1f5f9;padding:2px 6px;border-radius:4px;margin-right:6px;">${escapeHtml(s)}</code>`).join("")}</p>
        <p><b>可验证验收标准 (Acceptance Criteria)</b>：</p>
        <ul>${(c.acceptance_criteria || []).map(ac => `<li>${escapeHtml(ac)}</li>`).join("")}</ul>
      </div>
    `;
  } else if (selectedSubtab === "evidence") {
    if (!ev) {
      tabBody = `<p style="color:#64748b;font-size:13.5px;">尚未生成门禁验证报告（沙箱完成后自动产出）。</p>`;
    } else {
      const risks = (ev.reviewer_report && ev.reviewer_report.unverified_risks) || ev.unverified_risks || [];
      const acAssess = (ev.reviewer_report && ev.reviewer_report.ac_assessment) || [];
      tabBody = `
        <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px;font-size:12.5px;">
          <div class="kpi-card"><div>基线复现失败</div><b style="font-size:15px;">${ev.repro_test_failed_on_base ? '✅ FAIL (符合)' : '❌ 未复现'}</b></div>
          <div class="kpi-card"><div>修复后通过</div><b style="font-size:15px;">${ev.repro_test_passed_on_head ? '✅ PASS' : '❌ 失败'}</b></div>
          <div class="kpi-card"><div>原有关联测试套件</div><b style="font-size:15px;">${ev.existing_test_suite_passed ? '✅ 全绿' : '❌ 回归失败'}</b></div>
          <div class="kpi-card"><div>测试反作弊扫描</div><b style="font-size:15px;">${ev.tests_weakened_check ? '✅ 干净' : '❌ 违规'}</b></div>
          <div class="kpi-card"><div>范围与静态检查</div><b style="font-size:15px;">${(ev.scope_compliance_check && ev.static_analysis_passed) ? '✅ 合规' : '❌ 越界'}</b></div>
        </div>
        <p style="font-size:13.5px;"><b>目标回归单测</b>：<code>${escapeHtml(ev.repro_test_target)}</code> | <b>验证 Commit</b>：<code>${escapeHtml((ev.verified_commit_sha || "").slice(0, 10))}</code></p>
        ${ev.reviewer_report ? `<p style="font-size:13.5px;"><b>单审查器结论</b>：${escapeHtml(ev.reviewer_report.summary)}</p>` : ""}
        ${acAssess.length ? `<ul style="font-size:13px;">${acAssess.map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : ""}
        <div style="background:#fffbeb;border:1px solid #fde68a;padding:12px 16px;border-radius:8px;margin-top:12px;font-size:13px;">
          <b>⚠️ 未验证风险盲区显式披露 (Unverified Risks — 供人类最终把关)</b>
          <ul style="margin:6px 0 0;">${risks.map(r => `<li>${escapeHtml(r)}</li>`).join("")}</ul>
        </div>
      `;
    }
  } else if (selectedSubtab === "diff") {
    tabBody = `
      <div style="margin-bottom:14px;">
        <label>Git Diff 补丁 (${escapeHtml(t.branch_name)})</label>
        <div class="code-box">${formatColorizedDiff(detail.diff)}</div>
      </div>
      <details>
        <summary style="cursor:pointer;font-size:13px;font-weight:600;color:#475569;">查看沙箱单测与工具调用完整验证日志</summary>
        <div class="code-box" style="margin-top:8px;">${escapeHtml(detail.verification_log || "(暂无验证日志)")}</div>
      </details>
    `;
  } else if (selectedSubtab === "feishu") {
    const cps = (detail.checkpoints || []).map(cp => `
      <li>
        <code>${escapeHtml((cp.created_at || "").slice(11, 19))}</code>
        <span><b>[${escapeHtml(cp.step_name)}]</b> → ${escapeHtml(cp.lifecycle_status)} <span style="color:#64748b;">${escapeHtml((cp.note || "").slice(0, 110))}</span></span>
      </li>
    `).join("");

    tabBody = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
        <div>
          <label style="margin-bottom:8px;">系统向用户推送的飞书交互式卡片实时渲染预览：</label>
          ${renderFeishuCardHtml(detail.feishu_card_preview)}
        </div>
        <div>
          <label style="margin-bottom:8px;">SQLite WAL 持久化状态机快照 (${detail.checkpoints.length} 步)：</label>
          <ul class="timeline">${cps}</ul>
        </div>
      </div>
    `;
  }

  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
      <div style="display:flex;align-items:center;gap:10px;">
        <h2 style="margin:0;font-size:18px;">任务详情 #${escapeHtml(t.task_id)}</h2>
        <span class="badge st-${escapeHtml(t.lifecycle_status)}">${escapeHtml(t.lifecycle_status)}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        ${prBtn}
        <button class="btn btn-success" onclick="controlTask('${escapeHtml(t.task_id)}', 'accept')">✓ 标记已采纳合入</button>
        <button class="btn btn-danger" onclick="controlTask('${escapeHtml(t.task_id)}', 'cancel')">✕ 中断并交接 (Handoff)</button>
        <button class="btn btn-outline" onclick="closeInspector()">收起</button>
      </div>
    </div>

    <div style="margin-top:12px;">${bannerHtml}</div>

    <div class="subtabs">
      <button class="subtab ${selectedSubtab === 'contract' ? 'active' : ''}" onclick="switchSubtab('contract')">1. 任务契约与边界</button>
      <button class="subtab ${selectedSubtab === 'evidence' ? 'active' : ''}" onclick="switchSubtab('evidence')">2. 证据门禁与盲区披露</button>
      <button class="subtab ${selectedSubtab === 'diff' ? 'active' : ''}" onclick="switchSubtab('diff')">3. 代码补丁与沙箱日志</button>
      <button class="subtab ${selectedSubtab === 'feishu' ? 'active' : ''}" onclick="switchSubtab('feishu')">4. 飞书交付卡片预览 & 轨迹</button>
    </div>

    <div>${tabBody}</div>

    <!-- Follow-up & Decision Ledger Promotion Bar -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:18px;padding-top:16px;border-top:1px solid #e2e8f0;">
      <form onsubmit="submitFollowup(event, '${escapeHtml(t.task_id)}')">
        <label>💬 下半场追加修改指令 (唤醒原沙箱改代码并自动更新 PR)</label>
        <div style="display:flex;gap:8px;">
          <input type="text" id="fu-input" placeholder="例如：再补充一个空字符串与混合大小写边界断言" required />
          <button type="submit" class="btn btn-dark" style="white-space:nowrap;">增量追改</button>
        </div>
      </form>
      <form onsubmit="submitPromoteDecision(event, '${escapeHtml(t.task_id)}')">
        <label>🧠 沉淀一条长期规则到目标仓 .ai/DECISIONS.yaml</label>
        <div style="display:grid;grid-template-columns:140px 1fr auto;gap:8px;">
          <input type="text" id="dec-scope" value="${escapeHtml((c.permitted_scopes && c.permitted_scopes[0]) || '*')}" required />
          <input type="text" id="dec-rule" placeholder="输入需团队永久遵守的架构/业务规则" required />
          <button type="submit" class="btn btn-outline" style="white-space:nowrap;">写入账本</button>
        </div>
      </form>
    </div>
  `;

  if (scrollIntoView) {
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function switchSubtab(tab) {
  selectedSubtab = tab;
  if (selectedTaskId) openTaskInspector(selectedTaskId, false);
}

function closeInspector() {
  selectedTaskId = null;
  window.location.hash = "";
  document.getElementById("task-inspector").classList.add("hidden");
  renderTasksTable();
}

// ==================== ACTIONS ====================

async function submitDelegation(e) {
  e.preventDefault();
  const btn = document.getElementById("btn-delegate");
  btn.disabled = true;
  btn.textContent = "⏳ 正在探测代码库并起草契约...";
  try {
    const task = await fetchJson("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        target_repo: document.getElementById("del-repo").value,
        raw_request: document.getElementById("del-request").value,
        publish_pr: document.getElementById("del-publish-pr").checked,
        accept_defaults: document.getElementById("del-accept-defaults").checked,
        auto_execute: true,
      }),
    });
    document.getElementById("del-request").value = "";
    showToast(`任务 ${task.task_id} 已创建 (${task.lifecycle_status})`);
    await refreshAll();
    await openTaskInspector(task.task_id, true);
  } catch (err) {
    showToast(`发起失败: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "🚀 起草任务契约并启动沙箱";
  }
}

async function submitClarification(taskId, useDefaults, count) {
  const answers = {};
  if (!useDefaults) {
    for (let i = 0; i < count; i++) {
      const checked = document.querySelector(`input[name="clarify_q_${i}"]:checked`);
      if (checked) answers[String(i)] = checked.value;
    }
  }
  try {
    await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/clarify`, {
      method: "POST",
      body: JSON.stringify({ answers, use_defaults: useDefaults, auto_execute: true, publish_pr: true }),
    });
    showToast("已确认契约决策，后台沙箱开始执行！");
    await refreshAll();
  } catch (err) {
    showToast(`确认失败: ${err.message}`, true);
  }
}

async function controlTask(taskId, action) {
  try {
    await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/control`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    showToast(`已执行操作: ${action}`);
    await refreshAll();
  } catch (err) {
    showToast(`操作失败: ${err.message}`, true);
  }
}

async function submitFollowup(e, taskId) {
  e.preventDefault();
  const body = document.getElementById("fu-input").value;
  try {
    await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/followup`, {
      method: "POST",
      body: JSON.stringify({ comment_body: body, publish_pr: true }),
    });
    document.getElementById("fu-input").value = "";
    showToast("追改指令已下发，沙箱正在增量更新！");
    await refreshAll();
  } catch (err) {
    showToast(`追改失败: ${err.message}`, true);
  }
}

async function submitPromoteDecision(e, taskId) {
  e.preventDefault();
  try {
    const entry = await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/promote-decision`, {
      method: "POST",
      body: JSON.stringify({
        scope: document.getElementById("dec-scope").value,
        rule: document.getElementById("dec-rule").value,
      }),
    });
    document.getElementById("dec-rule").value = "";
    showToast(`已写入决策账本: ${entry.id}`);
  } catch (err) {
    showToast(`写入失败: ${err.message}`, true);
  }
}

// ==================== REPOSITORIES VIEW ====================

async function loadRepositoriesView() {
  const repos = await fetchJson("/api/repositories");
  const container = document.getElementById("repos-list");
  container.innerHTML = repos.map(r => {
    const policies = (r.active_policies || []).map(p =>
      `<li><b>[${escapeHtml(p.id)}]</b> (<code>${escapeHtml(p.scope)}</code>): ${escapeHtml(p.rule)} <span style="color:#64748b;">— ${escapeHtml(p.reason)}</span></li>`
    ).join("") || "<li style='color:#64748b;'>暂无生效策略</li>";

    return `
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <h3 style="margin:0;">📦 ${escapeHtml(r.repo_id)} <span style="font-weight:400;color:#64748b;font-size:13px;">(${escapeHtml(r.github_repo)} · 基线 <code>${escapeHtml(r.default_base_branch)}</code>)</span></h3>
          <code style="font-size:12px;color:#475569;">${escapeHtml(r.local_path)}</code>
        </div>
        <p style="font-size:12.5px;color:#475569;margin:8px 0;">测试执行器：<code>${escapeHtml((r.pytest_cmd || []).join(" "))}</code></p>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:13px;">
          <b>生效中的组织决策账本 (.ai/DECISIONS.yaml · APPROVED_POLICY)：</b>
          <ul style="margin:6px 0 0;">${policies}</ul>
        </div>
      </div>
    `;
  }).join("");
}

async function submitOnboard(e) {
  e.preventDefault();
  const btn = document.getElementById("btn-onboard");
  btn.disabled = true;
  btn.textContent = "正在接入...";
  try {
    const p = await fetchJson("/api/repositories/onboard", {
      method: "POST",
      body: JSON.stringify({
        source: document.getElementById("onb-source").value,
        repo_id: document.getElementById("onb-id").value || null,
        base_branch: document.getElementById("onb-branch").value || null,
      }),
    });
    document.getElementById("onb-source").value = "";
    showToast(`仓库 ${p.repo_id} 已成功接入！`);
    await loadHealthAndMetrics();
    await loadRepositoriesView();
  } catch (err) {
    showToast(`接入失败: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "接入仓库";
  }
}

// ==================== FEISHU VIEW ====================

async function loadFeishuConfig() {
  const data = await fetchJson("/api/integrations/feishu");
  document.getElementById("fs-appid").value = data.app_id || "";
  document.getElementById("fs-status-hint").innerHTML =
    `当前状态：群 Webhook <b>${data.webhook_configured ? '✅ 已配置 (' + escapeHtml(data.webhook_masked) + ')' : '未填写'}</b> | ` +
    `自建应用 <b>${data.app_secret_configured ? '✅ 已配置' : '未填写'}</b> | ` +
    `回调入口：<code>${escapeHtml(data.callback_webhook_path)}</code>`;
}

async function saveFeishuConfig(e) {
  e.preventDefault();
  try {
    await fetchJson("/api/integrations/feishu/config", {
      method: "POST",
      body: JSON.stringify({
        webhook_url: document.getElementById("fs-webhook").value,
        app_id: document.getElementById("fs-appid").value,
        app_secret: document.getElementById("fs-secret").value,
      }),
    });
    document.getElementById("fs-webhook").value = "";
    document.getElementById("fs-secret").value = "";
    showToast("飞书配置已保存并热加载生效！");
    await loadFeishuConfig();
    await loadHealthAndMetrics();
  } catch (err) {
    showToast(`保存失败: ${err.message}`, true);
  }
}

async function sendFeishuTestCard() {
  try {
    const res = await fetchJson("/api/integrations/feishu/test", { method: "POST" });
    showToast(res.message, !res.ok);
    document.getElementById("sim-result").innerHTML = renderFeishuCardHtml(res.card_preview);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function simulateFeishu(dispatchReal) {
  const text = document.getElementById("sim-input").value;
  try {
    const res = await fetchJson("/api/integrations/feishu/simulate", {
      method: "POST",
      body: JSON.stringify({ raw_text: text, dispatch_real_task: dispatchReal }),
    });
    const intentJson = escapeHtml(JSON.stringify(res.parsed_intent, null, 2));
    const card = res.feishu_reply_card || res.sample_test_card;
    document.getElementById("sim-result").innerHTML = `
      <div style="font-size:12.5px;margin-bottom:10px;">
        <b>解析出的结构化路由意图：</b>
        <pre style="background:#0f172a;color:#e2e8f0;padding:10px;border-radius:6px;margin:6px 0;">${intentJson}</pre>
      </div>
      ${card ? `<div style="font-size:12.5px;margin-bottom:6px;"><b>机器人向飞书会话推送的卡片输出：</b></div>` + renderFeishuCardHtml(card) : ""}
    `;
    if (res.dispatched && res.task) {
      showToast(`已通过模拟飞书通道创建任务 ${res.task.task_id}`);
      await refreshAll();
    }
  } catch (err) {
    showToast(`模拟执行失败: ${err.message}`, true);
  }
}

// ==================== BOOTSTRAP & AUTO-POLL ====================

async function refreshAll() {
  await Promise.all([loadHealthAndMetrics(), loadTasks()]);
}

window.addEventListener("DOMContentLoaded", async () => {
  const hash = window.location.hash || "";
  if (hash.startsWith("#task=")) {
    selectedTaskId = hash.replace("#task=", "").trim();
  }
  await refreshAll();
  if (selectedTaskId) {
    openTaskInspector(selectedTaskId, true).catch(() => {});
  }
  setInterval(() => {
    if (currentView === "workbench") {
      refreshAll().catch(() => {});
    }
  }, 3000);
});
