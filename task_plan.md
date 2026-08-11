# CapabilityHub Development Plan

## Goal

Build and validate an open-source capability control plane that discovers, routes, lazily loads, executes, audits, and budgets Skills, MCP tools, CLIs, APIs, and RAG sources while minimizing context-token overhead.

## Current Phase

Phase 1 — research, 25+ design-review rounds, architecture, and measurable acceptance criteria.

## Next Step

Inspect available runtimes and upstream projects, then write the design-review record and architecture specification.

## Phases

### Phase 1 — Research and design review

Status: in_progress

- Inspect relevant upstream implementations and licenses.
- Complete at least 25 explicit internal design-review questions.
- Define architecture, threat model, extension contracts, and anti-duplication rules.
- Define token-saving baselines and acceptance thresholds.

### Phase 2 — Core registry and manifest

Status: pending

- Implement normalized capability schema.
- Implement discovery, indexing, dependency/conflict validation, and project profiles.
- Add unit tests.

### Phase 3 — Router and budget controller

Status: pending

- Implement lexical/semantic-ready search and staged disclosure.
- Implement token estimation, budgets, admission control, caching, eviction, and audit events.
- Add deterministic benchmark fixtures.

### Phase 4 — Providers and execution

Status: pending

- Implement Skill, MCP, CLI, API, and RAG provider contracts.
- Implement safe local reference providers without duplicating upstream gateways.
- Add adapter configuration for existing open-source projects.

### Phase 5 — MCP server and CLI

Status: pending

- Expose minimal meta-tools: search, inspect/load, and execute.
- Provide management and audit commands.
- Add integration tests.

### Phase 6 — Validation and token benchmark

Status: pending

- Compare eager versus lazy capability exposure.
- Verify coverage across all five capability classes.
- Test conflicts, permissions, failures, cache invalidation, and context eviction.

### Phase 7 — Open-source release

Status: pending

- Complete README, architecture, security, contribution, notices, examples, and CI.
- Initialize repository, commit in small verified batches, create/push GitHub repository when an authenticated route is available.
- Run final full verification.

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
