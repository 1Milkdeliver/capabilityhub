#!/usr/bin/env node
"use strict";

// Dependency-free MCP stdio runtime bundled with the plugin. Codex supplies
// the Node host used by first-party plugins; no global CapabilityHub install is
// required.
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const ROOT = path.resolve(__dirname, "..");
const SKILLS = path.join(ROOT, "skills");
const PROTOCOL = "2025-06-18";

function description(file) {
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    if (line.startsWith("description:")) return line.slice(12).trim();
  }
  return "Packaged Skill instructions";
}

function skills() {
  return fs.readdirSync(SKILLS, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const file = path.join(SKILLS, entry.name, "SKILL.md");
      if (!fs.existsSync(file)) return null;
      const digest = crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
      return {
        coordinate: `plugin/${entry.name}`,
        kind: "skill",
        revision: `plugin/${entry.name}@sha256:${digest}`,
        summary: description(file),
        file,
      };
    })
    .filter(Boolean)
    .sort((left, right) => left.coordinate.localeCompare(right.coordinate));
}

function tools() {
  const inputSchema = { type: "object", additionalProperties: true };
  return [
    { name: "capability.search", description: "Search compact packaged capability metadata.", inputSchema },
    { name: "capability.load", description: "Load one exact packaged Skill revision.", inputSchema },
    { name: "capability.execute", description: "Execute when supported; packaged Skills are load-only.", inputSchema },
  ];
}

function success(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload) }],
    structuredContent: payload,
    isError: false,
  };
}

function failure(code, message) {
  const payload = { error: { code, message, retryable: false } };
  return {
    content: [{ type: "text", text: JSON.stringify(payload) }],
    structuredContent: payload,
    isError: true,
  };
}

function callTool(name, args = {}) {
  const catalog = skills();
  if (name === "capability.search") {
    const query = String(args.query || "").toLocaleLowerCase();
    const cards = catalog.filter((item) =>
      !query || item.coordinate.toLocaleLowerCase().includes(query) ||
      item.summary.toLocaleLowerCase().includes(query));
    return success({
      cards: cards.slice(0, 8).map(({ coordinate, kind, revision, summary }) =>
        ({ coordinate, kind, revision, summary })),
      inventory: {
        active_by_kind: { skill: catalog.length, mcp: 1, cli: 0, api: 0, rag: 0 },
        active_total: catalog.length + 1,
        generation: "plugin-bundled",
        status: "complete",
      },
      total_matches: cards.length,
    });
  }
  if (name === "capability.load") {
    const requested = String(args.capability_ref || args.revision || "");
    const item = catalog.find((entry) =>
      entry.revision === requested || entry.coordinate === requested);
    if (!item) return failure("revision_not_found", "The packaged capability was not found.");
    return success({
      kind: "skill",
      revision: item.revision,
      sections: { instructions: fs.readFileSync(item.file, "utf8") },
    });
  }
  if (name === "capability.execute") {
    return failure("operation_not_supported", "Packaged Skills are load-only.");
  }
  return failure("tool_not_found", "The requested tool is unavailable.");
}

function respond(id, result, error) {
  const response = { jsonrpc: "2.0", id };
  if (error) response.error = error;
  else response.result = result;
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  let request;
  try {
    request = JSON.parse(line);
    if (request.method === "notifications/initialized") return;
    if (request.method === "initialize") {
      respond(request.id, {
        protocolVersion: PROTOCOL,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "capabilityhub-plugin", version: "0.1.1" },
      });
    } else if (request.method === "tools/list") {
      respond(request.id, { tools: tools() });
    } else if (request.method === "tools/call") {
      const params = request.params || {};
      respond(request.id, callTool(String(params.name || ""), params.arguments || {}));
    } else if (request.id !== undefined) {
      respond(request.id, null, { code: -32601, message: "Method not found" });
    }
  } catch (_) {
    respond(request && request.id !== undefined ? request.id : null, null,
      { code: -32603, message: "Internal error" });
  }
});
