const messages = {
  en: {
    eyebrow: "LOCAL / CONTROLLED", loading: "Loading live Inventory…", navigation: "Dashboard navigation",
    search: "Search", manage: "Manage", back: "Back", home: "Home", inventory: "Inventory",
    capabilityLibrary: "Capabilities", capabilityLibraryHint: "Browse, understand, and control capabilities without loading their bodies.",
    conversations: "Conversations", conversationsHint: "Choose a local Codex task to see capability calls observed by CapSift. Conversation text is not read.",
    chooseConversation: "Choose conversation", chooseConversationPlaceholder: "Select a conversation",
    chooseConversationHelp: "Select a conversation to inspect observed capability use.", details: "System details",
    tokenNote: "Turning a capability on costs 0 Token now. The estimate applies only when its instructions are loaded later.",
    loadMore: "Load more", capabilityCount: "{value} capabilities", estimatedTokens: "About {value} Token when loaded",
    activationStatus: "Activation", disableSavings: "Disable to avoid about {value} Token on a future full instruction load",
    enableCost: "Enable now: 0 Token · future instruction load about {value} Token",
    allowLoading: "Allow loading", blockLoading: "Block loading",
    zeroTokenNow: "0 Token now", enabledLabel: "Enabled", disabledLabel: "Disabled", observedOnly: "Observed calls only; Codex-native loads that bypass CapSift may be missing.",
    noObservedCapabilities: "No capability calls were observed by CapSift in this conversation.",
    observedSkill: "Skill instructions observed", observedTool: "Tool call observed",
    traceTooLarge: "This conversation is too large for the safe metadata scan. CapSift did not read its messages.",
    inventoryHint: "What Codex can find right now. Professional names stay visible.",
    searchHint: "Find compact capability cards without loading their bodies.", taskOrName: "Task or name",
    searchPlaceholder: "Example: work with PDF files", kind: "Kind", capabilityKind: "Capability kind", all: "All",
    manageHint: "Project-only language and activation controls. No files are deleted or executed.",
    language: "Language", auto: "Auto", saveLanguage: "Save language", status: "Status", snapshot: "Snapshot",
    generation: "Generation", active: "Active", inactive: "Inactive", notices: "Notices",
    noticesHint: "Safe counts only. Paths, commands, URLs, and credentials are hidden.", health: "Health",
    healthHint: "Checks wiring without loading capability bodies.", connections: "Connections",
    connectionsHint: "Configured state only. No network probe is performed.", providers: "Providers",
    providersHint: "Which local adapter supplies each discovered capability.", loaded: "Loaded",
    loadedHint: "Recent successful loads from redacted project audit. Bodies are not retained here.",
    approvals: "Approvals", approvalsHint: "Exact, expiring requests. Arguments and their digests are never displayed.",
    context: "Context", contextHint: "Metadata for disclosed sections. Pin, unpin, or forget without reloading bodies.",
    reasoning: "Reasoning", reasoningHint: "Persistent advisory state for the Dashboard task. Recommendations do not call a model.",
    currentTier: "Current tier", budgetLeft: "Budget left", escalations: "Escalations", updates: "Updates",
    updatesHint: "Staged revision, health gate, active pointer, rollback target, and in-flight pins.",
    auditSecurity: "Audit security", auditSecurityHint: "Optional HMAC chain status. The signing key value is never displayed.",
    secureChain: "Secure chain", keySetting: "Key setting", audit: "Audit",
    auditHint: "Recent redacted project events. Arguments and credentials are never shown.",
    noExclusions: "No exclusions detected.", noHealth: "No health checks available.",
    noConnections: "No configured connections.", noProviders: "No Providers discovered.",
    noLoaded: "No successful loads recorded yet.", noApprovals: "No approval requests.",
    noContext: "No disclosed sections are resident.", noUpdates: "No staged update state.",
    noAudit: "No audit events recorded yet.", noLifecycle: "No lifecycle overrides. Search to manage a capability.",
    noMatches: "No matching capabilities.", searchUnavailable: "Search unavailable.", live: "Live snapshot updated.",
    unavailable: "Snapshot unavailable; retrying.", languageSaved: "Language preference saved.",
    languageFailed: "Language preference could not be saved.", saving: "Saving…", approve: "Approve", deny: "Deny",
    expires: "expires {value}", tokens: "{value} tokens", pinned: "pinned", evictable: "evictable", pin: "Pin",
    unpin: "Unpin", forget: "Forget", enable: "Enable", disable: "Disable", quarantine: "Quarantine",
    quarantineConfirm: "Quarantine {value}?", routing: "Routing", activeCount: "{active}/{total} active",
    providerState: "{provider} / {state}", updateState: "active {active} / staged {staged} / health {health}",
    configured: "configured", notConfigured: "not configured", notSelected: "not selected", global: "global",
    activeState: "active", inactiveState: "inactive",
  },
  "zh-CN": {
    eyebrow: "本地 / 受控", loading: "正在加载实时 Inventory（能力清单）…", navigation: "Dashboard 导航",
    search: "Search（搜索）", manage: "Manage（管理）", back: "返回", home: "主页",
    capabilityLibrary: "能力库", capabilityLibraryHint: "查看介绍并控制能力，不加载能力正文。",
    conversations: "对话", conversationsHint: "选择本机 Codex 对话，查看 CapSift 观察到的能力调用；不会读取对话正文。",
    chooseConversation: "选择对话", chooseConversationPlaceholder: "请选择一个对话",
    chooseConversationHelp: "选择对话后查看已观察到的能力使用情况。", details: "系统详情",
    tokenNote: "启用能力现在消耗 0 Token；只有以后加载说明正文时才会产生下方预计用量。",
    loadMore: "加载更多", capabilityCount: "{value} 项能力", estimatedTokens: "加载说明约 {value} Token",
    activationStatus: "启用状态", disableSavings: "停用后可避免以后完整加载约 {value} Token",
    enableCost: "现在启用：0 Token · 以后加载说明约 {value} Token",
    allowLoading: "允许加载", blockLoading: "阻止加载",
    zeroTokenNow: "当前 0 Token", enabledLabel: "已启用", disabledLabel: "已停用",
    observedOnly: "仅显示观察到的调用；绕过 CapSift 的 Codex 原生加载可能不会记录。",
    noObservedCapabilities: "CapSift 没有在此对话中观察到能力调用。",
    observedSkill: "观察到 Skill 说明读取", observedTool: "观察到工具调用",
    traceTooLarge: "此对话超过安全元数据扫描上限；CapSift 未读取其消息。",
    inventory: "Inventory（能力清单）", inventoryHint: "显示 Codex 当前可以找到的能力；保留专业名称。",
    searchHint: "搜索精简能力卡片，不加载能力正文。", taskOrName: "任务或名称",
    searchPlaceholder: "例如：处理 PDF 文件", kind: "类型", capabilityKind: "能力类型", all: "全部",
    manageHint: "仅管理当前项目的语言和启用状态；不会删除文件或执行能力。", language: "语言",
    auto: "自动", saveLanguage: "保存语言", status: "Status（状态）", snapshot: "快照",
    generation: "代次", active: "已启用", inactive: "未启用", notices: "Notices（提示）",
    noticesHint: "只显示安全计数；路径、命令、URL 和凭据均会隐藏。", health: "Health（健康检查）",
    healthHint: "不加载能力正文，只检查本地接线。", connections: "Connections（连接）",
    connectionsHint: "只显示配置状态，默认不进行网络探测。", providers: "Providers（提供方）",
    providersHint: "显示每项已发现能力由哪个本地适配器提供。", loaded: "Loaded（已加载）",
    loadedHint: "显示脱敏审计中的最近成功加载记录；这里不保留能力正文。", approvals: "Approvals（审批）",
    approvalsHint: "精确且会过期的请求；参数及参数摘要都不会显示。", context: "Context（上下文）",
    contextHint: "已披露部分的元数据；无需重新加载正文即可固定、取消固定或忘记。",
    reasoning: "Reasoning（推理）", reasoningHint: "Dashboard 任务的持久建议状态；生成建议不会调用模型。",
    currentTier: "当前强度", budgetLeft: "剩余预算", escalations: "升级次数", updates: "Updates（更新）",
    updatesHint: "显示暂存版本、健康门、活动指针、回滚目标和执行中的固定版本。",
    auditSecurity: "Audit security（审计安全）", auditSecurityHint: "可选 HMAC 链状态；绝不显示签名密钥值。",
    secureChain: "安全链", keySetting: "密钥设置", audit: "Audit（审计记录）",
    auditHint: "最近的脱敏项目事件；绝不显示参数和凭据。", noExclusions: "未发现排除项。",
    noHealth: "没有可用的健康检查。", noConnections: "没有已配置的连接。", noProviders: "没有发现 Provider。",
    noLoaded: "尚无成功加载记录。", noApprovals: "没有审批请求。", noContext: "没有驻留的已披露部分。",
    noUpdates: "没有暂存的更新状态。", noAudit: "尚无审计事件。",
    noLifecycle: "没有生命周期覆盖项；请先搜索要管理的能力。", noMatches: "没有匹配的能力。",
    searchUnavailable: "搜索暂不可用。", live: "实时快照已更新。", unavailable: "快照不可用，正在重试。",
    languageSaved: "语言设置已保存。", languageFailed: "语言设置保存失败。", saving: "正在保存…",
    approve: "批准", deny: "拒绝", expires: "到期时间 {value}", tokens: "{value} Token", pinned: "已固定",
    evictable: "可移除", pin: "固定", unpin: "取消固定", forget: "忘记", enable: "启用", disable: "停用",
    quarantine: "隔离", quarantineConfirm: "确定隔离 {value}？", routing: "Routing（选择原因）",
    activeCount: "已启用 {active}/{total}", providerState: "{provider} / {state}",
    updateState: "活动 {active} / 暂存 {staged} / 健康 {health}", configured: "已配置",
    notConfigured: "未配置", notSelected: "未选择", global: "全局", activeState: "已启用", inactiveState: "未启用",
  },
};

