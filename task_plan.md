# CapabilityHub Development Plan

## Goal

Build and validate an open-source capability control plane that discovers, routes, lazily loads, executes, audits, and budgets Skills, MCP tools, CLIs, APIs, and RAG sources while minimizing context-token overhead.

## Current Phase

Phase 18 — local HTTP, lifecycle draining, and scale evidence.

## Next Step

Integrate authenticated loopback HTTP, revision draining, and 10k/concurrent evidence without widening the always-loaded context.

## Phases

### Phase 1 — Research and design review

Status: complete

- Inspect relevant upstream implementations and licenses.
- Complete at least 25 explicit internal design-review questions.
- Define architecture, threat model, extension contracts, and anti-duplication rules.
- Define token-saving baselines and acceptance thresholds.

### Phase 2 — Core registry and manifest

Status: complete

- Implement normalized capability schema.
- Implement discovery, indexing, dependency/conflict validation, and project profiles.
- Add unit tests.

### Phase 3 — Router and budget controller

Status: complete

- Implement lexical/semantic-ready search and staged disclosure.
- Implement token estimation, budgets, admission control, caching, eviction, and audit events.
- Add deterministic benchmark fixtures.

### Phase 4 — Providers and execution

Status: complete (reference adapters only; production execution remains out of scope)

- Implement Skill, MCP, CLI, API, and RAG provider contracts.
- Implement safe local reference providers without duplicating upstream gateways.
- Add adapter configuration for existing open-source projects.

### Phase 5 — MCP server and CLI

Status: complete (pre-alpha local surface; twenty-seven commands and three MCP tools)

- Expose minimal meta-tools: search, inspect/load, and execute.
- Provide local validation, discovery, dashboard, and MCP serving commands.
- Add integration tests.

### Phase 6 — Validation and token benchmark

Status: complete for the deterministic release gate

- Compare eager versus lazy capability exposure.
- Verify coverage across all five capability classes.
- Test conflicts, permissions, failures, cache invalidation, and context eviction.

### Phase 7 — Visual UI and Codex plugin

Status: complete (repository plugin release-validated; supported local management actions are connected)

- Add a loopback-only management dashboard whose detailed data stays outside chat context.
- Package a minimal Codex plugin entrypoint and repo-local marketplace entry.
- Validate that help/status responses are bounded and do not preload the capability catalog.

### Phase 8 — Open-source release

Status: complete

- Complete README, architecture, security, contribution, notices, examples, and CI.
- Initialize repository, commit in small verified batches, create/push GitHub repository when an authenticated route is available.
- Run final full verification.

### Phase 9 — Live local inventory

Status: complete

- Refresh the local catalog atomically in the same MCP process only after a lightweight input fingerprint changes.
- Separate query match counts from global active Inventory counts and expose safe generation/diagnostic fields.
- Use stable source namespaces and deterministic project/user/plugin precedence for duplicate Skill names.
- Preserve references, execution grants, budgets, and audit ordering across catalog generations.
- Validate concurrent refresh, stale fallback, add/change/remove behavior, token bounds, and redaction.

## Decisions Made

### Phase 10 — Live local operations

Status: complete

- Add bounded `inventory`, `search`, and non-scanning `health` CLI commands.
- Connect the loopback Dashboard to the shared live Inventory generation.
- Make returned Inventory data isolated, freeze published registries, and reject mixed project scopes.
- Keep search output at or below 900 portable tokens and reject oversized result limits.
- Preserve the exact three-tool MCP surface.

### Phase 11 — Security admission and CLI completeness

Status: complete

- Filter search disclosure by caller permissions.
- Bind approval references to exact actor, task, revision, operation, arguments, and expiry.
- Validate inline JSON Schema contracts at registration and execution boundaries.
- Prevent duplicate or uncertain replay with scoped idempotency records.
- Expose `load`, configured `execute`, persistent `budget-report`, `benchmark`, Loaded, Providers, and Routing commands.
- Validate the built wheel in an isolated environment and publish an honest capability matrix.

### Phase 12 — Durable management and real adapters

Status: complete for the local pre-alpha scope

