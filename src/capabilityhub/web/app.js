const messages = {
  en: {
    eyebrow: "LOCAL / CONTROLLED", loading: "Loading live Inventory…", navigation: "Dashboard navigation",
    conversations: "Conversations", capabilityLibrary: "Capabilities", manage: "Manage", details: "System details",
    localGuide: "Local user guide", language: "Language", auto: "Auto", startHere: "START HERE",
    browseAndControl: "BROWSE AND CONTROL", projectControls: "PROJECT CONTROLS", advancedDetails: "ADVANCED DETAILS",
    localAndOffline: "LOCAL / OFFLINE", refreshConversations: "Refresh conversations", refreshing: "Refreshing…",
    conversationsRefreshed: "Conversation list refreshed.", findConversation: "Find conversation",
    conversationSearchPlaceholder: "Type a title or task ID", conversationState: "Location", activeConversations: "Active",
    archivedConversations: "Archived", chooseConversation: "Choose conversation", chooseConversationPlaceholder: "Select a conversation",
    chooseConversationHelp: "Select a conversation to inspect observed capability use.", conversationsHint: "Active and archived local Codex tasks. CapSift reads metadata and tool-call envelopes, not conversation text.",
    conversationIndexSummary: "Showing {shown} of {total} local tasks", conversationIndexTruncated: "Showing {shown} of {total} local tasks (safety limit reached)",
    observedOnly: "Coverage: scanned {calls} tool-call envelopes in this local trace. Silent native injections without an envelope cannot be proven.",
    noObservedCapabilities: "No capability calls were found in the readable tool-call envelopes for this task.",
    observedSkill: "Skill instruction read observed", observedTool: "Tool call observed",
    traceTooLarge: "This trace exceeds the 128 MB safety limit. Its messages were not read.",
    capabilityLibraryHint: "Use the filters for browsing. Use Search only when you know part of a name.",
    capabilityCount: "{value} shown", inventorySummary: "Inventory totals", specificName: "Search a specific name",
    searchPlaceholder: "Example: PDF or spreadsheet", search: "Search", clear: "Clear", filter: "Filter",
    capabilityFilters: "Capability filters", kind: "Kind", capabilityKind: "Capability kind", category: "Category", all: "All",
    activationStatus: "Status", sortBy: "Sort", tokenHighLow: "Token: high to low", tokenLowHigh: "Token: low to high",
    nameAZ: "Name: A–Z", nameZA: "Name: Z–A", enabledLabel: "On", disabledLabel: "Off", loadMore: "Load more",
    tokenNoteTitle: "Token impact", tokenNote: "Changing the switch costs 0 Token now. The estimate applies only if full instructions are loaded later.",
    avoidFutureLoad: "Avoid about", futureLoadSuffix: "Token on a future full load", futureLoadCost: "About {value} Token on a future full load",
    allowLoading: "Allow future loading", blockLoading: "Block future loading", saving: "Saving…", lifecycleSaved: "Loading preference saved.",
    lifecycleFailed: "Could not save the loading preference. The switch was restored.", viewDetails: "View details",
    capabilityDetails: "CAPABILITY DETAILS", close: "Close", provider: "Provider", revision: "Revision", operations: "Operations",
    state: "State", estimatedToken: "Estimated full-load Token", noOperations: "No advertised operations", categoryLabel: "Category",
    categoryDocuments: "Documents & office", categoryDevelopment: "Development & automation", categoryData: "Data & analysis",
    categoryMarketing: "Content & marketing", categoryDesign: "Design & media", categoryCollaboration: "Collaboration & productivity",
    categorySecurity: "Security & management", categoryOther: "Other", noMatches: "No matches. Clear Search or change a filter.",
    searchUnavailable: "Capability library unavailable.", manageHint: "Changes apply to this project. No files are deleted and no capability is executed.",
    lifecycle: "Lifecycle", lifecycleHint: "Advanced enable, disable, and quarantine controls.", approvals: "Approvals",
    approvalsHint: "Exact, expiring requests. Arguments and their digests are never displayed.", context: "Context",
    contextHint: "Metadata for disclosed sections. Pin, unpin, or forget without loading bodies again.", updates: "Updates",
    updatesHint: "Staged revision, health gate, active pointer, rollback target, and in-flight pins.",
    detailsHint: "Open only the section you need. These values do not load capability bodies.", status: "Status", snapshot: "Snapshot",
    generation: "Generation", active: "Active", inactive: "Inactive", notices: "Notices", noticesHint: "Safe counts only; sensitive paths and credentials are hidden.",
    health: "Health", healthHint: "Local wiring checks without loading capability bodies.", connections: "Connections",
    connectionsHint: "Configured state only; no network probe is performed.", providers: "Providers", providersHint: "The local adapter supplying each discovered capability.",
    loaded: "Loaded", loadedHint: "Recent successful loads from redacted audit metadata; bodies are not retained here.", reasoning: "Reasoning",
    reasoningHint: "Dashboard advisory state. Recommendations do not call a model.", currentTier: "Current tier", budgetLeft: "Budget left",
    escalations: "Escalations", auditSecurity: "Audit security", auditSecurityHint: "Optional HMAC chain status; the signing key value is never displayed.",
    secureChain: "Secure chain", keySetting: "Key setting", audit: "Audit", auditHint: "Recent redacted events; arguments and credentials are never shown.",
    guideHint: "A short guide built into this Dashboard. It works without opening an external website.", guideStep1Title: "Start with Conversations",
    guideStep1: "Refresh, choose a task, and review capability calls found in its active or archived local trace.", guideStep2Title: "Filter the capability library",
    guideStep2: "Type, category, status, Provider, and sorting update immediately. Use Search only for a specific name.", guideStep3Title: "Control future loading",
    guideStep3: "The switch changes whether CapSift may select the capability later. Switching costs 0 Token now.", guideStep4Title: "Open a card for details",
    guideStep4: "Details show compact metadata and estimated Token only; they do not load the capability body.",
    noExclusions: "No exclusions detected.", noHealth: "No health checks available.", noConnections: "No configured connections.",
    noProviders: "No Providers discovered.", noLoaded: "No successful loads recorded yet.", noApprovals: "No approval requests.",
    noContext: "No disclosed sections are resident.", noUpdates: "No staged update state.", noAudit: "No audit events recorded yet.",
    noLifecycle: "No lifecycle overrides.", live: "Live snapshot updated.", unavailable: "Snapshot unavailable; retrying.",
    languageSaved: "Language preference saved.", languageFailed: "Language preference could not be saved.", approve: "Approve", deny: "Deny",
    tokens: "{value} tokens", pinned: "pinned", evictable: "evictable", pin: "Pin", unpin: "Unpin", forget: "Forget",
    enable: "Enable", disable: "Disable", quarantine: "Quarantine", quarantineConfirm: "Quarantine {value}?", routing: "Routing",
    activeCount: "{active}/{total} active", providerState: "{provider} / {state}", updateState: "active {active} / staged {staged} / health {health}",
    configured: "configured", notConfigured: "not configured", notSelected: "not selected", global: "global", activeState: "active", inactiveState: "inactive",
    pendingState: "pending", approvedState: "approved", deniedState: "denied", consumedState: "consumed", expiredState: "expired",
    readyState: "ready", freshState: "fresh", completeState: "complete", partialState: "partial", staleState: "stale", degradedState: "degraded",
    availableState: "available", quarantinedState: "quarantined", unknownState: "unknown", reachableState: "reachable",
    unsupportedState: "unsupported", healthyState: "healthy", failedState: "failed", configuredNotProbedState: "configured, not probed",
    lowTier: "low", mediumTier: "medium", highTier: "high",
  },
  "zh-CN": {
    eyebrow: "本地 / 受控", loading: "正在读取本地状态…", navigation: "Dashboard 导航", conversations: "对话",
    capabilityLibrary: "能力库", manage: "管理", details: "系统详情", localGuide: "本地使用说明书", language: "语言", auto: "自动",
    startHere: "从这里开始", browseAndControl: "查找和控制", projectControls: "项目控制", advancedDetails: "高级详情", localAndOffline: "本地 / 离线",
    refreshConversations: "刷新对话", refreshing: "正在刷新…", conversationsRefreshed: "对话列表已刷新。", findConversation: "查找对话",
    conversationSearchPlaceholder: "输入标题或任务 ID", conversationState: "位置", activeConversations: "当前对话", archivedConversations: "已归档",
    chooseConversation: "选择对话", chooseConversationPlaceholder: "请选择一个对话", chooseConversationHelp: "选择对话后查看已检测到的能力使用情况。",
    conversationsHint: "自动读取本机当前和已归档的 Codex 任务；只扫描元数据和工具调用，不读取对话正文。",
    conversationIndexSummary: "显示 {shown}/{total} 个本地任务", conversationIndexTruncated: "显示 {shown}/{total} 个本地任务（已达到安全上限）",
    observedOnly: "覆盖范围：已扫描本地记录中的 {calls} 个工具调用。没有调用记录的 Codex 静默原生注入无法被可靠证明。",
    noObservedCapabilities: "在此任务可读取的工具调用记录中没有找到能力使用。", observedSkill: "检测到 Skill 说明读取",
    observedTool: "检测到工具调用", traceTooLarge: "此记录超过 128 MB 安全上限，因此未读取其中消息。",
    capabilityLibraryHint: "平时直接用筛选器浏览；只有知道部分名称时才使用搜索。", capabilityCount: "显示 {value} 项",
    inventorySummary: "Inventory（能力总数）", specificName: "搜索特定名称", searchPlaceholder: "例如：PDF 或表格", search: "搜索", clear: "清除",
    filter: "筛选", capabilityFilters: "能力筛选器", kind: "类型", capabilityKind: "能力类型", category: "自动分类", all: "全部",
    activationStatus: "状态", sortBy: "排序", tokenHighLow: "Token：从高到低", tokenLowHigh: "Token：从低到高",
    nameAZ: "名称：A–Z", nameZA: "名称：Z–A", enabledLabel: "开", disabledLabel: "关", loadMore: "加载更多",
    tokenNoteTitle: "Token 影响", tokenNote: "切换开关现在消耗 0 Token；只有以后完整加载说明时才可能产生下方预计用量。",
    avoidFutureLoad: "可避免以后完整加载约", futureLoadSuffix: "Token", futureLoadCost: "以后完整加载约 {value} Token",
    allowLoading: "允许以后加载", blockLoading: "阻止以后加载", saving: "正在保存…", lifecycleSaved: "加载设置已保存。",
    lifecycleFailed: "加载设置保存失败，开关已恢复。", viewDetails: "查看详情", capabilityDetails: "能力详情", close: "关闭",
    provider: "Provider（提供方）", revision: "版本", operations: "可用操作", state: "状态", estimatedToken: "完整加载预计 Token",
    noOperations: "没有声明操作", categoryLabel: "自动分类", categoryDocuments: "文档与办公", categoryDevelopment: "开发与自动化",
    categoryData: "数据与分析", categoryMarketing: "内容与营销", categoryDesign: "设计与媒体", categoryCollaboration: "协作与效率",
    categorySecurity: "安全与管理", categoryOther: "其他", noMatches: "没有匹配项；请清除搜索或更换筛选条件。", searchUnavailable: "能力库暂不可用。",
    manageHint: "设置只作用于当前项目；不会删除文件，也不会执行能力。", lifecycle: "生命周期", lifecycleHint: "高级启用、停用和隔离控制。",
    approvals: "审批", approvalsHint: "精确且会过期的请求；不显示参数及摘要。", context: "上下文", contextHint: "已披露部分的元数据；无需重载正文即可固定或忘记。",
    updates: "更新", updatesHint: "暂存版本、健康状态、活动版本、回滚目标和执行中版本。", detailsHint: "只展开你需要的部分；这些信息不会加载能力正文。",
    status: "状态", snapshot: "快照", generation: "代次", active: "已启用", inactive: "未启用", notices: "提示",
    noticesHint: "只显示安全计数；敏感路径和凭据会隐藏。", health: "健康检查", healthHint: "不加载能力正文，只检查本地接线。",
    connections: "连接", connectionsHint: "只显示配置状态，不进行网络探测。", providers: "Provider", providersHint: "显示每项能力由哪个本地适配器提供。",
    loaded: "已加载", loadedHint: "脱敏审计中的最近加载记录；这里不保留正文。", reasoning: "推理", reasoningHint: "Dashboard 建议状态；不会调用模型。",
    currentTier: "当前强度", budgetLeft: "剩余预算", escalations: "升级次数", auditSecurity: "审计安全",
    auditSecurityHint: "可选 HMAC 链状态；绝不显示签名密钥值。", secureChain: "安全链", keySetting: "密钥设置", audit: "审计记录",
    auditHint: "最近的脱敏事件；绝不显示参数和凭据。", guideHint: "内置在 Dashboard 的简明说明书，无需打开外部网站。",
    guideStep1Title: "先看对话", guideStep1: "刷新并选择任务，查看当前或已归档本地记录中的能力调用。", guideStep2Title: "筛选能力库",
    guideStep2: "类型、分类、状态、Provider 和排序都会立即生效；只在找特定名称时使用搜索。", guideStep3Title: "控制以后加载",
    guideStep3: "滑块决定 CapSift 以后能否选择该能力；切换本身消耗 0 Token。", guideStep4Title: "点击卡片看详情",
    guideStep4: "详情只显示精简元数据和预计 Token，不会加载能力正文。", noExclusions: "未发现排除项。", noHealth: "没有健康检查。",
    noConnections: "没有已配置连接。", noProviders: "没有发现 Provider。", noLoaded: "尚无成功加载记录。", noApprovals: "没有审批请求。",
    noContext: "没有驻留的已披露部分。", noUpdates: "没有暂存更新。", noAudit: "尚无审计事件。", noLifecycle: "没有生命周期覆盖项。",
    live: "本地状态已更新。", unavailable: "状态暂不可用，正在重试。", languageSaved: "语言设置已保存。", languageFailed: "语言设置保存失败。",
    approve: "批准", deny: "拒绝", tokens: "{value} Token", pinned: "已固定", evictable: "可移除", pin: "固定", unpin: "取消固定",
    forget: "忘记", enable: "启用", disable: "停用", quarantine: "隔离", quarantineConfirm: "确定隔离 {value}？", routing: "选择原因",
    activeCount: "已启用 {active}/{total}", providerState: "{provider} / {state}", updateState: "活动 {active} / 暂存 {staged} / 健康 {health}",
    configured: "已配置", notConfigured: "未配置", notSelected: "未选择", global: "全局", activeState: "已启用", inactiveState: "未启用",
    pendingState: "等待处理", approvedState: "已批准", deniedState: "已拒绝", consumedState: "已使用", expiredState: "已过期",
    readyState: "正常", freshState: "最新", completeState: "完整", partialState: "部分可用", staleState: "已过期", degradedState: "降级运行",
    availableState: "可用", quarantinedState: "已隔离", unknownState: "未知", reachableState: "可连接",
    unsupportedState: "不支持", healthyState: "健康", failedState: "失败", configuredNotProbedState: "已配置，未探测",
    lowTier: "低", mediumTier: "中", highTier: "高",
  },
};