let activePreference = "auto";
let currentLocale = "en";

const resolvedLocale = (preference) => {
  if (preference === "zh-CN") return "zh-CN";
  if (preference === "en") return "en";
  return navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
};

const t = (key, values = {}) => {
  const template = messages[currentLocale][key] ?? messages.en[key] ?? key;
  return Object.entries(values).reduce(
    (result, [name, value]) => result.replaceAll(`{${name}}`, String(value)),
    template,
  );
};

const applyLocale = (preference) => {
  activePreference = preference || "auto";
  currentLocale = resolvedLocale(activePreference);
  document.documentElement.lang = currentLocale;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
  });
  document.getElementById("language").value = activePreference;
};

const text = (value) => String(value ?? "—");
let csrfToken = "";
let capabilityEntries = [];
let capabilityNextOffset = 0;
let capabilityTotal = 0;

const setText = (id, value) => {
  document.getElementById(id).textContent = text(value);
};

const list = (id, items, empty) => {
  const target = document.getElementById(id);
  target.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const value = document.createElement("span");
    name.textContent = text(item.name);
    value.textContent = text(item.value);
    row.append(name, value);
    target.append(row);
  });
  if (!target.children.length) {
    const row = document.createElement("li");
    row.textContent = empty;
    target.append(row);
  }
};

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw Error("status unavailable");
    const payload = await response.json();
    const inventory = payload.inventory || {};
    csrfToken = payload.dashboard?.csrf_token || "";
    applyLocale(payload.preferences?.locale || activePreference);
    renderConversationOptions(payload.conversations?.entries || []);
    const kinds = inventory.active_by_kind || {};
    ["skill", "mcp", "cli", "api", "rag"].forEach((kind) => {
      setText(`kind-${kind}`, kinds[kind] ?? 0);
    });
    setText("snapshot-status", inventory.status);
    setText("generation", inventory.generation);
    setText("active-total", inventory.active_total);
    setText("inactive-total", inventory.inactive_count);

    const excluded = inventory.excluded_by_reason || {};
    const notices = Object.entries(excluded)
      .filter(([, count]) => Number(count) > 0)
      .map(([name, count]) => ({ name, value: count }));
    list("notice-list", notices, t("noExclusions"));

    const checks = (payload.health?.checks || []).map((item) => ({
      name: item.check,
      value: item.status,
    }));
    list("health-list", checks, t("noHealth"));
    const connections = (payload.connections?.connections || []).map((item) => ({
      name: `${item.kind} (${item.active}/${item.configured})`,
      value: item.state,
    }));
    list("connection-list", connections, t("noConnections"));
    const providerSource = payload.providers?.entries || payload.providers || [];
    const providers = providerSource.map((item) => ({
      name: item.provider || item.name,
      value: t("activeCount", { active: item.active ?? 0, total: item.discovered ?? 0 }),
    }));
    list("provider-list", providers, t("noProviders"));
    const loaded = (payload.loaded_capabilities || []).map((item) => ({
      name: `${item.kind}: ${item.revision}`,
      value: t("providerState", {
        provider: item.provider,
        state: item.active ? t("activeState") : t("inactiveState"),
      }),
    }));
    list("loaded-list", loaded, t("noLoaded"));
    renderApprovals(payload.approvals?.approvals || []);
    renderContext(payload.context?.entries || []);
    setText("reasoning-tier", payload.reasoning?.current_tier || t("notSelected"));
    setText("reasoning-budget", payload.reasoning?.budget?.remaining);
    setText("reasoning-escalations", payload.reasoning?.escalations_used ?? 0);
    const updates = (payload.updates?.states || []).map((item) => ({
      name: item.coordinate,
      value: t("updateState", {
        active: text(item.active_revision),
        staged: text(item.staged_revision),
        health: text(item.health_status),
      }),
    }));
    list("update-list", updates, t("noUpdates"));
    setText("secure-audit-status", payload.secure_audit?.configured ? t("configured") : t("notConfigured"));
    setText("secure-audit-key", payload.secure_audit?.key_environment);
    const audit = (payload.audit?.events || []).map((item) => ({
      name: `${item.sequence}: ${item.event_type} / ${item.outcome}`,
      value: item.capability_revision || item.reason_codes?.join(", ") || t("global"),
    }));
    list("audit-list", audit, t("noAudit"));
    renderLifecycle(payload.lifecycle?.entries || []);
    document.getElementById("state").textContent = t("live");
  } catch {
    document.getElementById("state").textContent = t("unavailable");
  }
}

