# Release readiness

## Current decision

**Do not label the current tree production-ready.** It is a pre-alpha, local Python
control core with a tested CLI, experimental MCP SDK adapter, and deterministic
structural-disclosure benchmark. It has local SQLite/JSON persistence and supervised
reference adapters and an in-memory secret-handle broker. Authenticated loopback data/admin boundaries now
exist, but there is no remote tenant deployment, OS sandbox, production provider profile, or deployment
hardening guide.

## Evidence currently available

| Area | Evidence | Release interpretation |
|---|---|---|
| Manifest and registry | JSON plus bounded safe-YAML `v1alpha1` parsing, deterministic export, explicit v1alpha0 alias migration, feature handshake, immutable revisions, activation pointers, dependency/conflict checks and exact activation locks | Local compatibility surface only; no stable deprecation window or signed lock distribution |
| Search/load/execute core | Permission-filtered lexical search, scope-bound references, selective section/operation loading, bounded notices and signed rehydration handles, JSON Schema validation, durable exact-intent approval queue, SQLite idempotency and budget admission, policy/audit tests | Pre-alpha library/CLI/MCP behavior; execution grants remain process-scoped |
| Skill intake | Filesystem-only `SKILL.md` discovery without script execution | Content discovery, not Skill execution or sandboxing |
| Local dashboard | Loopback server with live Inventory/Health, bounded compact search, Routing reasons, Providers, Loaded, approvals, Context and Reasoning state, plus CSRF/same-origin protected safe controls | Local management only; no remote connection probe, user authentication, install/update/delete, or provider execution from the browser |
| CLI, MCP and HTTP | Twenty-nine local commands, three MCP tools, and a strict real-service adapter with authenticated loopback HTTP roundtrips | Experimental local interfaces; `http-serve` is a one-process immutable snapshot, connection state is configuration-only, and no remote multi-user API is shipped |
| Local audit | Synchronized, flushed project JSONL events plus bounded redacted CLI/Dashboard tail; malformed partial records are ignored | Compatibility fallback only; opt into the secure ledger for tamper evidence, while multi-process sequencing remains open |
| Durable idempotency | SQLite atomic key reservation, argument-digest conflict checks, explicit startup recovery to uncertain state, default no-result persistence, and opt-in replay result storage | No encrypted result store, TTL/cleanup policy, distributed database, or recovery UI |
| Durable budget | SQLite atomic reserve/reconcile/cancel, cross-process hard limits, restart-safe used/reserved state, real project `budget-report`, and loopback HTTP tenant/principal/session/task accounting with atomic ancestor admission | CLI remains one scope; distributed/global accounting remains open |
| Staged updates | SQLite stage/health/activate/rollback CAS pointers, previous revision retention and in-flight pins; stage/health/activate each re-acquire and trust-verify artifact bytes before mutation | Health evidence is operator supplied; no automatic download, public-key distribution profile, or provider process draining |
| Secure audit | Optional environment-keyed HMAC chain with checkpoint verification, atomic segment rotation, bounded retention and verified export | Local opt-in only; no OS-backed key broker, remote log sink, or multi-process sequence allocator |
| Parameter authorization | Optional service context shares dependency-aware search/execute eligibility and constrains normalized paths, hosts, methods, commands, profiles, and secret aliases | Embedders must still supply reviewed caller grants; configured providers do not yet derive them automatically |
| Supply-chain trust | Every forward staged transition re-acquires bytes; policies support explicit local HMAC evidence or optional Ed25519 keys with publisher/registry, issuer/subject, expiry, revocation and transparency metadata | Ed25519 uses the optional `cryptography` extra; no X.509 chain validation, online Rekor proof verification, key-distribution service, or signed release publication |
| Client protocol | One versioned request/response/error envelope, correlation ID, feature negotiation and stream/cancel conformance fixture; library/HTTP and the exact three MCP tools share the strict service adapter | Broad management CLI still calls runtime functions directly; no remote multi-user service |
| Secret broker | In-memory scope/expiry/use-bound opaque handles resolve environment aliases only inside trusted callbacks with digest-only audit | Embedding API only; not wired into every provider and not backed by an OS keychain or remote vault |
| Provider resilience | Optional service executor applies typed retry gates, explicit failure certainty, deadline-aware backoff and bounded circuit breaking | Retry classification is embedder supplied; production adapter fault matrices and distributed breaker state remain open |
| Conflict projections | Inert driver metadata produces hashed identity/name/route/port/root/permission claims and registry admission applies deterministic deny/namespace/isolate/select-one resolution | Local registry admission only; no distributed port/route allocator |
| Scoped tenant state | Trusted HTTP identity partitions approvals, idempotency and audit query state by opaque tenant/principal/session/task digests | Raw scope is absent from keys, but not every business repository or local management surface uses the authenticated plane |
| Degraded decisions | TTL-aware dependency observations produce deterministic allow/degraded/deny outcomes; explicit operation-specific fallbacks are required | Standalone matrix; no live external dependency observer or production fallback certification |
| Loopback HTTP planes | Data HTTP exposes exactly search/load/execute. A separate `/admin` plane uses distinct role-scoped, expiring, single-use credentials for lifecycle/update/approval/policy/audit operations; credentials are not interchangeable | Loopback-only; Dashboard/CLI are not all routed through admin credentials, and no remote TLS/multi-user deployment profile exists |
| Lifecycle draining | Concurrent admission pins, drain deadlines, declared cancellation requests and explicit forced-retire policy preserve old revisions in flight | Standalone coordinator; not yet wired into every service execution or provider cancellation transport |
| Scale evidence | Fixed-seed 10k metadata/100-read CI plus a separate on-disk million-chunk FTS replay artifact with cold/warm/concurrent latency | The million-chunk index is a benchmark provider without ACLs and is not wired to production RAG; no model-quality or production-provider claim |
| Five-kind Provider matrix | One table drives real Skill/CLI/loopback HTTP/RAG/MCP discovery, service loading, supported execution/retrieval, explicit unsupported paths, deny/failure/revision/budget and secret-canary assertions | Local deterministic providers and subprocesses only; not external SaaS/gateway adversarial evidence |
| Provider supervision | Local configured execution uses a spawned worker, wall-clock termination, bounded JSON result envelopes, and safe crash/timeout/protocol errors | No OS CPU/memory/filesystem sandbox or long-lived worker pool |
| Local preferences/lifecycle | Atomic project/global locale and enabled/disabled/quarantined overrides with project precedence | Local catalog activation only; no install, update, rollback, process draining, or durable execution ledger |
| CLI process adapter | Opt-in absolute-executable, fixed-argv, shell-free provider with explicit environment, deadline, output parsing, redacted failures, project-manifest wiring, and full service admission test | No sandbox, OS resource limits, or durable cancellation |
| HTTP API adapter | Opt-in fixed-origin JSON provider plus offline allowlisted OpenAPI 3 projection, HTTPS/loopback policy, redirect denial, encoded path/query arguments, environment-backed headers, bounded reads, project-manifest wiring, and service admission tests | OpenAPI import emits an inert preview only; no OAuth lifecycle, streaming, or automatic activation |
| Privacy observability | The shared real-service adapter can emit bounded hashed-correlation spans, low-cardinality metrics, deterministic sampling, and SQLite aggregate retention/export while rejecting raw arguments, output, URLs, paths, secrets, and identities | Opt-in; not enabled on every runtime path and not an external telemetry backend |
| Local RAG adapter | Opt-in bounded `.md`/`.txt` retrieval with containment checks, deterministic chunk ranking, relative line citations, deadline/output limits, and service admission test | Small read-only reference adapter; no vector index, ACL backend, embedding model, persistence, or million-chunk scale evidence |
| MCP stdio adapter | Optional official-SDK client with fixed absolute command/args/environment, initialization and advertised-tool validation, whole-session deadline, JSON/budget checks, safe failures, and service admission test | One process/session per call; no OAuth, HTTP pooling, persistent sessions, streaming passthrough, or production gateway profile |
| Explicit connection probes | Default remains configuration-only; `connections --probe` performs bounded DNS/TCP/TLS setup for configured MCP HTTP(S), with SSRF/mixed-DNS denial and redacted results | Reachability/TLS only; application authentication and health remain unknown, stdio and other provider kinds are unsupported |
| Benchmark | Pinned 100-definition, five-kind fixture run in `benchmarks/reference-run.json` | Structural exposure evidence only; not model-quality evidence |
| Packaging | Linux 3.11-3.13 and Windows 3.12 CI build a wheel, run the full local gate, and smoke-test the base install without optional MCP dependencies | Guards package contents, lazy imports and two OS families; not a signed native installer test |

## Benchmark claim boundary

The reference run routes ten natural-language fixture tasks through the actual deterministic lexical search, then scores the selected revisions. It also pins five failure results and 40 cold/warm/invalidation cache events. It cannot establish whether a language model would choose the same capability or whether production providers preserve the same quality and latency.

Describe 10/10 only as deterministic lexical selection accuracy on the pinned fixture. Do not describe it as model tool-selection quality, reasoning-token reduction, provider latency reduction, production cost reduction, or successful production-provider execution. The portable estimator (`utf8-bytes-div-4-v1`) is a reproducible context-size proxy, not any model provider's billable tokenizer.

## Required before a beta or production claim

- Stabilize and version the CLI/MCP contracts beyond the current pre-alpha adapter.
- Route remaining management surfaces through authenticated principals, add distributed durable state and a documented deployment profile.
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
