# Release readiness

## Current decision

**Do not label the current tree production-ready.** It is a pre-alpha, local Python
control core with a tested CLI, experimental MCP SDK adapter, and deterministic
structural-disclosure benchmark. It does not yet have a production provider adapter,
secret broker, sandbox, persistence layer, authentication/tenant boundary, or
deployment hardening guide.

## Evidence currently available

| Area | Evidence | Release interpretation |
|---|---|---|
| Manifest and registry | JSON `v1alpha1` parsing, immutable revisions, activation pointers, dependency/conflict checks | Suitable for local/core experimentation; not a compatibility guarantee |
| Search/load/execute core | Permission-filtered lexical search, scope-bound references, section/operation loading, JSON Schema validation, exact-intent approvals, SQLite idempotency and budget admission, policy/audit tests | Pre-alpha library/CLI/MCP behavior; approval references and execution grants remain process-scoped |
| Skill intake | Filesystem-only `SKILL.md` discovery without script execution | Content discovery, not Skill execution or sandboxing |
| Local dashboard | Loopback server with live Inventory/Health, bounded compact search, Routing reasons, Providers, recent Loaded audit entries, CSRF/same-origin protected project locale and enable/disable/quarantine controls | Local management only; no remote connection probe, user authentication, install/update/delete, approval, or provider execution |
| CLI and MCP | Eighteen local commands and three MCP tools tested with the official SDK's in-memory client | Experimental local interface; execute supports explicit project drivers plus a test-only static fixture, connection state is configuration-only, and catalog generations refresh atomically after a lightweight change check |
| Local audit | Synchronized, flushed project JSONL events plus bounded redacted CLI/Dashboard tail; malformed partial records are ignored | No tamper-evident chain, rotation/retention policy, external export, or multi-process sequence allocator |
| Durable idempotency | SQLite atomic key reservation, argument-digest conflict checks, explicit startup recovery to uncertain state, default no-result persistence, and opt-in replay result storage | No encrypted result store, TTL/cleanup policy, distributed database, or recovery UI |
| Durable budget | SQLite atomic reserve/reconcile/cancel, cross-process hard limits, restart-safe used/reserved state, and real project `budget-report` | One local runtime scope; durable hierarchical child scopes and distributed accounting remain open |
| Provider supervision | Local configured execution uses a spawned worker, wall-clock termination, bounded JSON result envelopes, and safe crash/timeout/protocol errors | No OS CPU/memory/filesystem sandbox or long-lived worker pool |
| Local preferences/lifecycle | Atomic project/global locale and enabled/disabled/quarantined overrides with project precedence | Local catalog activation only; no install, update, rollback, process draining, or durable execution ledger |
| CLI process adapter | Opt-in absolute-executable, fixed-argv, shell-free provider with explicit environment, deadline, output parsing, redacted failures, project-manifest wiring, and full service admission test | No sandbox, OS resource limits, or durable cancellation |
| HTTP API adapter | Opt-in fixed-origin JSON provider with HTTPS/loopback policy, redirect denial, encoded path/query arguments, environment-backed headers, bounded reads, project-manifest wiring, and service admission test | No OAuth lifecycle, retries, streaming, circuit breaker, or automatic OpenAPI import |
| Local RAG adapter | Opt-in bounded `.md`/`.txt` retrieval with containment checks, deterministic chunk ranking, relative line citations, deadline/output limits, and service admission test | Small read-only reference adapter; no vector index, ACL backend, embedding model, persistence, or million-chunk scale evidence |
| MCP stdio adapter | Optional official-SDK client with fixed absolute command/args/environment, initialization and advertised-tool validation, whole-session deadline, JSON/budget checks, safe failures, and service admission test | One process/session per call; no OAuth, HTTP pooling, persistent sessions, streaming passthrough, or production gateway profile |
| Benchmark | Pinned 100-definition, five-kind fixture run in `benchmarks/reference-run.json` | Structural exposure evidence only; not model-quality evidence |
| Packaging | CI builds a wheel, installs it without optional MCP dependencies in a clean environment, and runs Health, Budget, and Benchmark smoke checks | Guards base-package contents and lazy optional imports; not a cross-platform installer test |

## Benchmark claim boundary

The reference run routes ten natural-language fixture tasks through the actual deterministic lexical search, then scores the selected revisions. It also pins five failure results and 40 cold/warm/invalidation cache events. It cannot establish whether a language model would choose the same capability or whether production providers preserve the same quality and latency.

Describe 10/10 only as deterministic lexical selection accuracy on the pinned fixture. Do not describe it as model tool-selection quality, reasoning-token reduction, provider latency reduction, production cost reduction, or successful production-provider execution. The portable estimator (`utf8-bytes-div-4-v1`) is a reproducible context-size proxy, not any model provider's billable tokenizer.

## Required before a beta or production claim

- Stabilize and version the CLI/MCP contracts beyond the current pre-alpha adapter.
- Add authenticated tenant/principal handling, durable state, lifecycle/rollback, and a documented deployment profile.
- Add OS resource sandboxing, secret brokering, network/filesystem policy enforcement, and explicit incident/recovery procedures beyond the current spawned-worker boundary.
- Run adversarial, failure, cache-invalidation, authorization, and multi-tenant validation against actual adapters.
- Add model-backed, stratified evaluation for semantic selection accuracy and report model/version, reasoning tier, prompts, seeds, latency, tool calls, tokenizer, and pricing assumptions separately.
- Publish compatible API/versioning, support, privacy/retention, and operational documentation.

## Lightweight release checks

From the repository root after installing development dependencies:

```bash
python -m pytest
python -m ruff check src tests benchmarks
python -m mypy
capabilityhub benchmark
python -m pip wheel --no-deps --wheel-dir dist .
```

The final command only verifies the deterministic fixture gate. It is necessary for a benchmark artifact update, but it is not sufficient for a software release decision.