- Add persistent language and activation lifecycle state. (Locale, enabled/disabled/quarantined overrides, redacted project audit, conservative SQLite idempotency, exact-intent approvals, and budgets are complete.)
- Connect menu and Dashboard actions only where a real backend exists.
- Add allowlisted process-level MCP, CLI, HTTP/API, and RAG adapters without copying upstream implementations. (Reference adapters, project-manifest wiring, and spawned-worker supervision are complete; OS resource confinement remains open.)
- Add semantic, failure, cold/warm cache, browser, and plugin validation evidence. Production-adapter evidence remains an explicit release gate.

### Phase 13 — Durable approvals, Context, and Reasoning

Status: complete for the local pre-alpha scope

- Persist exact-intent single-use approvals without storing arguments.
- Persist metadata-only context residency, pinning, access order, and observable eviction.
- Persist budget-aware reasoning advice and anti-loop decisions without storing prompts.
- Expose bounded CLI and Dashboard views and actions.

### Phase 14 — Staged updates, portability, and secure audit

Status: complete for the local pre-alpha scope

- Stage, health-gate, atomically activate, and roll back immutable revisions.
- Export and migrate JSON manifests and negotiate v1alpha1 optional/required features.
- Add opt-in HMAC-chained audit, verified rotation, bounded retention, and export.
- Verify all additions through the CLI, live catalog, Dashboard assets, and full release gates.

### Phase 15 — Trust, least privilege, and client conformance

Status: complete for the local pre-alpha scope

- Verify artifacts against explicit publisher and registry trust policy.
- Attenuate permissions against normalized arguments and dependency privileges.
- Define one request/error/feature envelope for library, CLI, MCP, and future HTTP clients.
- Connect only proven backends to CLI, Dashboard, and plugin documentation.

### Phase 16 — Secrets, resilience, and portable activation

Status: complete for the local pre-alpha scope

- Add scope-, expiry-, and use-bound secret handles for trusted local callbacks.
- Add typed, certainty-aware retry and bounded circuit-breaker primitives.
- Accept resource-bounded safe YAML through the same inert manifest model.
- Export and verify deterministic exact-revision activation locks.

### Phase 17 — Conflict projection, tenancy, and degraded operation

Status: complete for the standalone local control-plane scope

- Derive privacy-preserving resource claims and deterministic conflict resolutions.
- Partition generic SQLite KV/cache/events by opaque tenant/principal/session/task scope.
- Evaluate operation-specific dependency freshness and explicit safe fallbacks.
- Keep automatic registry/business-store/live-observer wiring visible as remaining work.

### Phase 18 — Local HTTP, lifecycle draining, and scale evidence

Status: complete for the standalone local pre-alpha scope

- Serve the shared request envelope over a bearer-protected numeric loopback endpoint.
- Coordinate accepting, draining, cancellation requests, and retired revision pins.
- Replay 10k metadata and 100 concurrent read evidence with percentile latency.
- Keep remote deployment, service drain wiring, 1m RAG, and production-provider claims open.

| Decision | Reason |
|---|---|
| Use adapters instead of merging upstream source trees | Avoid duplicated maintenance and license/technology conflicts |
| Keep only metadata/search meta-tools always visible | Core mechanism for context savings |
| Measure savings against deterministic eager baselines | Prevent unsubstantiated token-saving claims |
| Route work by reasoning tier: low/medium for mechanical work, high for architecture/security/benchmark decisions | Avoid spending expensive reasoning on routine tasks while preserving rigor at irreversible decision points |

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `git` and `gh` not found on PATH | 1 | Search installed runtimes and configured GitHub connectors; continue local work independently |
| Initial commit lacked Git author identity; `gh` could not find the out-of-PATH Git executable | 1 | Configure repository-local noreply identity and prepend bundled Git only for publish commands |
| Bundled Python did not include pytest | 1 | Create a project-local ignored virtual environment and install the declared development extra once |
| Core tests passed but Ruff reported one long line plus import hygiene issues | 1 | Apply a targeted formatting/import fix in the owning files, then rerun the same validation once |
| Codex plugin validator could not import its PyYAML dependency | 1 | Add PyYAML to development-only extras, install once in the project venv, and rerun the validator |
| CLI integration passed tests but failed initial Ruff/mypy checks | 1 | Returned the issue to the owning implementation stream; after model capacity interrupted it, applied the formatter/type-only fixes once and revalidated |
| MCP SDK v2 could not derive a schema for recursive `JsonValue` | 1 | Return the SDK's typed `CallToolResult` directly while retaining structured content and official transports |
