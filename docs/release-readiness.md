# Release readiness

## Current decision

**Do not label the current tree production-ready.** It is a pre-alpha, local Python
control core with a tested CLI, experimental MCP SDK adapter, and deterministic
structural-disclosure benchmark. It has local SQLite/JSON persistence and supervised
reference adapters and a platform-backed secret broker. Authenticated loopback and optional mTLS
data/admin boundaries, worker CPU/memory limits, and process-tree cleanup now exist. The mTLS profile
is a reference boundary rather than a production service: filesystem/network confinement, a production
provider profile, distributed coordination, and a complete deployment hardening guide remain open.

## Evidence currently available

| Area | Evidence | Release interpretation |
|---|---|---|
| Manifest and registry | JSON plus bounded safe-YAML `v1alpha1` parsing, deterministic export, explicit v1alpha0 alias migration, a published minimum 180-day deprecation policy, feature handshake, immutable revisions, activation pointers, dependency/conflict checks and exact activation locks | Pre-alpha compatibility surface; no signed lock distribution or multi-version production interoperability evidence |
| Search/load/execute core | Permission-filtered lexical search, scope-bound references, selective section/operation loading, bounded notices and signed rehydration handles, JSON Schema validation, durable exact-intent approval queue, scoped idempotency and hierarchical budget admission, and a CAS-versioned principal grant policy | Pre-alpha interfaces; external identity-provider and distributed-policy operation remain open |
| Skill intake | Filesystem-only `SKILL.md` discovery without script execution | Content discovery, not Skill execution or sandboxing |
| Local dashboard | Loopback server with live Inventory/Health, bounded compact search, Routing reasons, Providers, Loaded, approvals, Context and Reasoning state, plus CSRF/same-origin protected safe controls | Local management only; no remote connection probe, user authentication, install/update/delete, or provider execution from the browser |
| CLI, MCP and HTTP | Twenty-nine local commands, three MCP tools, and a strict real-service adapter with authenticated loopback HTTP roundtrips | Experimental local interfaces; `http-serve` is a one-process immutable snapshot, connection state is configuration-only, and no remote multi-user API is shipped |
| Local audit | Synchronized, flushed project JSONL events plus bounded redacted CLI/Dashboard tail; malformed partial records are ignored | Compatibility fallback only; opt into the secure ledger for tamper evidence, while multi-process sequencing remains open |
| Durable idempotency | SQLite atomic key reservation, argument-digest conflict checks, explicit startup recovery to uncertain state, default no-result persistence, and opt-in replay result storage | No encrypted result store, TTL/cleanup policy, distributed database, or recovery UI |
| Durable budget | SQLite atomic reserve/reconcile/cancel, cross-process hard limits, restart-safe used/reserved state, real project `budget-report`, and HTTP/CLI tenant/principal/session/task accounting with atomic ancestor admission | Distributed/global accounting remains open |
| Staged updates | SQLite stage/health/activate/rollback CAS pointers, previous revision retention and in-flight pins; stage/health/activate each re-acquire and trust-verify artifact bytes before mutation | Health evidence is operator supplied; no automatic download, public-key distribution profile, or provider process draining |
| Secure audit | Optional environment-keyed HMAC chain with checkpoint verification, atomic segment rotation, bounded retention and verified export | Local opt-in only; no OS-backed key broker, remote log sink, or multi-process sequence allocator |
| Parameter authorization | Authenticated HTTP and explicit local CLI identities resolve immutable CAS-versioned Provider grants; search/execute share dependency-aware eligibility and normalized path/host/method/command/profile/secret-alias constraints | External identity-provider synchronization and distributed policy administration remain open |
| Supply-chain trust | Every forward staged transition re-acquires bytes; policies support explicit local HMAC evidence or optional Ed25519 keys with publisher/registry, issuer/subject, expiry, revocation and transparency metadata | Ed25519 uses the optional `cryptography` extra; no X.509 chain validation, online Rekor proof verification, key-distribution service, or signed release publication |
| Client protocol | One versioned request/response/error envelope, correlation ID, feature negotiation and stream/cancel conformance fixture; library/HTTP and the exact three MCP tools share the strict service adapter | Broad management CLI still calls runtime functions directly; no remote multi-user service |
| Secret broker | Scope/expiry/use-bound handles and alias-only worker envelopes resolve through Windows DPAPI, macOS Keychain, or Linux Secret Service | Local platform stores only; no remote vault or centrally managed rotation service |
| Provider resilience | Shared runtime execution applies typed retry gates, real-adapter failure certainty, deadline-aware backoff and bounded circuit breaking | Distributed breaker state and external SaaS fault matrices remain open |
| Conflict projections | Inert driver metadata produces hashed identity/name/route/port/root/permission claims and registry admission applies deterministic deny/namespace/isolate/select-one resolution | Local registry admission only; no distributed port/route allocator |
| Scoped tenant state | Trusted identities partition grants, approvals, idempotency, audit, budgets, reasoning, RAG and context residency by opaque tenant/principal/session/task digests; concurrent cross-scope probes are tested | Raw scope is absent from persisted keys; distributed-database operation remains outside the reference profile |
| Degraded decisions | Query-time policy revision and Provider circuit observations feed the TTL-aware allow/degraded/deny matrix; malformed or missing evidence fails closed and explicit operation-specific fallbacks are required | External dependency certification and distributed health aggregation remain deployment responsibilities |
| Data/admin planes | Loopback and optional TLS 1.2+ mutual-TLS transports keep search/load/execute separate from role-scoped lifecycle/update/approval/policy/audit operations; certificate identity drives the per-request tenant context, data/admin credentials are not interchangeable, and CLI/Dashboard management uses the same authenticated dispatcher | Reference deployment only; distributed coordination and production hardening remain open |
| Lifecycle draining | Concurrent admission pins, drain deadlines, forced-retire policy, live generation swaps and supervised worker cancellation preserve old revisions in flight | Local runtime only; distributed coordinators remain open |
| Scale evidence | Fixed-seed 10k metadata/100-read CI plus a million-chunk replay using the production on-disk RAG index with cold/warm/concurrent latency | ACL correctness is tested separately; no model-quality or external-provider claim |
| Five-kind Provider matrix | One table drives real Skill/CLI/loopback HTTP/RAG/MCP discovery, service loading, supported execution/retrieval, explicit unsupported paths, deny/failure/revision/budget and secret-canary assertions | Local deterministic providers and subprocesses only; not external SaaS/gateway adversarial evidence |
| Provider supervision | Local configured execution uses a spawned worker, wall-clock and CPU/memory limits, bounded JSON IPC, Job Object/POSIX process-tree cleanup, and typed failures | No filesystem/network confinement backend or long-lived worker pool |
| Local preferences/lifecycle | Atomic locale and activation overrides, validated install, stage/health/activate/rollback, drain and durable pins | Local catalog/runtime only; no distributed rollout coordinator |
| CLI process adapter | Absolute executable, fixed argv, shell-free provider with alias-only environment, deadline, output budgets, redacted failures, resource limits and tree cancellation | Filesystem/network confinement remains unavailable |
| HTTP API adapter | Opt-in fixed-origin JSON provider plus offline allowlisted OpenAPI 3 projection, HTTPS/loopback policy, redirect denial, encoded path/query arguments, environment-backed headers, bounded reads, project-manifest wiring, and service admission tests | OpenAPI import emits an inert preview only; no OAuth lifecycle, streaming, or automatic activation |
| Privacy observability | The shared real-service adapter can emit bounded hashed-correlation spans, low-cardinality metrics, deterministic sampling, and SQLite aggregate retention/export while rejecting raw arguments, output, URLs, paths, secrets, and identities | Opt-in; not enabled on every runtime path and not an external telemetry backend |
| Local RAG adapter | Persistent FTS index with tenant partitioning, principal ACLs, filters, digest dedupe, citations, retention/freshness, bounded output and ACL-rechecked expansion | No embedding/vector backend or managed remote RAG service |
| MCP stdio adapter | Optional official-SDK client with fixed absolute command/args/environment, initialization and advertised-tool validation, whole-session deadline, JSON/budget checks, safe failures, and service admission test | One process/session per call; no OAuth, HTTP pooling, persistent sessions, streaming passthrough, or production gateway profile |
| Explicit connection probes | Default remains configuration-only; `connections --probe` performs bounded DNS/TCP/TLS setup for configured MCP HTTP(S), with SSRF/mixed-DNS denial and redacted results | Reachability/TLS only; application authentication and health remain unknown, stdio and other provider kinds are unsupported |
| Benchmark | Pinned 100-definition, five-kind fixture run in `benchmarks/reference-run.json` | Structural exposure evidence only; not model-quality evidence |
| Packaging | Linux 3.11-3.13 and Windows 3.12 CI build a wheel, run the full local gate, and smoke-test the base install without optional MCP dependencies | Guards package contents, lazy imports and two OS families; not a signed native installer test |
| Production reference gate | Deterministic profile digest requires split mTLS planes, bounded dependency freshness, fail-closed unknown state and worker isolation; a real-service adversarial gate checks tampered/cross-principal references, output bounds and policy disconnect | Credential-free reference evidence only; not external-provider or production-environment certification |

## Benchmark claim boundary

The reference run routes ten natural-language fixture tasks through the actual deterministic lexical search, then scores the selected revisions. It also pins five failure results and 40 cold/warm/invalidation cache events. It cannot establish whether a language model would choose the same capability or whether production providers preserve the same quality and latency.

Describe 10/10 only as deterministic lexical selection accuracy on the pinned fixture. Do not describe it as model tool-selection quality, reasoning-token reduction, provider latency reduction, production cost reduction, or successful production-provider execution. The portable estimator (`utf8-bytes-div-4-v1`) is a reproducible context-size proxy, not any model provider's billable tokenizer.

## Required before a beta or production claim

- Stabilize and version the CLI/MCP contracts beyond the current pre-alpha adapter.
- Route remaining management surfaces through authenticated principals, add distributed durable state and a documented deployment profile.
- Add network/filesystem confinement backends and explicit incident/recovery procedures beyond the current CPU/memory/process-tree boundary.
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