const categoryIcons = {documents: "▤", development: "⌘", data: "◫", marketing: "◎", design: "◇", collaboration: "◉", security: "◆", other: "•"};
const categoryKeys = {documents: "categoryDocuments", development: "categoryDevelopment", data: "categoryData", marketing: "categoryMarketing", design: "categoryDesign", collaboration: "categoryCollaboration", security: "categorySecurity", other: "categoryOther"};
let activePreference = "auto";
let currentLocale = "en";
let csrfToken = "";
let capabilityEntries = [];
let capabilityNextOffset = 0;
let capabilityTotal = 0;
let capabilityQuery = "";
let conversationEntries = [];
let conversationTotal = 0;
let conversationsTruncated = false;
let activeDialogItem = null;
let latestSnapshot = null;
let latestConversation = null;

const resolvedLocale = (preference) => preference === "zh-CN" ? "zh-CN" : preference === "en" ? "en" : navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
const t = (key, values = {}) => Object.entries(values).reduce((result, [name, value]) => result.replaceAll(`{${name}}`, String(value)), messages[currentLocale][key] ?? messages.en[key] ?? key);
const text = (value) => String(value ?? "—");
const setText = (id, value) => { document.getElementById(id).textContent = text(value); };

const localizedStateKeys = {
  "not selected": "notSelected", "未选择": "notSelected", configured: "configured", "已配置": "configured",
  "not configured": "notConfigured", "未配置": "notConfigured", global: "global", "全局": "global",
  active: "activeState", "已启用": "activeState", inactive: "inactiveState", "未启用": "inactiveState",
  enabled: "activeState", "启用": "activeState", disabled: "inactiveState", "停用": "inactiveState",
  pending: "pendingState", "等待处理": "pendingState", approved: "approvedState", "已批准": "approvedState",
  denied: "deniedState", "已拒绝": "deniedState", consumed: "consumedState", "已使用": "consumedState",
  expired: "expiredState", "已过期": "expiredState", ready: "readyState", "正常": "readyState",
  fresh: "freshState", "最新": "freshState", complete: "completeState", "完整": "completeState",
  partial: "partialState", "部分可用": "partialState", stale: "staleState", degraded: "degradedState", "降级运行": "degradedState",
  available: "availableState", "可用": "availableState", quarantined: "quarantinedState", "已隔离": "quarantinedState",
  unknown: "unknownState", "未知": "unknownState", reachable: "reachableState", "可连接": "reachableState",
  unsupported: "unsupportedState", "不支持": "unsupportedState", healthy: "healthyState", "健康": "healthyState",
  failed: "failedState", fail: "failedState", "失败": "failedState", configured_not_probed: "configuredNotProbedState", "已配置，未探测": "configuredNotProbedState",
  not_configured: "notConfigured", low: "lowTier", "低": "lowTier", medium: "mediumTier", "中": "mediumTier", high: "highTier", "高": "highTier",
};

