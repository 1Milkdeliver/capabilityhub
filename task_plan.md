# CapabilityHub Development Plan

## Goal

Build and validate an open-source capability control plane that discovers, routes, lazily loads, executes, audits, and budgets Skills, MCP tools, CLIs, APIs, and RAG sources while minimizing context-token overhead.

## Current Phase

Phase 9 — live local inventory hardening.

## Next Step

Publish and validate atomic same-process inventory refresh, safe diagnostics, stable Skill identities, and explicit project-root discovery.

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

Status: complete (pre-alpha local surface)

- Expose minimal meta-tools: search, inspect/load, and execute.
- Provide local validation, discovery, dashboard, and MCP serving commands.
- Add integration tests.

### Phase 6 — Validation and token benchmark

Status: complete for the deterministic release gate

- Compare eager versus lazy capability exposure.
- Verify coverage across all five capability classes.
- Test conflicts, permissions, failures, cache invalidation, and context eviction.

### Phase 7 — Visual UI and Codex plugin

Status: complete (repository plugin; personal installation pending release verification)

- Add a loopback-only management dashboard whose detailed data stays outside chat context.
- Package a minimal Codex plugin entrypoint and repo-local marketplace entry.
- Validate that help/status responses are bounded and do not preload the capability catalog.

### Phase 8 — Open-source release

Status: in_progress

- Complete README, architecture, security, contribution, notices, examples, and CI.
- Initialize repository, commit in small verified batches, create/push GitHub repository when an authenticated route is available.
- Run final full verification.

### Phase 9 — Live local inventory

Status: in_progress

- Refresh the local catalog atomically in the same MCP process only after a lightweight input fingerprint changes.
- Separate query match counts from global active Inventory counts and expose safe generation/diagnostic fields.
- Use stable source namespaces and deterministic project/user/plugin precedence for duplicate Skill names.
- Preserve references, execution grants, budgets, and audit ordering across catalog generations.
- Validate concurrent refresh, stale fallback, add/change/remove behavior, token bounds, and redaction.

## Decisions Made

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
