# Requirement completion matrix

This file is the release truth table for the 36 discovery rounds. `Implemented` means the current
tree contains a real path and relevant verification. `Partial` means useful code exists but the stated
acceptance scope is not yet proven. `Open` means the required subsystem is absent. A green unit-test
run does not upgrade a row by itself.

| ID | Requirement | Current state | Evidence or remaining gate |
|---|---|---|---|
| R1 | Separate control and data planes | Implemented | Model-facing HTTP/MCP expose only search/load/execute; loopback and mTLS admin paths use distinct plane-specific credentials, while CLI and Dashboard management actions share the authenticated admin dispatcher and cannot be invoked with data credentials |
| R2 | End-to-end cost and quality targets | Implemented | A real six-call Codex evaluation records provider-reported input/cache/output/reasoning tokens, tool calls and latency across 30 paired tasks; lazy loading passed quality non-inferiority and used 40.65% of eager estimated cost |
| R3 | One abstraction for Skill/MCP/CLI/API/RAG | Implemented | Manifest, registry and provider protocol cover all five; configured reference adapters execute four and Skill loads instructions |
| R4 | Three meta-tools invoke every kind | Implemented | One MCP-envelope search/load/execute chain covers all five real Provider kinds; Skill is explicitly load-only and CLI/API/RAG/MCP invoke their advertised operation |
| R5 | Compact search under hard budgets | Implemented | Search enforces portable-token, total-byte, and per-card-byte ceilings, skips oversized cards safely, defaults to top-8, and gates the 10k fixture on the correct top-3 result |
| R6 | Progressive selective loading | Implemented | Sections/operations are selectable; bounded dependency/conflict notices, omission counts, and scope/revision/expiry-bound opaque rehydration handles support exact continuation |
| R7 | Revision-bound references | Implemented | Signed scope/purpose/revision/expiry references reject stale and tampered input |
| R8 | Manifest as source of truth | Implemented | JSON and bounded safe YAML remain inert; CLI validation, atomic installation, and live local catalog admission reject malformed permissions, unknown required semantics, and kind-specific Skill/CLI/MCP/API/RAG driver configuration before registry mutation or activation |
| R9 | Stable identity and digest | Implemented | Immutable coordinate/version/digest revisions and activation pointers are enforced |
| R10 | Explainable authorized search | Implemented | Revisioned configurable lexical/structured-alias and metadata fitness weights produce safe reasons; authorization, availability, trust, cost, latency, and caller eligibility filter before scoring |
| R11 | Catalog/RAG index separation | Implemented | The metadata catalog and production on-disk FTS content index are separate; tenant HMAC partitions and principal ACL changes take effect without rebuilding either catalog or FTS content |
| R12 | Bounded RAG results and citations | Implemented | Production retrieval enforces top-k, token/byte/deadline bounds, filters, digest dedupe, relative citations, retention/freshness, and ACL-rechecked opaque expansion handles |
| R13 | Dependency resolution and lock | Implemented | Constraints/cycles are validated and a deterministic exact-revision activation lock with transitive dependency closure detects missing, extra, or drifted active state |
| R14 | Conflict detection and policy | Implemented | Registry admission resolves automatic identity/name/route/port/path/permission claims before activation, applies deny/namespace/isolate/select-one deterministically, and exposes only safe digests |
| R15 | Lifecycle state machine | Implemented | Validated atomic installation plus persistent enabled/disabled/quarantined overrides and accepting/draining/retired admission pins are wired into the local runtime and independent admin plane |
| R16 | Staged update and rollback | Implemented | SQLite stage/health/activate/rollback CAS pointers drive live generations, re-acquire and trust-check bytes, retain in-flight revision pins, and cancel supervised worker trees after drain deadlines |
| R17 | Least-privilege permissions | Implemented | Authenticated HTTP and explicit local CLI identities automatically resolve an immutable CAS-versioned Provider grant policy; search and execute share default-deny dependency intersections and normalized path/host/method/command/profile/secret-alias constraints |
| R18 | Exact-intent approvals | Implemented | Durable single-use approvals bind revision, operation, normalized-argument digest, tenant/principal/session/task, side effect, policy revision and expiry; CLI, Dashboard, loopback and mTLS decisions use authenticated scoped approver identities and clients without that channel fail closed |
| R19 | Supply-chain trust | Implemented | Production policy can require a pinned digest, publisher/registry-scoped certificate root, Ed25519 artifact signature, identity SAN, signed transparency checkpoint and RFC6962-style inclusion proof; online freshness, root/log revocation, replay and fork checks fail closed and every staged transition re-verifies the bundle |
| R20 | Secret broker | Implemented | Providers resolve aliases only inside scoped single-use worker callbacks; Windows DPAPI, macOS Keychain, and Linux Secret Service storage are selected strictly, with missing/headless/insecure backends failing closed |
| R21 | Provider isolation | Implemented | Supervised workers enforce hard deadlines, process-tree cancellation and bounded JSON IPC; Windows Job Objects enforce CPU/RAM/tree limits, while the mandatory Ubuntu 24.04 gate proves Landlock allow-root filesystem confinement and libseccomp network denial for malicious providers and descendants |
| R22 | Typed failure and retry | Implemented | Real local adapters run through structured failure classification, bounded retry/circuit breaking, explicit not-applied certainty, side-effect/idempotency gates, hard deadlines, and supervised cancellation |
| R23 | Reasoning-tier routing | Implemented | Every shared adapter applies a budget/risk decision before work; optional real model execution is bound to the selected endpoint, model, effort, cost and latency, with post-use enforcement and bounded failure escalation |
| R24 | Anti-loop escalation | Implemented | SQLite task orchestration persists only attempt/evidence digests, caps escalation and returns an observable stop decision across restarts and concurrent callers |
| R25 | Hierarchical budgets | Implemented | HTTP and CLI use durable HMAC-scoped tenant/principal/session/task trees with atomic ancestor reservation/reconciliation across restarts; reasoning, bytes, loads and executions share hard limits without storing raw scope |
| R26 | Context residency and eviction | Implemented | Scoped residency applies budget-driven eviction and emits bounded events; a negotiated durable removal contract stays pending through retries and becomes confirmed only after a generation-bound positive client acknowledgement, while unsupported clients are reported honestly |
| R27 | Tenant/session isolation | Implemented | HMAC-opaque tenant/principal/session/task scopes partition approvals, idempotency, audit, budgets, reasoning, grants, RAG and context residency; concurrent cross-tenant/session probes reveal neither private values nor raw scope identifiers |
| R28 | Multi-client consistency | Implemented | Library, CLI, MCP and HTTP data paths share the strict negotiated envelope and conformance fixtures; CLI, Dashboard, loopback and mTLS management paths share the authenticated admin dispatcher with equivalent identity, role and safe-error behavior |
| R29 | Concurrency and idempotency | Implemented | SQLite exact-slot idempotency and accepting/draining/retired revision pins are wired into shared runtime services; concurrent duplicates perform one side effect and cancellable deadlines retain or release pins safely |
| R30 | Observable, private audit | Implemented | Default HTTP/MCP runtimes use chained redacted audit and bounded privacy-safe spans/SQLite aggregates; verification, retention and export are available and signing material uses protected local platform storage |
| R31 | Degraded operation | Implemented | Atomic last-good catalog fallback and query-time policy-revision/provider-circuit observations feed the TTL-aware dependency matrix; unknown or unavailable policy/provider state fails closed, while only explicit bounded stale catalog fallbacks report degraded operation |
| R32 | Scale evidence | Implemented | CI gates fixed-seed 10k metadata search and 100 concurrent real service executions; the signed million-chunk cold/warm/concurrent replay uses production `DiskRagIndex`, while separate tests enforce its tenant/ACL semantics |
| R33 | Compatibility and migration | Implemented | The published lifecycle policy enforces a minimum 180-day deprecation window and migration target; all four adapters reject unsupported old clients identically, required features fail closed, and v1alpha0 documents migrate idempotently |
| R34 | Self-hosted operation | Implemented | Clean Linux/Windows wheel gates run offline discovery, search, load, execution, audit and local RAG; the checked-in self-hosted reference profile binds split mTLS planes, dependency freshness, fail-closed confinement requirements and reproducible configuration digest without a cloud dependency |
| R35 | Release gates | Implemented | Mandatory CI is green across Python 3.11/3.12/3.13, Windows wheel smoke, Ubuntu Landlock/seccomp and real Chrome; replayable 10k/1m, five-provider adversarial and real Codex cost/quality evidence feed a fail-closed fresh same-revision signed release certification workflow |
| R36 | Context-external UI/plugin | Implemented | The packaged `/helpme` and `/myskills` entry points remain outside task context; isolated real-browser tests cover search/lifecycle/language/approval/context/back/home and fresh install/upgrade proves live three-tool MCP inventory without native slash collisions |