function localizedState(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  const raw = String(value).replaceAll("**", "").trim();
  const key = localizedStateKeys[raw.toLowerCase()] || localizedStateKeys[raw];
  return key ? t(key) : raw;
}

function applyLocale(preference, {rerenderSnapshot = true} = {}) {
  activePreference = preference || "auto";
  currentLocale = resolvedLocale(activePreference);
  document.documentElement.lang = currentLocale;
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((node) => { node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel)); });
  document.getElementById("language").value = activePreference;
  renderCapabilityCards();
  renderConversationOptions();
  if (rerenderSnapshot && latestSnapshot) renderSnapshot(latestSnapshot);
  if (latestConversation) renderConversation(latestConversation);
}

function showPage(name) {
  const page = document.querySelector(`[data-page-view="${name}"]`) ? name : "conversations";
  document.querySelectorAll("[data-page-view]").forEach((node) => { node.hidden = node.dataset.pageView !== page; });
  document.querySelectorAll("[data-page-link]").forEach((node) => {
    const active = node.dataset.pageLink === page;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
  });
}

function list(id, items, empty) {
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
  if (!target.children.length) { const row = document.createElement("li"); row.className = "empty-state"; row.textContent = empty; target.append(row); }
}

function renderSnapshot(payload) {
    const inventory = payload.inventory || {};
    csrfToken = payload.dashboard?.csrf_token || "";
    conversationEntries = payload.conversations?.entries || [];
    conversationTotal = Number(payload.conversations?.total || conversationEntries.length);
    conversationsTruncated = Boolean(payload.conversations?.truncated);
    renderConversationOptions();
    const kinds = inventory.active_by_kind || {};
    ["skill", "mcp", "cli", "api", "rag"].forEach((kind) => setText(`kind-${kind}`, kinds[kind] ?? 0));
    setText("snapshot-status", localizedState(inventory.status)); setText("generation", inventory.generation); setText("active-total", inventory.active_total); setText("inactive-total", inventory.inactive_count);
    const notices = Object.entries(inventory.excluded_by_reason || {}).filter(([, count]) => Number(count) > 0).map(([name, count]) => ({name, value: count}));
    list("notice-list", notices, t("noExclusions"));
    list("health-list", (payload.health?.checks || []).map((item) => ({name: item.check, value: localizedState(item.status)})), t("noHealth"));
    list("connection-list", (payload.connections?.connections || []).map((item) => ({name: `${item.kind} (${item.active}/${item.configured})`, value: localizedState(item.state)})), t("noConnections"));
    const providerSource = payload.providers?.entries || payload.providers || [];
    list("provider-list", providerSource.map((item) => ({name: item.provider || item.name, value: t("activeCount", {active: item.active ?? 0, total: item.discovered ?? 0})})), t("noProviders"));
    list("loaded-list", (payload.loaded_capabilities || []).map((item) => ({name: `${item.kind}: ${item.revision}`, value: t("providerState", {provider: item.provider, state: item.active ? t("activeState") : t("inactiveState")})})), t("noLoaded"));
    renderApprovals(payload.approvals?.approvals || []); renderContext(payload.context?.entries || []); renderLifecycle(payload.lifecycle?.entries || []);
    setText("reasoning-tier", localizedState(payload.reasoning?.current_tier, t("notSelected"))); setText("reasoning-budget", payload.reasoning?.budget?.remaining); setText("reasoning-escalations", payload.reasoning?.escalations_used ?? 0);
    list("update-list", (payload.updates?.states || []).map((item) => ({name: item.coordinate, value: t("updateState", {active: text(item.active_revision), staged: text(item.staged_revision), health: localizedState(item.health_status)})})), t("noUpdates"));
    setText("secure-audit-status", payload.secure_audit?.configured ? t("configured") : t("notConfigured")); setText("secure-audit-key", payload.secure_audit?.key_environment);
    list("audit-list", (payload.audit?.events || []).map((item) => ({name: `${item.sequence}: ${item.event_type} / ${localizedState(item.outcome)}`, value: item.capability_revision || item.reason_codes?.join(", ") || t("global")})), t("noAudit"));
}

