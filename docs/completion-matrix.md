# Requirement completion matrix

This file is the release truth table for the 36 discovery rounds. `Implemented` means the current
tree contains a real path and relevant verification. `Partial` means useful code exists but the stated
acceptance scope is not yet proven. `Open` means the required subsystem is absent. A green unit-test
run does not upgrade a row by itself.

| ID | Requirement | Current state | Evidence or remaining gate |
|---|---|---|---|
| R1 | Separate control and data planes | Partial | Three MCP meta-tools exist; authenticated control-plane credentials remain open |
| R2 | End-to-end cost and quality targets | Partial | Structural disclosure benchmark exists; real task quality, latency, tool-call and billed-token evidence remains open |
| R3 | One abstraction for Skill/MCP/CLI/API/RAG | Implemented | Manifest, registry and provider protocol cover all five; configured reference adapters execute four and Skill loads instructions |
| R4 | Three meta-tools invoke every kind | Partial | Search/load/execute service is shared; packaged real-adapter conformance matrix remains open |
| R5 | Compact search under hard budgets | Implemented | Search is capped at 900 portable tokens and CLI result limits are bounded |
| R6 | Progressive selective loading | Partial | Sections and operations are selectable; dependency/conflict notices and rehydration handles remain open |
| R7 | Revision-bound references | Implemented | Signed scope/purpose/revision/expiry references reject stale and tampered input |
| R8 | Manifest as source of truth | Partial | JSON v1alpha1, inert driver config, deterministic export and explicit alias migration exist; YAML remains open |
| R9 | Stable identity and digest | Implemented | Immutable coordinate/version/digest revisions and activation pointers are enforced |
| R10 | Explainable authorized search | Implemented | Deterministic lexical reasons and pre-disclosure permission filtering exist |
| R11 | Catalog/RAG index separation | Partial | Catalog stays metadata-only and local RAG scans bounded text; persistent ACL index remains open |
| R12 | Bounded RAG results and citations | Implemented | Local adapter enforces top-k, file/byte/deadline bounds and relative line citations |
| R13 | Dependency resolution and lock | Partial | Constraints/cycles are validated; persistent activation lock export remains open |
| R14 | Conflict detection and policy | Partial | Declared typed conflicts are enforced; automatic port/path/route projection policies remain open |
| R15 | Lifecycle state machine | Partial | Persistent enabled/disabled/quarantined overrides exist; install/update/retire/drain remains open |
| R16 | Staged update and rollback | Partial | SQLite stage/health/activate/rollback CAS pointers and in-flight pins drive live catalog generations; artifact acquisition/signature verification and worker draining remain open |
| R17 | Least-privilege permissions | Partial | Manifest permissions are denied by default; argument/path/host/secret attenuation remains open |
| R18 | Exact-intent approvals | Partial | Durable single-use queue binds actor/task/revision/operation/argument digest/side effect/policy/expiry and has CLI/Dashboard decisions; authenticated remote approver identity remains open |
| R19 | Supply-chain trust | Partial | Digests, sources and trust tiers exist; signatures, attestations and publisher policy remain open |
| R20 | Secret broker | Partial | Environment-name indirection and redaction exist; brokered scoped secret handles remain open |
| R21 | Provider isolation | Partial | Local configured execution uses supervised spawned workers with bounded JSON IPC; OS resource sandboxing remains open |
| R22 | Typed failure and retry | Partial | Safe structured errors exist; class-wide retry/idempotency conformance remains open |
| R23 | Reasoning-tier routing | Partial | Persistent CLI/Dashboard advice selects the cheapest safe affordable tier; actual model endpoint enforcement and measured quality remain open |
| R24 | Anti-loop escalation | Implemented | SQLite task orchestration persists only attempt/evidence digests, caps escalation and returns an observable stop decision across restarts and concurrent callers |
| R25 | Hierarchical budgets | Partial | Local runtime uses atomic restart-safe SQLite accounting; durable hierarchical child scopes remain open |
| R26 | Context residency and eviction | Partial | Local load records metadata-only residency, atomically persists access/pins and exposes real evictions; model-client context removal cannot be enforced by the current transport |
| R27 | Tenant/session isolation | Partial | References bind scopes; authenticated principals and cross-tenant storage tests remain open |
| R28 | Multi-client consistency | Partial | Library, CLI and MCP exist; HTTP adapter, handshake, stream/cancel conformance remains open |
| R29 | Concurrency and idempotency | Partial | SQLite conservative idempotency exists; lifecycle drain/cancel and distributed storage remain open |
| R30 | Observable, private audit | Partial | Redacted views plus optional HMAC chain, checkpoint, bounded retention and verified export exist; external metrics/traces and OS-backed key management remain open |
| R31 | Degraded operation | Partial | Atomic last-good catalog fallback exists; dependency freshness matrix remains open |
| R32 | Scale evidence | Partial | 100-definition deterministic gate exists; 10k/1m/concurrency/p95 evidence remains open |
| R33 | Compatibility and migration | Partial | Feature handshake fails closed on unknown required semantics and v1alpha0 aliases migrate idempotently; published deprecation windows and old-client/new-server transport conformance remain open |
| R34 | Self-hosted operation | Partial | Clean local wheel, CLI, Dashboard and adapters run; production profile and isolated workers remain open |
| R35 | Release gates | Partial | Ruff/mypy/pytest/wheel smoke plus non-oracle lexical, failure and cache artifacts exist; browser, real-provider adversarial and model-backed gates remain open |
| R36 | Context-external UI/plugin | Partial | Loopback Dashboard manages search/lifecycle/language/approvals/context and shows reasoning; packaged installation freshness and remaining menu action coverage remain open |

## Completion rule

The project may be marked complete only when every row is `Implemented`, the corresponding broad-scope
evidence is reproducible from a clean checkout, and no release document contradicts that result.