## Direct evidence index

These are narrow, reproducible evidence paths; they do not close a `Partial` row's remaining gate.

| ID | Source evidence | Verification evidence |
|---|---|---|
| R1 | `http_control.py`, `admin_control.py`, `runtime.py` | `test_http_control.py`, `test_admin_control.py` |
| R2 | `benchmarks/harness.py`, `benchmarks/codex_live_eval.py`, `benchmarks/artifacts/codex-live-eval.json` | `test_benchmark.py`, `test_codex_live_eval.py` |
| R3 | `models.py`, `providers/base.py`, `registry.py` | `test_provider_conformance_matrix.py`, `test_registry.py` |
| R4 | `mcp_server.py`, `service_adapter.py` | `test_mcp_server.py`, `test_provider_conformance_matrix.py` |
| R5 | `search.py` | `test_search.py`, `test_scale_benchmark.py` |
| R6 | `service.py`, `references.py` | `test_progressive_rehydration.py` |
| R7 | `references.py` | `test_references.py` |
| R8 | `manifest.py`, `manifest_yaml.py`, `manifest_export.py` | `test_manifest.py`, `test_manifest_yaml.py` |
| R9 | `registry.py`, `models.py` | `test_registry.py`, `test_references.py` |
| R10 | `search.py`, `authorization.py` | `test_search.py`, `test_runtime_authorization.py` |
| R11 | `local_catalog.py`, `rag_index.py` | `test_local_catalog.py`, `test_rag_index.py` |
| R12 | `providers/rag.py` | `test_rag_provider.py` |
| R13 | `activation_lock.py`, `registry.py` | `test_activation_lock.py`, `test_registry.py` |
| R14 | `projections.py`, `registry.py` | `test_projection_admission.py`, `test_projections.py` |
| R15 | `state.py`, `draining.py` | `test_state.py`, `test_drained_service.py` |
| R16 | `lifecycle.py`, `update_store.py` | `test_lifecycle.py`, `test_update_store.py` |
| R17 | `authorization.py`, `service.py` | `test_authorization.py`, `test_runtime_authorization.py` |
| R18 | `approval_store.py`, `admin_control.py` | `test_approval_store.py`, `test_admin_control.py` |
| R19 | `supply_chain.py`, `lifecycle.py` | `test_supply_chain.py`, `test_supply_chain_public.py` |
| R20 | `secret_broker.py` | `test_secret_broker.py`, `test_provider_conformance_matrix.py` |
| R21 | `supervision.py`, `linux_sandbox.py`, `.github/workflows/ci.yml` | `test_supervision.py`, `test_linux_sandbox.py`, mandatory Ubuntu CI |
| R22 | `resilience.py`, `service.py` | `test_resilience.py`, `test_service.py` |
| R23 | `reasoning.py`, `reasoning_store.py` | `test_reasoning.py`, `test_reasoning_store.py` |
| R24 | `orchestration.py` | `test_orchestration.py` |
| R25 | `hierarchical_budget.py`, `runtime.py` | `test_hierarchical_budget.py`, `test_runtime_http_budget.py` |
| R26 | `residency.py`, `context_state.py` | `test_residency.py`, `test_context_state.py` |
| R27 | `tenancy.py`, `approval_store.py`, `idempotency.py`, `audit.py` | `test_tenant_business_isolation.py` |
| R28 | `protocol.py`, `service_adapter.py` | `test_protocol.py`, `test_cli_shared_adapter.py` |
| R29 | `idempotency.py`, `draining.py` | `test_idempotency.py`, `test_draining.py` |
| R30 | `observability.py`, `secure_audit.py` | `test_observability.py`, `test_secure_audit.py` |
| R31 | `degraded.py`, `local_runtime.py` | `test_degraded.py`, `test_runtime_degraded.py` |
| R32 | `benchmarks/scale.py`, `benchmarks/rag_scale.py` | `test_scale_benchmark.py`, `test_rag_scale_benchmark.py` |
| R33 | `compatibility.py`, `migration.py` | `test_compatibility.py`, `test_migration.py` |
| R34 | `cli.py`, `runtime.py` | `test_cli.py`, `test_wheel_smoke.py` |
| R35 | `.github/workflows/ci.yml`, `.github/workflows/release-certification.yml`, `release_certification.py` | `test_release_gate.py`, `test_release_certification.py`, browser and Linux sandbox CI |
| R36 | `webui.py`, `plugins/capabilityhub` | `test_webui.py`, `test_plugin_package.py` |

## Completion rule

The project may be marked complete only when every row is `Implemented`, the corresponding broad-scope
evidence is reproducible from a clean checkout, and no release document contradicts that result.