async function refresh({ announce = true } = {}) {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw Error("status unavailable");
    const payload = await response.json();
    latestSnapshot = payload;
    applyLocale(payload.preferences?.locale || activePreference, {rerenderSnapshot: false});
    renderSnapshot(payload);
    if (announce) document.getElementById("state").textContent = t("live");
  } catch { document.getElementById("state").textContent = t("unavailable"); }
}

async function loadCapabilities({reset = false, all = false} = {}) {
  if (reset) { capabilityEntries = []; capabilityNextOffset = 0; }
  do {
    const params = new URLSearchParams({q: "", offset: String(capabilityNextOffset || 0), limit: "500"});
    const kind = document.getElementById("search-kind").value;
    if (kind) params.set("kind", kind);
    const response = await fetch(`/api/capabilities?${params}`, {cache: "no-store"});
    if (!response.ok) throw Error("capability listing failed");
    const payload = await response.json();
    capabilityEntries.push(...(payload.entries || [])); capabilityTotal = Number(payload.total || 0); capabilityNextOffset = payload.next_offset;
  } while (all && capabilityNextOffset !== null);
  updateProviderFilter(); renderCapabilityCards();
}

const normalize = (value) => String(value || "").normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
const fuzzyContains = (haystack, needle) => {
  if (!needle) return true;
  if (haystack.includes(needle)) return true;
  let index = 0;
  for (const character of haystack) if (character === needle[index]) index += 1;
  return index === needle.length;
};

