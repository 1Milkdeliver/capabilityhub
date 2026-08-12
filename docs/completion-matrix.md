# Requirement completion matrix

This file is the release truth table for the 36 discovery rounds. `Implemented` means the current
tree contains a real path and relevant verification. `Partial` means useful code exists but the stated
acceptance scope is not yet proven. `Open` means the required subsystem is absent. A green unit-test
run does not upgrade a row by itself.

| ID | Requirement | Current state | Evidence or remaining gate |
|---|---|---|---|
| R1 | Separate control and data planes | Partial | Data HTTP exposes only three capability operations; a separate loopback admin endpoint has role-scoped, expiring, single-use credentials. Dashboard/CLI management paths still do not all share that plane and no remote deployment profile exists |
| R2 | End-to-end cost and quality targets | Partial | Structural disclosure benchmark exists; real task quality, latency, tool-call and billed-token evidence remains open |
| R3 | One abstraction for Skill/MCP/CLI/API/RAG | Implemented | Manifest, registry and provider protocol cover all five; configured reference adapters execute four and Skill loads instructions |
| R4 | Three meta-tools invoke every kind | Partial | The exact three schemas and five-kind provider matrix exist, but one uniform three-meta-tool integration does not yet execute/load every kind without conditional MCP coverage |
| R5 | Compact search under hard budgets | Partial | Search has a 900 portable-token total cap and top-8 default; explicit per-card/total byte limits and the required correct top-3 benchmark remain open |
| R6 | Progressive selective loading | Implemented | Sections/operations are selectable; bounded dependency/conflict notices, omission counts, and scope/revision/expiry-bound opaque rehydration handles support exact continuation |
| R7 | Revision-bound references | Implemented | Signed scope/purpose/revision/expiry references reject stale and tampered input |
| R8 | Manifest as source of truth | Partial | JSON and bounded safe YAML parse/export inert v1alpha1 data, but driver-specific configuration and required-semantics rejection are not proven at every installation/activation entry point |
| R9 | Stable identity and digest | Implemented | Immutable coordinate/version/digest revisions and activation pointers are enforced |
| R10 | Explainable authorized search | Partial | Deterministic lexical reasons and pre-disclosure permission filtering exist; hybrid retrieval, configurable fitness weights, and per-capability availability filtering remain open |
| R11 | Catalog/RAG index separation | Partial | Catalog stays metadata-only and local RAG scans bounded text; persistent ACL index remains open |
| R12 | Bounded RAG results and citations | Partial | Local retrieval enforces top-k, scan/deadline/token bounds and citations; response-byte/filter/expansion semantics and caller-scope ACL enforcement remain open |
| R13 | Dependency resolution and lock | Implemented | Constraints/cycles are validated and a deterministic exact-revision activation lock with transitive dependency closure detects missing, extra, or drifted active state |
| R14 | Conflict detection and policy | Implemented | Registry admission resolves automatic identity/name/route/port/path/permission claims before activation, applies deny/namespace/isolate/select-one deterministically, and exposes only safe digests |
| R15 | Lifecycle state machine | Partial | Persistent enabled/disabled/quarantined overrides plus a real service drain wrapper enforce accepting/draining/retired admission pins; install orchestration and default runtime wiring remain open |
| R16 | Staged update and rollback | Partial | SQLite stage/health/activate/rollback CAS pointers and in-flight pins drive live generations; every forward transition now re-acquires and trust-verifies bytes, while automatic provider-worker drain/cancel wiring remains open |
| R17 | Least-privilege permissions | Partial | The optional service authorizer now shares search/execute decisions and attenuates dependency permissions plus path/host/method/command/profile/secret-alias arguments; automatic grant derivation for every configured provider remains open |
| R18 | Exact-intent approvals | Partial | Durable single-use approvals bind exact intent and scoped admin approvers are authenticated; Dashboard/CLI approval surfaces are not all routed through the independent admin plane and remote approver deployment remains open |
| R19 | Supply-chain trust | Partial | Every forward staged transition re-acquires bytes and supports explicit HMAC-local or optional Ed25519 publisher/issuer/subject/expiry/revocation/transparency policy; certificate-chain, online transparency proof, and key distribution remain open |
| R20 | Secret broker | Partial | An in-memory broker provides scoped, expiring, use-limited handles and resolves aliases only inside trusted callbacks; provider wiring and OS-backed storage remain open |
| R21 | Provider isolation | Partial | Local configured execution uses supervised spawned workers with bounded JSON IPC; OS resource sandboxing remains open |
| R22 | Typed failure and retry | Partial | Safe structured errors plus optional service retries/circuit breaking enforce typed retryability, explicit not-applied certainty, side-effect/idempotency gates and deadlines; real-adapter failure classification remains open |
| R23 | Reasoning-tier routing | Partial | Persistent CLI/Dashboard advice selects the cheapest safe affordable tier; actual model endpoint enforcement and measured quality remain open |
| R24 | Anti-loop escalation | Implemented | SQLite task orchestration persists only attempt/evidence digests, caps escalation and returns an observable stop decision across restarts and concurrent callers |
| R25 | Hierarchical budgets | Partial | Loopback HTTP uses a durable HMAC-scoped tenant/principal/session/task tree with atomic ancestor admission across restarts; CLI remains single-scope and distributed/global accounting remains open |
| R26 | Context residency and eviction | Partial | Local load records metadata-only residency, atomically persists access/pins and exposes real evictions; model-client context removal cannot be enforced by the current transport |
| R27 | Tenant/session isolation | Partial | Trusted HTTP identity now partitions approvals, idempotency and audit queries with opaque HMAC keys; other business stores, local management paths and distributed isolation remain open |
| R28 | Multi-client consistency | Partial | Library/data HTTP/MCP and capability CLI calls share the strict envelope; management CLI/Dashboard use separate direct/admin paths and no remote multi-user conformance profile exists |
| R29 | Concurrency and idempotency | Partial | SQLite conservative idempotency plus accepting/draining/retired admission pins and cancellable deadline requests exist; service wiring and distributed storage remain open |
| R30 | Observable, private audit | Partial | Redacted views, HMAC audit retention/export, and the shared real-service adapter's optional bounded privacy-safe spans/SQLite aggregate metrics exist; default runtime enablement, external export and OS-backed key management remain open |
| R31 | Degraded operation | Partial | Atomic last-good catalog fallback plus a TTL-aware registry/index/policy/provider matrix fail closed unless an explicit bounded safe fallback applies; opt-in connection probes add transport evidence but live policy/provider observation wiring remains open |
| R32 | Scale evidence | Partial | CI covers fixed-seed 10k metadata/100 reads and a separate on-disk million-chunk FTS replay artifact; the latter is not the production RAG provider, carries no ACL model, and proves neither model nor provider quality |
| R33 | Compatibility and migration | Partial | Feature handshake fails closed on unknown required semantics and v1alpha0 aliases migrate idempotently; published deprecation windows and old-client/new-server transport conformance remain open |
| R34 | Self-hosted operation | Partial | Clean local wheel, CLI, Dashboard and adapters run; production profile and isolated workers remain open |
| R35 | Release gates | Partial | Cross-platform CI adds disclosure and scale gates plus one five-kind real-provider security matrix; automated browser, external-provider adversarial and model-backed gates remain open |
| R36 | Context-external UI/plugin | Partial | Loopback Dashboard manages search/lifecycle/language/approvals/context and shows reasoning; packaged installation freshness and remaining menu action coverage remain open |

## Direct evidence index

These are narrow, reproducible evidence paths; they do not close a `Partial` row's remaining gate.

| ID | Source evidence | Verification evidence |
|---|---|---|
| R1 | `http_control.py`, `admin_control.py`, `runtime.py` | `test_http_control.py`, `test_admin_control.py` |
| R2 | `benchmarks/harness.py`, `benchmarks/model_eval.py` | `test_benchmark.py`, `test_model_eval.py` |
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
| R21 | `supervision.py` | `test_supervision.py`, `test_provider_conformance_matrix.py` |
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
| R35 | `.github/workflows/ci.yml`, `benchmarks/release_gate.py` | `test_release_gate.py`, browser tests |
| R36 | `webui.py`, `plugins/capabilityhub` | `test_webui.py`, `test_plugin_package.py` |

## Completion rule

The project may be marked complete only when every row is `Implemented`, the corresponding broad-scope
evidence is reproducible from a clean checkout, and no release document contradicts that result.