async function loadCapabilities({ reset = false, all = false } = {}) {
  if (reset) {
    capabilityEntries = [];
    capabilityNextOffset = 0;
  }
  const query = document.getElementById("search-query").value.trim();
  const kind = document.getElementById("search-kind").value;
  do {
    const params = new URLSearchParams({
      q: query,
      offset: String(capabilityNextOffset || 0),
      limit: "500",
    });
    if (kind) params.set("kind", kind);
    const response = await fetch(`/api/capabilities?${params}`, { cache: "no-store" });
    if (!response.ok) throw Error("capability listing failed");
    const payload = await response.json();
    capabilityEntries.push(...(payload.entries || []));
    capabilityTotal = Number(payload.total || 0);
    capabilityNextOffset = payload.next_offset;
  } while (all && capabilityNextOffset !== null);
  renderCapabilityCards();
}

function renderCapabilityCards() {
  const target = document.getElementById("search-list");
  target.replaceChildren();
  const activationFilter = document.getElementById("activation-filter").value;
  const visibleEntries = capabilityEntries.filter((item) => (
    !activationFilter || (activationFilter === "enabled") === Boolean(item.active)
  ));
  visibleEntries.forEach((item) => {
    const row = document.createElement("li");
    row.className = "capability-card";
    const top = document.createElement("div");
    top.className = "card-top";
    const identity = document.createElement("div");
    const name = document.createElement("strong");
    const meta = document.createElement("span");
    const summary = document.createElement("p");
    const footer = document.createElement("div");
    const token = document.createElement("span");
    const button = actionButton(
      item.active ? t("blockLoading") : t("allowLoading"),
      item.coordinate,
      item.active ? "disabled" : "enabled",
    );
    name.textContent = item.coordinate;
    meta.textContent = `${String(item.kind).toUpperCase()} · ${text(item.provider)} · ${item.active ? t("enabledLabel") : t("disabledLabel")}`;
    summary.textContent = text(item.summary);
    token.textContent = item.active
      ? t("disableSavings", { value: item.estimated_load_tokens || 0 })
      : t("enableCost", { value: item.estimated_load_tokens || 0 });
    footer.className = "card-footer";
    footer.append(token, button);
    identity.append(name, meta);
    top.append(identity);
    row.append(top, summary, footer);
    target.append(row);
  });
  if (!visibleEntries.length) list("search-list", [], t("noMatches"));
  document.getElementById("capability-count").textContent = t("capabilityCount", { value: visibleEntries.length });
  document.getElementById("load-more").hidden = capabilityNextOffset === null;
}