function visibleCapabilities() {
  const activation = document.getElementById("activation-filter").value;
  const category = document.getElementById("category-filter").value;
  const provider = document.getElementById("provider-filter").value;
  const query = normalize(capabilityQuery);
  const words = query.split(" ").filter(Boolean);
  const entries = capabilityEntries.filter((item) => {
    if (activation && (activation === "enabled") !== Boolean(item.active)) return false;
    if (category && item.category !== category) return false;
    if (provider && item.provider !== provider) return false;
    const searchable = normalize([item.coordinate, item.summary, item.provider, ...(item.operations || [])].join(" "));
    return words.every((word) => fuzzyContains(searchable, word));
  });
  const sort = document.getElementById("capability-sort").value;
  return entries.sort((left, right) => {
    if (sort === "token-desc") return Number(right.estimated_load_tokens || 0) - Number(left.estimated_load_tokens || 0) || left.coordinate.localeCompare(right.coordinate);
    if (sort === "token-asc") return Number(left.estimated_load_tokens || 0) - Number(right.estimated_load_tokens || 0) || left.coordinate.localeCompare(right.coordinate);
    return sort === "name-desc" ? right.coordinate.localeCompare(left.coordinate) : left.coordinate.localeCompare(right.coordinate);
  });
}

function updateProviderFilter() {
  const select = document.getElementById("provider-filter");
  const selected = select.value;
  const first = select.options[0];
  select.replaceChildren(first);
  [...new Set(capabilityEntries.map((item) => item.provider).filter(Boolean))].sort().forEach((provider) => {
    const option = document.createElement("option"); option.value = provider; option.textContent = provider; select.append(option);
  });
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
}

