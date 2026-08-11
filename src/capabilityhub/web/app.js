const text = (value) => String(value ?? "—");
let csrfToken = "";

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
    list("notice-list", notices, "No exclusions detected.");

    const checks = (payload.health?.checks || []).map((item) => ({
      name: item.check,
      value: item.status,
    }));
    list("health-list", checks, "No health checks available.");
    const connections = (payload.connections?.connections || []).map((item) => ({
      name: `${item.kind} (${item.active}/${item.configured})`,
      value: item.state,
    }));
    list("connection-list", connections, "No configured connections.");
    const providerSource = payload.providers?.entries || payload.providers || [];
    const providers = providerSource.map((item) => ({
      name: item.provider || item.name,
      value: `${item.active ?? 0}/${item.discovered ?? 0} active`,
    }));
    list("provider-list", providers, "No Providers discovered.");
    const loaded = (payload.loaded_capabilities || []).map((item) => ({
      name: `${item.kind}: ${item.revision}`,
      value: `${item.provider} / ${item.active ? "active" : "inactive"}`,
    }));
    list("loaded-list", loaded, "No successful loads recorded yet.");
    renderApprovals(payload.approvals?.approvals || []);
    renderContext(payload.context?.entries || []);
    setText("reasoning-tier", payload.reasoning?.current_tier || "not selected");
    setText("reasoning-budget", payload.reasoning?.budget?.remaining);
    setText("reasoning-escalations", payload.reasoning?.escalations_used ?? 0);
    const audit = (payload.audit?.events || []).map((item) => ({
      name: `${item.sequence}: ${item.event_type} / ${item.outcome}`,
      value: item.capability_revision || item.reason_codes?.join(", ") || "global",
    }));
    list("audit-list", audit, "No audit events recorded yet.");
    document.getElementById("language").value = payload.preferences?.locale || "auto";
    renderLifecycle(payload.lifecycle?.entries || []);
    document.getElementById("state").textContent = "Live snapshot updated.";
  } catch {
    document.getElementById("state").textContent = "Snapshot unavailable; retrying.";
  }
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
        approvalButton("Approve", item.approval_id, "approve"),
        approvalButton("Deny", item.approval_id, "deny"),
      );
      row.append(actions);
    }
    target.append(row);
  });
  if (!entries.length) list("approval-list", [], "No approval requests.");
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
    state.textContent = `${text(item.portable_tokens)} tokens / ${item.pinned ? "pinned" : "evictable"}`;
    actions.className = "actions";
    actions.append(
      contextButton(item.pinned ? "Unpin" : "Pin", item.key, item.pinned ? "unpin" : "pin"),
      contextButton("Forget", item.key, "remove"),
    );
    row.append(name, state, actions);
    target.append(row);
  });
  if (!entries.length) list("context-list", [], "No disclosed sections are resident.");
}

const coordinateFromRevision = (revision) => String(revision).split("@", 1)[0];

const actionButton = (label, coordinate, state) => {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    if (state === "quarantined" && !confirm(`Quarantine ${coordinate}?`)) return;
    await postJson("/api/lifecycle", { coordinate, state });
    await refresh();
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
    state.textContent = `${text(item.state)} / ${item.active ? "active" : "inactive"}`;
    actions.className = "actions";
    actions.append(
      actionButton("Enable", item.coordinate, "enabled"),
      actionButton("Disable", item.coordinate, "disabled"),
      actionButton("Quarantine", item.coordinate, "quarantined"),
    );
    row.append(name, state, actions);
    target.append(row);
  });
  if (!entries.length) list("lifecycle-list", [], "No lifecycle overrides. Search to manage a capability.");
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
    summary.textContent = `${text(item.summary)} / Routing: ${reasons}`;
    actions.className = "actions";
    actions.append(
      actionButton("Enable", coordinate, "enabled"),
      actionButton("Disable", coordinate, "disabled"),
      actionButton("Quarantine", coordinate, "quarantined"),
    );
    row.append(name, summary, actions);
    target.append(row);
  });
  if (!results.length) list("search-list", [], "No matching capabilities.");
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CapabilityHub-CSRF": csrfToken },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw Error("action failed");
  return response.json();
}

document.getElementById("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = document.getElementById("search-query").value;
  const kind = document.getElementById("search-kind").value;
  const params = new URLSearchParams({ q: query, limit: "5" });
  if (kind) params.set("kind", kind);
  try {
    const response = await fetch(`/api/search?${params}`, { cache: "no-store" });
    if (!response.ok) throw Error("search failed");
    renderSearch((await response.json()).results || []);
  } catch {
    list("search-list", [], "Search unavailable.");
  }
});

document.getElementById("save-language").addEventListener("click", async () => {
  try {
    await postJson("/api/language", { locale: document.getElementById("language").value });
    document.getElementById("state").textContent = "Language preference saved.";
  } catch {
    document.getElementById("state").textContent = "Language preference could not be saved.";
  }
});

refresh();
setInterval(refresh, 3000);
