# Product scope and delivery slices

This document converts the 35 discovery rounds into an implementable release boundary. The architecture describes the long-term system; this scope prevents the first release from recreating gateways, sandboxes, vector databases, or agent frameworks.

## What “control” means

CapSift controls a capability only when the client routes discovery and execution through the Hub. Control consists of:

1. normalized, revisioned metadata;
2. visibility filtering and ranked discovery;
3. bounded progressive disclosure;
4. dependency/conflict validation;
5. policy and budget admission;
6. provider dispatch through an adapter;
7. structured audit events and measured context cost.

CapSift cannot govern a tool that a client also exposes through an independent bypass. Deployments that require enforcement MUST remove direct client access or enforce the same policy at the upstream gateway.

## Release 0.1 — evidence-producing core

Release 0.1 MUST deliver:

- one versioned manifest and immutable capability revision model;
- registry operations for all five kinds: Skill, MCP, CLI, API, and RAG;
- filesystem Skill discovery without executing Skill scripts;
- declarative/static adapters for MCP, CLI, API, and RAG fixtures;
- dependency and conflict validation;
- compact catalog search with hard result budgets;
- exactly three model-facing operations: `capability.search`, `capability.load`, and `capability.execute`;
- revision-bound references that reject tampering and stale revisions;
- task budget accounting for bytes, portable tokens, loads, executions, retries, and reasoning tier;
- deterministic reasoning-tier recommendations (`low`, `medium`, `high`) with escalation caps and reason codes;
- deny-by-default execution plus side-effect/approval-required decisions;
- structured errors and JSONL audit events;
- deterministic eager-versus-lazy benchmark fixtures covering all five kinds;
- CLI commands for discovery, validation, search, load, execute, budget report, and benchmark;
- an MCP server adapter if it can use the official SDK without reimplementing MCP transport.

Release 0.1 MUST prove the initial-context and total-input savings gates defined in `validation-plan.md` using deterministic fixtures. It MUST NOT claim savings for live models until a versioned live-model report is published.

## Release 0.2 — real upstream adapters

- official MCP client/server SDK integration;
- ContextForge and Docker MCP Gateway profiles;
- `mcpc` operator adapter;
- OpenAPI operation importer;
- pluggable RAG retrieval adapter with citations and ACL hooks;
- approval-token integration for clients that support approval UI;
- SQLite persistence and cache invalidation.

## Later releases

- shared PostgreSQL deployment;
- distributed workers and durable lifecycle reconciliation;
- container/sandbox integrations;
- semantic/vector catalog search;
- managed RAG indexing pipelines;
- telemetry-trained model-tier prediction after shadow evaluation;
- web administration UI and marketplace workflows.

## Explicit non-goals

CapSift will not implement:

- an MCP transport stack, OAuth client, credential vault, or API gateway;
- a container runtime, code sandbox, shell, terminal multiplexer, or agent scheduler;
- a general vector database or document-processing platform;
- proprietary hidden-chain-of-thought inspection;
- automatic installation or execution of untrusted Skill scripts;
- policy enforcement over capabilities that bypass the Hub;
- a clone of ContextForge, Docker MCP Gateway, mcpc, Agent Framework, or CAO.

## Reasoning-strength policy

Reasoning strength is a budgeted recommendation, not a universal model switch:

| Tier | Default work | Escalation trigger |
|---|---|---|
| `low` | exact lookup, parsing, formatting, deterministic validation | ambiguity, conflict, or failed invariant |
| `medium` | normal routing, multi-step execution, routine implementation | low selection margin, repeated typed failure, elevated side-effect risk |
| `high` | architecture, security review, benchmark interpretation, irreversible/high-risk action | no higher automatic tier; stop or request human input when capped |

The router MUST select the cheapest eligible tier, enforce policy minimums for risk, record reason codes, and stop equivalent retries without new evidence. It MUST NOT estimate hidden reasoning tokens when the provider does not report them.

## Development gates

1. No production module is added without a requirement or acceptance-test link.
2. No upstream feature is copied when an adapter boundary exists.
3. A failed test is assigned one owner and one root-cause record; no parallel duplicate bug fixes.
4. Every provider kind passes the same registry/search/load/policy/audit conformance matrix.
5. Token-saving claims require raw benchmark artifacts and an eager baseline over identical capability definitions.