function tokenImpact(item) {
  const wrap = document.createElement("span"); wrap.className = "token-impact";
  if (item.active) {
    wrap.append(document.createTextNode(`${t("avoidFutureLoad")} `));
    const value = document.createElement("strong"); value.textContent = text(item.estimated_load_tokens || 0); wrap.append(value, document.createTextNode(` ${t("futureLoadSuffix")}`));
  } else wrap.textContent = t("futureLoadCost", {value: item.estimated_load_tokens || 0});
  return wrap;
}

function createSwitch(item, {dialog = false} = {}) {
  const button = document.createElement("button");
  button.type = "button"; button.className = "switch-control"; button.setAttribute("role", "switch"); button.setAttribute("aria-checked", String(Boolean(item.active)));
  button.setAttribute("aria-label", item.active ? t("blockLoading") : t("allowLoading"));
  const track = document.createElement("span"); track.className = "switch-track"; track.setAttribute("aria-hidden", "true"); track.append(document.createElement("span"));
  const label = document.createElement("span"); label.className = "switch-label"; label.textContent = item.active ? t("enabledLabel") : t("disabledLabel");
  button.append(track, label);
  button.addEventListener("click", async (event) => { event.stopPropagation(); await setCapabilityState(item, !item.active, dialog); });
  return button;
}

async function setCapabilityState(item, enabled, dialog = false) {
  const previous = Boolean(item.active);
  item.active = enabled; item.state = enabled ? "enabled" : "disabled"; renderCapabilityCards(); if (dialog) openCapabilityDialog(item);
  document.getElementById("state").textContent = t("saving");
  try {
    await postJson("/api/lifecycle", {coordinate: item.coordinate, state: item.state});
    document.getElementById("state").textContent = t("lifecycleSaved");
    await refresh({announce: false});
    await loadCapabilities({reset: true, all: true});
  } catch {
    item.active = previous; item.state = previous ? "enabled" : "disabled"; renderCapabilityCards(); if (dialog) openCapabilityDialog(item);
    document.getElementById("state").textContent = t("lifecycleFailed");
  }
}

function renderCapabilityCards() {
  const target = document.getElementById("search-list");
  if (!target) return;
  target.replaceChildren();
  const entries = visibleCapabilities();
  entries.forEach((item) => {
    const row = document.createElement("li"); row.className = `capability-card category-${item.category || "other"}`;
    const heading = document.createElement("div"); heading.className = "card-heading";
    const category = document.createElement("span"); category.className = "category-badge"; category.textContent = `${categoryIcons[item.category] || "•"} ${t(categoryKeys[item.category] || "categoryOther")}`;
    const kind = document.createElement("span"); kind.className = `kind-badge kind-${item.kind}`; kind.textContent = String(item.kind).toUpperCase(); heading.append(category, kind);
    const name = document.createElement("strong"); name.className = "capability-name"; name.textContent = item.coordinate;
    const provider = document.createElement("span"); provider.className = "provider-line"; provider.textContent = `${t("provider")}: ${text(item.provider)}`;
    const summary = document.createElement("p"); summary.textContent = text(item.summary);
    const footer = document.createElement("div"); footer.className = "card-footer";
    const impact = tokenImpact(item);
    const controls = document.createElement("div"); controls.className = "card-controls";
    const details = document.createElement("button"); details.type = "button"; details.className = "text-button"; details.textContent = t("viewDetails"); details.addEventListener("click", () => openCapabilityDialog(item));
    controls.append(details, createSwitch(item)); footer.append(impact, controls);
    row.append(heading, name, provider, summary, footer); target.append(row);
  });
  if (!entries.length) list("search-list", [], t("noMatches"));
  document.getElementById("capability-count").textContent = t("capabilityCount", {value: entries.length});
  document.getElementById("load-more").hidden = capabilityNextOffset === null;
}

