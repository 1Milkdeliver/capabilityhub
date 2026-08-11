# Release readiness

## Current decision

**Do not label the current tree production-ready.** It is a pre-alpha, local Python
control core with a tested CLI, experimental MCP SDK adapter, and deterministic
structural-disclosure benchmark. It has local SQLite/JSON persistence and supervised
reference adapters, but no secret broker, OS sandbox, authenticated tenant boundary,
production provider profile, or deployment hardening guide.

## Evidence currently available

| Area | Evidence | Release interpretation |
|---|---|---|
| Manifest and registry | JSON plus bounded safe-YAML `v1alpha1` parsing, deterministic export, explicit v1alpha0 alias migration, feature handshake, immutable revisions, activation pointers, dependency/conflict checks and exact activation locks | Local compatibility surface only; no stable deprecation window or signed lock distribution |
| Search/load/execute core | Permission-filtered lexical search, scope-bound references, section/operation loading, JSON Schema validation, durable exact-intent approval queue, SQLite idempotency and budget admission, policy/audit tests | Pre-alpha library/CLI/MCP behavior; execution grants remain process-scoped |
| Skill intake | Filesystem-only `SKILL.md` discovery without script execution | Content discovery, not Skill execution or sandboxing |
| Local dashboard | Loopback server with live Inventory/Health, bounded compact search, Routing reasons, Providers, Loaded, approvals, Context and Reasoning state, plus CSRF/same-origin protected safe controls | Local management only; no remote connection probe, user authentication, install/update/delete, or provider execution from the browser |
| CLI and MCP | Twenty-seven local commands and three MCP tools tested with the official SDK's in-memory client | Experimental local interface; execute supports explicit project drivers plus a test-only static fixture, connection state is configuration-only, and catalog generations refresh atomically after a lightweight change check |
| Local audit | Synchronized, flushed project JSONL events plus bounded redacted CLI/Dashboard tail; malformed partial records are ignored | Compatibility fallback only; opt into the secure ledger for tamper evidence, while multi-process sequencing remains open |
| Durable idempotency | SQLite atomic key reservation, argument-digest conflict checks, explicit startup recovery to uncertain state, default no-result persistence, and opt-in replay result storage | No encrypted result store, TTL/cleanup policy, distributed database, or recovery UI |
| Durable budget | SQLite atomic reserve/reconcile/cancel, cross-process hard limits, restart-safe used/reserved state, and real project `budget-report` | One local runtime scope; durable hierarchical child scopes and distributed accounting remain open |
| Staged updates | SQLite stage/health/activate/rollback CAS pointers, previous revision retention and in-flight pins wired into live catalog generations | Health evidence is operator supplied; no artifact fetch/signature verification or process draining |
| Secure audit | Optional environment-keyed HMAC chain with checkpoint verification, atomic segment rotation, bounded retention and verified export | Local opt-in only; no OS-backed key broker, remote log sink, or multi-process sequence allocator |
| Parameter authorization | Optional service context shares dependency-aware search/execute eligibility and constrains normalized paths, hosts, methods, commands, profiles, and secret aliases | Embedders must still supply reviewed caller grants; configured providers do not yet derive them automatically |
| Supply-chain trust | Manifest digest verification plus publisher/registry allowlists, expiry, revocation and a local HMAC attestation profile | Shared-key local evidence only; no public-key/Sigstore identity or staged artifact acquisition gate |
| Client protocol | One versioned request/response/error envelope, correlation ID, feature negotiation and stream/cancel conformance fixture for library/CLI/MCP/HTTP boundaries | Contract core only; no remote HTTP server or real four-transport conformance run |
| Secret broker | In-memory scope/expiry/use-bound opaque handles resolve environment aliases only inside trusted callbacks with digest-only audit | Embedding API only; not wired into every provider and not backed by an OS keychain or remote vault |
| Provider resilience | Optional service executor applies typed retry gates, explicit failure certainty, deadline-aware backoff and bounded circuit breaking | Retry classification is embedder supplied; production adapter fault matrices and distributed breaker state remain open |
| Conflict projections | Inert driver metadata produces hashed identity/name/route/port/root/permission claims with deterministic deny/namespace/isolate/select-one resolution | Standalone policy core; not yet an automatic registry admission gate |
| Scoped tenant state | SQLite generic KV/cache/events are partitioned by HMAC scope/key digests and bounded per-scope TTL cleanup | Raw identities are absent from this store, but existing business repositories are not all migrated and there is no authenticated principal source |
| Degraded decisions | TTL-aware dependency observations produce deterministic allow/degraded/deny outcomes; explicit operation-specific fallbacks are required | Standalone matrix; no live external dependency observer or production fallback certification |
| Loopback HTTP control | Real bounded JSON roundtrips use the shared protocol envelope, exact three operations, numeric loopback binding, bearer-token digest, Host/peer/Origin checks and safe errors | Embeddable local adapter only; no remote TLS listener, authenticated tenant identity, or production deployment profile |
| Lifecycle draining | Concurrent admission pins, drain deadlines, declared cancellation requests and explicit forced-retire policy preserve old revisions in flight | Standalone coordinator; not yet wired into every service execution or provider cancellation transport |
| Scale evidence | Fixed-seed 10k metadata catalog, top-8 quality fixtures, cold/warm paths and 100 concurrent reads report p50/p95/max plus replay environment in CI | Synthetic in-process metadata only; no 1m RAG, model-quality, or production-provider claim |
| Provider supervision | Local configured execution uses a spawned worker, wall-clock termination, bounded JSON result envelopes, and safe crash/timeout/protocol errors | No OS CPU/memory/filesystem sandbox or long-lived worker pool |
| Local preferences/lifecycle | Atomic project/global locale and enabled/disabled/quarantined overrides with project precedence | Local catalog activation only; no install, update, rollback, process draining, or durable execution ledger |
| CLI process adapter | Opt-in absolute-executable, fixed-argv, shell-free provider with explicit environment, deadline, output parsing, redacted failures, project-manifest wiring, and full service admission test | No sandbox, OS resource limits, or durable cancellation |
| HTTP API adapter | Opt-in fixed-origin JSON provider with HTTPS/loopback policy, redirect denial, encoded path/query arguments, environment-backed headers, bounded reads, project-manifest wiring, and service admission test | No OAuth lifecycle, retries, streaming, circuit breaker, or automatic OpenAPI import |
| Local RAG adapter | Opt-in bounded `.md`/`.txt` retrieval with containment checks, deterministic chunk ranking, relative line citations, deadline/output limits, and service admission test | Small read-only reference adapter; no vector index, ACL backend, embedding model, persistence, or million-chunk scale evidence |
| MCP stdio adapter | Optional official-SDK client with fixed absolute command/args/environment, initialization and advertised-tool validation, whole-session deadline, JSON/budget checks, safe failures, and service admission test | One process/session per call; no OAuth, HTTP pooling, persistent sessions, streaming passthrough, or production gateway profile |
| Benchmark | Pinned 100-definition, five-kind fixture run in `benchmarks/reference-run.json` | Structural exposure evidence only; not model-quality evidence |
| Packaging | Linux 3.11-3.13 and Windows 3.12 CI build a wheel, run the full local gate, and smoke-test the base install without optional MCP dependencies | Guards package contents, lazy imports and two OS families; not a signed native installer test |

## Benchmark claim boundary

The reference run routes ten natural-language fixture tasks through the actual deterministic lexical search, then scores the selected revisions. It also pins five failure results and 40 cold/warm/invalidation cache events. It cannot establish whether a language model would choose the same capability or whether production providers preserve the same quality and latency.

Describe 10/10 only as deterministic lexical selection accuracy on the pinned fixture. Do not describe it as model tool-selection quality, reasoning-token reduction, provider latency reduction, production cost reduction, or successful production-provider execution. The portable estimator (`utf8-bytes-div-4-v1`) is a reproducible context-size proxy, not any model provider's billable tokenizer.

## Required before a beta or production claim

- Stabilize and version the CLI/MCP contracts beyond the current pre-alpha adapter.
- Add authenticated tenant/principal handling, distributed durable state, process draining around lifecycle/rollback, and a documented deployment profile.
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
