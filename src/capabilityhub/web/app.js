const text = (value) => String(value ?? "—");

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
    document.getElementById("state").textContent = "Live snapshot updated.";
  } catch {
    document.getElementById("state").textContent = "Snapshot unavailable; retrying.";
  }
}

refresh();
setInterval(refresh, 3000);