function addDetailField(target, name, value) {
  const wrap = document.createElement("div"); const term = document.createElement("dt"); const description = document.createElement("dd");
  term.textContent = name; description.textContent = value; wrap.append(term, description); target.append(wrap);
}

function openCapabilityDialog(item) {
  activeDialogItem = item;
  setText("capability-dialog-title", item.coordinate); setText("capability-dialog-summary", item.summary);
  const fields = document.getElementById("capability-dialog-fields"); fields.replaceChildren();
  addDetailField(fields, t("categoryLabel"), t(categoryKeys[item.category] || "categoryOther")); addDetailField(fields, t("kind"), String(item.kind).toUpperCase());
  addDetailField(fields, t("provider"), text(item.provider)); addDetailField(fields, t("state"), item.active ? t("enabledLabel") : t("disabledLabel"));
  addDetailField(fields, t("operations"), (item.operations || []).join(", ") || t("noOperations")); addDetailField(fields, t("revision"), text(item.revision));
  setText("capability-dialog-token", t("futureLoadCost", {value: item.estimated_load_tokens || 0}));
  const old = document.getElementById("capability-dialog-toggle"); const replacement = createSwitch(item, {dialog: true}); replacement.id = old.id; old.replaceWith(replacement);
  const dialog = document.getElementById("capability-dialog"); if (!dialog.open) dialog.showModal();
}

function filteredConversations() {
  const query = normalize(document.getElementById("conversation-query").value);
  const state = document.getElementById("conversation-state").value;
  return conversationEntries.filter((item) => {
    if (state === "archived" && !item.archived) return false; if (state === "active" && item.archived) return false;
    return fuzzyContains(normalize(`${item.title} ${item.id}`), query);
  });
}

function renderConversationOptions() {
  const select = document.getElementById("conversation-select"); if (!select) return;
  const selected = select.value; select.replaceChildren();
  const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = t("chooseConversationPlaceholder"); select.append(placeholder);
  const visible = filteredConversations();
  visible.forEach((item) => { const option = document.createElement("option"); option.value = item.id; const marker = item.archived ? ` · ${t("archivedConversations")}` : ""; option.textContent = `${item.title}${marker} · ${String(item.updated_at).replace("T", " ").slice(0, 16)}`; select.append(option); });
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
  const key = conversationsTruncated ? "conversationIndexTruncated" : "conversationIndexSummary";
  document.getElementById("conversation-index-summary").textContent = t(key, {shown: visible.length, total: conversationTotal});
}

function renderConversation(payload) {
  if (payload.status === "trace_too_large") list("conversation-capabilities", [], t("traceTooLarge"));
  else list("conversation-capabilities", (payload.capabilities || []).map((item) => ({name: `${String(item.kind).toUpperCase()} · ${item.name}`, value: item.source === "observed_skill_instruction_read" ? t("observedSkill") : t("observedTool")})), t("noObservedCapabilities"));
  setText("conversation-summary", t("observedOnly", {calls: payload.coverage?.tool_envelopes ?? 0}));
}

async function loadConversation(taskId) {
  if (!taskId) { latestConversation = null; document.getElementById("conversation-capabilities").replaceChildren(); setText("conversation-summary", t("chooseConversationHelp")); return; }
  setText("conversation-summary", t("loading"));
  const response = await fetch(`/api/conversation?id=${encodeURIComponent(taskId)}`, {cache: "no-store"}); if (!response.ok) throw Error("conversation unavailable");
  latestConversation = await response.json();
  renderConversation(latestConversation);
}