function renderConversationOptions(entries) {
  const select = document.getElementById("conversation-select");
  const selected = select.value;
  const existing = new Set(Array.from(select.options).map((option) => option.value));
  entries.forEach((item) => {
    if (existing.has(item.id)) return;
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.title} · ${String(item.updated_at).replace("T", " ").slice(0, 16)}`;
    select.append(option);
  });
  if (selected) select.value = selected;
}

async function loadConversation(taskId) {
  const target = document.getElementById("conversation-capabilities");
  if (!taskId) {
    target.replaceChildren();
    document.getElementById("conversation-summary").textContent = t("chooseConversationHelp");
    return;
  }
  document.getElementById("conversation-summary").textContent = t("loading");
  const response = await fetch(`/api/conversation?id=${encodeURIComponent(taskId)}`, { cache: "no-store" });
  if (!response.ok) throw Error("conversation unavailable");
  const payload = await response.json();
  if (payload.status === "trace_too_large") {
    list("conversation-capabilities", [], t("traceTooLarge"));
  } else {
    const items = (payload.capabilities || []).map((item) => ({
      name: `${String(item.kind).toUpperCase()} · ${item.name}`,
      value: item.source === "observed_skill_instruction_read" ? t("observedSkill") : t("observedTool"),
    }));
    list("conversation-capabilities", items, t("noObservedCapabilities"));
  }
  document.getElementById("conversation-summary").textContent = t("observedOnly");
  await loadCapabilities({ reset: true, all: true });
}

const approvalButton = (label, approvalId, decision) => {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    await postJson("/api/approval", { approval_id: approvalId, decision });
    await refresh();
  });
  return button;
};

function renderApprovals(entries) {
  const target = document.getElementById("approval-list");
  target.replaceChildren();
  entries.forEach((item) => {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const state = document.createElement("span");
    name.textContent = `${text(item.operation)} / ${text(item.status)}`;
    state.textContent = `${text(item.revision)} / expires ${text(item.expires_at)}`;
    row.append(name, state);
    if (item.status === "pending") {
      const actions = document.createElement("div");
      actions.className = "actions";
      actions.append(
        approvalButton(t("approve"), item.approval_id, "approve"),
        approvalButton(t("deny"), item.approval_id, "deny"),
      );
      row.append(actions);
    }
    target.append(row);
  });
  if (!entries.length) list("approval-list", [], t("noApprovals"));
}

const contextButton = (label, key, action) => {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    await postJson("/api/context", { key, action });
    await refresh();
  });
  return button;
};

function renderContext(entries) {
  const target = document.getElementById("context-list");
  target.replaceChildren();
  entries.forEach((item) => {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const state = document.createElement("span");
    const actions = document.createElement("div");
    name.textContent = text(item.section);
    state.textContent = `${t("tokens", { value: text(item.portable_tokens) })} / ${item.pinned ? t("pinned") : t("evictable")}`;
    actions.className = "actions";
    actions.append(
      contextButton(item.pinned ? t("unpin") : t("pin"), item.key, item.pinned ? "unpin" : "pin"),
      contextButton(t("forget"), item.key, "remove"),
    );
    row.append(name, state, actions);
    target.append(row);
  });
  if (!entries.length) list("context-list", [], t("noContext"));
}

const coordinateFromRevision = (revision) => String(revision).split("@", 1)[0];

const actionButton = (label, coordinate, state) => {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    if (state === "quarantined" && !confirm(t("quarantineConfirm", { value: coordinate }))) return;
    await postJson("/api/lifecycle", { coordinate, state });
    await refresh();
    await loadCapabilities({ reset: true, all: true });
  });
  return button;
};

function renderLifecycle(entries) {
  const target = document.getElementById("lifecycle-list");
  target.replaceChildren();
  entries.forEach((item) => {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const state = document.createElement("span");
    const actions = document.createElement("div");
    name.textContent = text(item.coordinate);
    state.textContent = `${text(item.state)} / ${item.active ? t("activeState") : t("inactiveState")}`;
    actions.className = "actions";
    actions.append(
      actionButton(t("enable"), item.coordinate, "enabled"),
      actionButton(t("disable"), item.coordinate, "disabled"),
      actionButton(t("quarantine"), item.coordinate, "quarantined"),
    );
    row.append(name, state, actions);
    target.append(row);
  });
  if (!entries.length) list("lifecycle-list", [], t("noLifecycle"));
}

function renderSearch(results) {
  const target = document.getElementById("search-list");
  target.replaceChildren();
  results.forEach((item) => {
    const coordinate = coordinateFromRevision(item.revision);
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const summary = document.createElement("span");
    const actions = document.createElement("div");
    name.textContent = `${coordinate} (${item.kind})`;
    const reasons = (item.match_reason || []).join(", ") || "active catalog";
    summary.textContent = `${text(item.summary)} / ${t("routing")}: ${reasons}`;
    actions.className = "actions";
    actions.append(
      actionButton(t("enable"), coordinate, "enabled"),
      actionButton(t("disable"), coordinate, "disabled"),
      actionButton(t("quarantine"), coordinate, "quarantined"),
    );
    row.append(name, summary, actions);
    target.append(row);
  });
  if (!results.length) list("search-list", [], t("noMatches"));
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CapSift-CSRF": csrfToken },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw Error("action failed");
  return response.json();
}

document.getElementById("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await loadCapabilities({ reset: true, all: true });
  } catch {
    list("search-list", [], t("searchUnavailable"));
  }
});

document.getElementById("language").addEventListener("change", async () => {
  const previousPreference = activePreference;
  const requestedPreference = document.getElementById("language").value;
  applyLocale(requestedPreference);
  document.getElementById("state").textContent = t("saving");
  try {
    await postJson("/api/language", { locale: requestedPreference });
    document.getElementById("state").textContent = t("languageSaved");
    renderCapabilityCards();
  } catch {
    applyLocale(previousPreference);
    document.getElementById("state").textContent = t("languageFailed");
  }
});

document.getElementById("load-more").addEventListener("click", async () => {
  await loadCapabilities();
});

document.getElementById("activation-filter").addEventListener("change", renderCapabilityCards);

document.getElementById("conversation-select").addEventListener("change", async (event) => {
  try {
    await loadConversation(event.target.value);
  } catch {
    list("conversation-capabilities", [], t("unavailable"));
  }
});

applyLocale("auto");
refresh();
loadCapabilities().catch(() => list("search-list", [], t("searchUnavailable")));
setInterval(refresh, 3000);
