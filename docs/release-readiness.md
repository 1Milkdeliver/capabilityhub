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
| Search/load/execute core | Permission-filtered lexical search, scope-bound references, section/operation loading, JSON Schema validation, exact-intent approvals, in-process replay plus optional SQLite idempotency admission, policy/budget/audit tests | Pre-alpha library/CLI/MCP behavior; approval references and execution grants remain process-scoped |
| Skill intake | Filesystem-only `SKILL.md` discovery without script execution | Content discovery, not Skill execution or sandboxing |
| Local dashboard | Loopback server with live Inventory/Health, bounded compact search, CSRF/same-origin protected project locale and enable/disable/quarantine controls | Local management only; no remote connection probe, user authentication, install/update/delete, approval, or provider execution |
| CLI and MCP | Fifteen local commands and three MCP tools tested with the official SDK's in-memory client | Experimental local interface; execute supports explicit project drivers plus a test-only static fixture, connection state is configuration-only, and catalog generations refresh atomically after a lightweight change check |
| Local audit | Synchronized, flushed project JSONL events plus bounded redacted CLI/Dashboard tail; malformed partial records are ignored | No tamper-evident chain, rotation/retention policy, external export, or multi-process sequence allocator |
| Durable idempotency | SQLite atomic key reservation, argument-digest conflict checks, crash-to-uncertain recovery, default no-result persistence, and opt-in replay result storage | No encrypted result store, TTL/cleanup policy, distributed database, or recovery UI |
| Local preferences/lifecycle | Atomic project/global locale and enabled/disabled/quarantined overrides with project precedence | Local catalog activation only; no install, update, rollback, process draining, or durable execution ledger |
| CLI process adapter | Opt-in absolute-executable, fixed-argv, shell-free provider with explicit environment, deadline, output parsing, redacted failures, project-manifest wiring, and full service admission test | No sandbox, OS resource limits, or durable cancellation |
| HTTP API adapter | Opt-in fixed-origin JSON provider with HTTPS/loopback policy, redirect denial, encoded path/query arguments, environment-backed headers, bounded reads, project-manifest wiring, and service admission test | No OAuth lifecycle, retries, streaming, circuit breaker, or automatic OpenAPI import |
| Local RAG adapter | Opt-in bounded `.md`/`.txt` retrieval with containment checks, deterministic chunk ranking, relative line citations, deadline/output limits, and service admission test | Small read-only reference adapter; no vector index, ACL backend, embedding model, persistence, or million-chunk scale evidence |
| MCP stdio adapter | Optional official-SDK client with fixed absolute command/args/environment, initialization and advertised-tool validation, whole-session deadline, JSON/budget checks, safe failures, and service admission test | One process/session per call; no OAuth, HTTP pooling, persistent sessions, streaming passthrough, or production gateway profile |
| Benchmark | Pinned 100-definition, five-kind fixture run in `benchmarks/reference-run.json` | Structural exposure evidence only; not model-quality evidence |
| Packaging | CI builds a wheel, installs it without optional MCP dependencies in a clean environment, and runs Health, Budget, and Benchmark smoke checks | Guards base-package contents and lazy optional imports; not a cross-platform installer test |

## Benchmark claim boundary

The reference run uses oracle-supplied expected target revisions. The harness can therefore compare equal eager definitions against a fixed lazy meta-tool payload plus the selected search card and selected definition, and can verify that every fixture provider kind is represented. It cannot establish whether a model would choose the correct capability from natural-language intent.

Do not describe the result as tool-selection accuracy, semantic routing accuracy, reasoning-token reduction, provider latency reduction, production cost reduction, or successful provider execution. The portable estimator (`utf8-bytes-div-4-v1`) is a reproducible context-size proxy, not any model provider's billable tokenizer.

## Required before a beta or production claim

- Stabilize and version the CLI/MCP contracts beyond the current pre-alpha adapter.
- Add authenticated tenant/principal handling, durable state, lifecycle/rollback, and a documented deployment profile.
- Add production-grade provider isolation, secret handling, network/process policy enforcement, and explicit incident/recovery procedures.
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