const approvalButton = (label, approvalId, decision) => { const button = document.createElement("button"); button.type = "button"; button.textContent = label; button.addEventListener("click", async () => { await postJson("/api/approval", {approval_id: approvalId, decision}); await refresh(); }); return button; };
function renderApprovals(entries) { const target = document.getElementById("approval-list"); target.replaceChildren(); entries.forEach((item) => { const row = document.createElement("li"); const name = document.createElement("strong"); const state = document.createElement("span"); name.textContent = `${text(item.operation)} / ${localizedState(item.status)}`; state.textContent = `${text(item.revision)} / ${text(item.expires_at)}`; row.append(name, state); if (item.status === "pending") { const actions = document.createElement("div"); actions.className = "actions"; actions.append(approvalButton(t("approve"), item.approval_id, "approve"), approvalButton(t("deny"), item.approval_id, "deny")); row.append(actions); } target.append(row); }); if (!entries.length) list("approval-list", [], t("noApprovals")); }
const contextButton = (label, key, action) => { const button = document.createElement("button"); button.type = "button"; button.textContent = label; button.addEventListener("click", async () => { await postJson("/api/context", {key, action}); await refresh(); }); return button; };
function renderContext(entries) { const target = document.getElementById("context-list"); target.replaceChildren(); entries.forEach((item) => { const row = document.createElement("li"); const name = document.createElement("strong"); const state = document.createElement("span"); const actions = document.createElement("div"); name.textContent = text(item.section); state.textContent = `${t("tokens", {value: text(item.portable_tokens)})} / ${item.pinned ? t("pinned") : t("evictable")}`; actions.className = "actions"; actions.append(contextButton(item.pinned ? t("unpin") : t("pin"), item.key, item.pinned ? "unpin" : "pin"), contextButton(t("forget"), item.key, "remove")); row.append(name, state, actions); target.append(row); }); if (!entries.length) list("context-list", [], t("noContext")); }
const actionButton = (label, coordinate, state) => { const button = document.createElement("button"); button.type = "button"; button.textContent = label; button.addEventListener("click", async () => { if (state === "quarantined" && !confirm(t("quarantineConfirm", {value: coordinate}))) return; await postJson("/api/lifecycle", {coordinate, state}); await refresh(); await loadCapabilities({reset: true, all: true}); }); return button; };
function renderLifecycle(entries) { const target = document.getElementById("lifecycle-list"); target.replaceChildren(); entries.forEach((item) => { const row = document.createElement("li"); const name = document.createElement("strong"); const state = document.createElement("span"); const actions = document.createElement("div"); name.textContent = text(item.coordinate); state.textContent = `${localizedState(item.state)} / ${item.active ? t("activeState") : t("inactiveState")}`; actions.className = "actions"; actions.append(actionButton(t("enable"), item.coordinate, "enabled"), actionButton(t("disable"), item.coordinate, "disabled"), actionButton(t("quarantine"), item.coordinate, "quarantined")); row.append(name, state, actions); target.append(row); }); if (!entries.length) list("lifecycle-list", [], t("noLifecycle")); }
async function postJson(path, body) { const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-CapSift-CSRF": csrfToken}, body: JSON.stringify(body)}); if (!response.ok) throw Error("action failed"); return response.json(); }

document.getElementById("search-form").addEventListener("submit", async (event) => { event.preventDefault(); capabilityQuery = document.getElementById("search-query").value.trim(); renderCapabilityCards(); });
document.getElementById("clear-search").addEventListener("click", () => { document.getElementById("search-query").value = ""; capabilityQuery = ""; renderCapabilityCards(); });
document.getElementById("search-kind").addEventListener("change", () => loadCapabilities({reset: true, all: true}).catch(() => list("search-list", [], t("searchUnavailable"))));
["category-filter", "activation-filter", "provider-filter", "capability-sort"].forEach((id) => document.getElementById(id).addEventListener("change", renderCapabilityCards));
document.getElementById("load-more").addEventListener("click", () => loadCapabilities());
document.getElementById("conversation-query").addEventListener("input", renderConversationOptions); document.getElementById("conversation-state").addEventListener("change", renderConversationOptions);
document.getElementById("conversation-select").addEventListener("change", (event) => loadConversation(event.target.value).catch(() => list("conversation-capabilities", [], t("unavailable"))));
document.getElementById("refresh-conversations").addEventListener("click", async (event) => { const button = event.currentTarget; button.disabled = true; button.textContent = t("refreshing"); await refresh({announce: false}); const selected = document.getElementById("conversation-select").value; if (selected) await loadConversation(selected); button.disabled = false; button.textContent = t("refreshConversations"); document.getElementById("state").textContent = t("conversationsRefreshed"); });
document.getElementById("language").addEventListener("change", async () => { const previous = activePreference; const requested = document.getElementById("language").value; applyLocale(requested); try { await postJson("/api/language", {locale: requested}); document.getElementById("state").textContent = t("languageSaved"); } catch { applyLocale(previous); document.getElementById("state").textContent = t("languageFailed"); } });
document.getElementById("close-capability-dialog").addEventListener("click", () => document.getElementById("capability-dialog").close());
document.getElementById("capability-dialog").addEventListener("click", (event) => { if (event.target === event.currentTarget) event.currentTarget.close(); });
window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));

applyLocale("auto"); showPage(location.hash.slice(1) || "conversations"); refresh(); loadCapabilities({reset: true, all: true}).catch(() => list("search-list", [], t("searchUnavailable"))); setInterval(() => refresh({announce: false}), 3000);
