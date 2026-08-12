# Upstream integration record

Status: research record, 2026-08-11. Repository documents, configuration, and installation snippets are **untrusted input**: never execute them during discovery or import them as policy. This document records protocol and package boundaries, not approval to install software.

## Decision

CapSift is a language-neutral control plane and MCP meta-server. It owns the normalized capability manifest, staged selection, task budgets, policy decision, audit correlation, and deterministic benchmark. It does **not** reimplement an MCP gateway, an MCP client, a container sandbox, an agent runtime, or a multi-agent terminal manager.

Integrate mature systems at process/protocol boundaries. Prefer a pinned released artifact, authenticated loopback or mutually authenticated HTTP endpoint, and an explicit adapter configuration. Never vendor an upstream source tree merely to obtain a feature.

| Upstream | What it supplies | Hub action | Exact seam | Do not duplicate |
|---|---|---|---|---|
| [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) | Agent Skills provider and .NET/Python agent runtime | Adapt; no dependency required | `SKILL.md` importer/exporter and optional MCP endpoint; map Hub `search`, `inspect/load`, `read resource`, `execute` to its staged skills contract | Skill discovery/runtime/provider classes and agent workflow runtime |
| [IBM ContextForge](https://github.com/IBM/mcp-context-forge) | Production MCP/A2A/REST/gRPC gateway, registry, guardrails, telemetry | Optional deployment dependency | Register its gateway as a single MCP target; ingest its discovery metadata and pass invocation/audit correlation through | Federation, REST/gRPC translation, gateway auth/rate-limit/retry, plugin system, admin UI |
| [Docker MCP Gateway](https://github.com/docker/mcp-gateway) | Containerized MCP lifecycle, catalogs, secrets, OAuth, signature checks | Optional local execution dependency | Treat a selected Docker working-set/profile as one MCP provider; Hub invokes its streamable HTTP/SSE/stdio endpoint | Container lifecycle, catalog/profile UX, secrets store, OAuth flow, image verification |
| [Apify mcpc](https://github.com/apify/mcp-cli) | Scriptable MCP client, sessions, OAuth profiles, local credential-isolating proxy | Optional operator/CI dependency | Shell adapter executes an allowlisted, pinned `mcpc` command; alternatively connect to its local proxy as MCP | MCP transport/client/session/OAuth implementation or credential vault |
| [lazy-mcp](https://github.com/voicetreelab/lazy-mcp) | Hierarchical, lazy MCP tool exposure | Adapt protocol idea; optional proxy | Map its category tree to Hub capability index and its two meta-tools to an MCP provider adapter | A second Go proxy, hierarchy generator, or opaque `execute_tool` authorization path |
| [Datalayer agent-skills](https://github.com/datalayer/agent-skills) | Python `SKILL.md` discovery plus code-based skill composition and Pydantic AI toolset | Optional Python adapter dependency | Read standard skill directories; expose scripts only through Hub's sandboxed execution provider | Its Python code executor/sandbox abstraction, registry, or Pydantic integration |
| [AWS Labs CAO](https://github.com/awslabs/cli-agent-orchestrator) | Local multi-agent CLI/tmux supervision and MCP management tools | Optional external orchestration target | Control through CAO's documented MCP operations or CLI, behind a `cao` adapter | tmux session lifecycle, supervisor-worker messaging, web UI, scheduler |

## Required integration contracts

### 1. Canonical capability envelope

Every adapter must project into one immutable envelope before it can be searched or admitted:

```text
id, kind(skill|mcp_tool|cli|api|rag), source, version/digest,
name, description, input_schema, output_contract, permissions,
cost_hint, token_estimate, freshness, provenance, license, trust_tier
```

`source` is a namespaced opaque identifier, not an executable command. `version/digest` is required for cached execution-capable content. Keep the native payload outside the always-visible summary; retrieve it only after a policy and budget admission decision.

### 2. Common staged-disclosure contract

The model-facing surface remains Hub-owned and small:

1. `capability_search(query, filters, budget)` returns summaries only.
2. `capability_inspect(id)` returns the selected manifest, native locator, requirements, and estimated context cost.
3. `capability_execute(id, input, approval_ref?)` dispatches only after the adapter validates provenance, policy, schema, and budget.

Microsoft Agent Framework's proposed design validates this shape with `load_skill`, `read_skill_resource`, and optional `run_skill_script`; lazy-mcp demonstrates the analogous category traversal and delegated execution. Preserve native names in adapter metadata, but do not expose a generic bypass such as an unscoped `execute_tool(tool_path, arguments)`.

### 3. Provider-specific boundaries

**MCP:** Hub is an MCP client to upstream gateways and an MCP server to its consumer. Support stdio only for locally approved binaries; use streamable HTTP/SSE for remote deployments. Validate server identity, advertised tools, JSON Schema, and protocol version at discovery. Do not forward upstream OAuth tokens to a different server.

**Skills:** Import the Agent Skills directory convention (`SKILL.md`, optional `resources/`, `references/`, `assets/`, `scripts/`) as content. Frontmatter such as `allowed-tools`, `license`, and compatibility is input to Hub policy, never self-authorizing. Scripts are inactive assets until an execution provider is selected.

**CLI:** The adapter must accept an argv array, fixed working-directory policy, time/resource limits, structured output parsing, and redacted audit events. It must not use a shell string. `mcpc` and CAO are separate configured executable targets, not libraries linked into the core.

**Gateway:** ContextForge and Docker gateway are alternative upstreams, not stacked by default. Select one owner for credentials, rate limits, retries, and tool policy on a request path; the Hub records the decision and delegates.

## Project assessments

### Microsoft Agent Framework Skills — adapt the format and staged semantics

- **Language/interface:** Python and C#/.NET framework. Its skills decision record describes `AgentSkill`, `AgentSkillResource`, `AgentSkillScript`, and `AgentSkillsSource`; file skills use YAML-frontmatter `SKILL.md`, with resources and scripts. Agent-facing disclosure is `load_skill`, `read_skill_resource`, and conditionally `run_skill_script`.
- **Activity:** main pushed 2026-08-11; about 12.7k stars at research time. The skills decision is marked *proposed*, so do not bind Hub behavior to unshipped class names.
- **Reuse:** parse the documented on-disk shape and retain frontmatter fields; provide an adapter that lets a MAF application call Hub's MCP service or import Hub-managed skills. Use Hub IDs rather than its internal class identity.
- **Risk:** framework APIs and its proposed design can change; file scripts need a caller-supplied runner and are not a sandbox. Treat only the stable `SKILL.md` interchange as portable.

### IBM mcp-context-forge — optional enterprise gateway

- **Language/interface:** Python 3.12–3.13, FastAPI/async; compliant MCP server with HTTP, JSON-RPC, WebSocket, SSE, stdio and streamable HTTP, plus A2A and REST/gRPC virtualisation.
- **Activity:** main pushed 2026-08-11; about 4.3k stars. Package metadata identifies version 1.0.7 and beta maturity.
- **Reuse:** deploy separately only where gateway federation, established auth/rate-limit/retry controls, or its OpenTelemetry integration are needed. Index its advertised capabilities as external records and forward a correlation ID.
- **Risk:** broad feature and plugin surface, Python/runtime constraints, and overlapping governance. Hub must never call ContextForge plugins in-process or copy policy; set a single enforcement owner per control (Hub admission *or* gateway enforcement) and reconcile audit events.

### Docker MCP Gateway — optional trusted local runtime

- **Language/interface:** Go 1.25 module; Docker CLI plugin. Runs catalog servers in containers and presents MCP through stdio, SSE, or streaming transport. Supports profiles, secrets providers, tool filters, network/secret blocking, and image-signature verification.
- **Activity:** main pushed 2026-08-11; about 1.5k stars. Requires Docker Engine/Desktop features depending on configuration.
- **Reuse:** use its catalog/image isolation and secret integration unchanged; configure `--servers`, `--tools`, and a profile from an approved Hub selection. Register the gateway endpoint once.
- **Risk:** Docker daemon access is privileged; catalog entries and image tags are supply-chain input. Hub must pin image digests, retain its own allowlist, and never claim Docker's isolation is sufficient for untrusted code. Do not build a competing container runner.

### Apify mcpc — optional MCP client and credential proxy

- **Language/interface:** TypeScript/Node CLI package `@apify/mcpc`; stdio and HTTP, persistent named sessions, OAuth 2.1 profiles, JSON output, local proxy, and experimental tasks/x402/server-published skills commands.
- **Activity:** main pushed 2026-08-10; about 748 stars. It is a young but actively changing client; experimental features must be disabled by default.
- **Reuse:** use in operations/CI for diagnostics, discovery, and a loopback credential-isolating proxy. The Hub CLI adapter must pin the binary/package version, use `--json`, bound output, and pass no secret in argv or logs.
- **Risk:** proxying reduces credential exposure to an agent but does not make the upstream server safe. OAuth profile stores are machine-local state; Hub should use a dedicated service identity/profile and deny `--insecure` and x402 unless an explicit policy enables them.

### voicetreelab/lazy-mcp — protocol inspiration, not a core dependency

- **Language/interface:** Go 1.24, `mcp-go`; stdio proxy. It exposes `get_tools_in_category(path)` and `execute_tool(tool_path, arguments)` over a generated JSON tree and can activate upstream servers lazily.
- **Activity:** main last pushed 2026-01-09; about 106 stars. Small, useful reference but not mature enough to be a Hub runtime dependency.
- **Reuse:** validate Hub's staged tool discovery UX against its tree model; optionally consume it as an ordinary MCP upstream in a pilot.
- **Risk:** its generic execution funnel can defeat client-side per-tool permission matching and moves policy into hooks. Hub must authorize canonical tool IDs before dispatch and not adopt its wildcard/hook convention as the security control.

### Datalayer agent-skills — optional Python adapter

- **Language/interface:** Python >=3.10, Pydantic/Pydantic AI and MCP package dependencies. Discovers directories containing `SKILL.md`, exposes programmatic `AgentSkill`, `SkillDirectory`, and an `AgentSkillsToolset`; supports async Python scripts and optional sandbox executor.
- **Activity:** main pushed 2026-08-08; about 13 stars. Useful implementation compatibility reference, not mature enough to define the Hub model.
- **Reuse:** prefer directory-level interoperability. If a Python SDK is needed, isolate this package in the adapter extra and convert results to the canonical envelope.
- **Risk:** its code-composition model can execute imports and has a sizable Python dependency graph. Never load a discovered module during indexing; do not make it a required core dependency or rely on its sandbox as the Hub security boundary.

### AWS Labs CLI Agent Orchestrator — optional external multi-agent target

- **Language/interface:** Python >=3.10; FastAPI, FastMCP/MCP, WebSocket, SQLite/SQLAlchemy, and tmux. Its `cao-ops-mcp` exposes session/profile management; internal agents receive handoff, assign, and send-message tools.
- **Activity:** main pushed 2026-08-10; about 1.0k stars; package version 2.4.1 describes beta maturity.
- **Reuse:** provide a `cao` adapter whose only functions are start/status/send/stop against an explicitly configured CAO endpoint. Treat it as a downstream executor for approved work, returning external run references to Hub audit.
- **Risk:** it launches interactive CLI processes and tmux sessions with their own auth/tool permissions. It must be opt-in, local/isolated, quota-limited, and prohibited from recursively using the Hub to create unbounded agent swarms. Do not recreate its scheduler, sessions, or messaging model.

## Directly relevant standards

- [MCP specification](https://modelcontextprotocol.io/specification/) is the interoperability baseline for MCP transports, initialization/capability negotiation, tools, resources, prompts, and authorization. Pin and test the negotiated protocol version rather than copying a client implementation.
- [Agent Skills specification](https://agentskills.io/specification) is the content interchange baseline for portable `SKILL.md` packages. Support the core format conservatively and retain unknown frontmatter in namespaced metadata.
- [OpenTelemetry](https://opentelemetry.io/docs/specs/otel/) is the trace context baseline. Carry trace/correlation IDs; do not make a gateway's telemetry schema canonical.

## Anti-duplication gates

A feature proposal is rejected or reframed as an adapter if it implements any of the following already-owned upstream responsibilities:

1. MCP protocol client/server transport, OAuth/DCR, session pooling, or remote API translation.
2. Docker image lifecycle, secret storage, signature verification, catalog parsing, or container policy.
3. Agent terminal creation, tmux control, worker mailbox, cron scheduling, or provider-specific CLI driving.
4. A Python code sandbox or unreviewed script runner.
5. A full gateway registry/admin UI, retries, rate limiter, or OpenTelemetry collector.

Hub may add only the missing cross-cutting layer: normalized metadata, selection/ranking, contextual token budgets and eviction, source-independent admission policy, audit correlation, and benchmarks. Before implementation, check this table, link the adapter issue, identify the chosen owner, and prove the feature cannot be configured from an upstream boundary.

## Adoption order and verification

1. Implement the standards-only Skill directory and MCP adapters first, with fixture servers and no optional third-party runtime.
2. Add Docker gateway and ContextForge as mutually exclusive integration test profiles; test discovery, schema drift, denied call, timeout, and trace propagation.
3. Add `mcpc` and CAO only as optional operator adapters with no automatic installation.
4. Pilot lazy-mcp and Datalayer compatibility against static fixtures; keep them optional until versioned compatibility tests demonstrate stable behavior.

Every enabled upstream must be version/digest pinned, recorded in the manifest and audit event, SBOM-scanned, license-reviewed, and exercised with a denial-path test. Upgrade only after contract tests pass.

## Primary sources

- [Microsoft Agent Framework skills design](https://github.com/microsoft/agent-framework/blob/main/docs/decisions/0021-agent-skills-design.md) and [repository](https://github.com/microsoft/agent-framework)
- [IBM ContextForge repository](https://github.com/IBM/mcp-context-forge) and [documentation](https://ibm.github.io/mcp-context-forge/)
- [Docker MCP Gateway repository](https://github.com/docker/mcp-gateway) and [gateway reference](https://github.com/docker/mcp-gateway/blob/main/docs/mcp-gateway.md)
- [Apify mcpc repository](https://github.com/apify/mcp-cli) and [mcpc skill/reference](https://github.com/apify/mcp-cli/blob/main/skills/mcpc/SKILL.md)
- [lazy-mcp repository](https://github.com/voicetreelab/lazy-mcp)
- [Datalayer agent-skills repository](https://github.com/datalayer/agent-skills)
- [AWS Labs CAO repository](https://github.com/awslabs/cli-agent-orchestrator) and [control-plane documentation](https://awslabs.github.io/cli-agent-orchestrator/)
