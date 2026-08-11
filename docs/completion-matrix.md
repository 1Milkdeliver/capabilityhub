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
| R4 | Three meta-tools invoke every kind | Implemented | One table-driven real-provider matrix covers Skill load-only behavior plus CLI/API/RAG/MCP service load/execute-or-retrieve, deny, failure, revision, budget and canary boundaries |
| R5 | Compact search under hard budgets | Implemented | Search is capped at 900 portable tokens and CLI result limits are bounded |
| R6 | Progressive selective loading | Partial | Sections and operations are selectable; dependency/conflict notices and rehydration handles remain open |
| R7 | Revision-bound references | Implemented | Signed scope/purpose/revision/expiry references reject stale and tampered input |
| R8 | Manifest as source of truth | Implemented | JSON and resource-bounded safe YAML parse into the same inert v1alpha1 model; deterministic JSON export and explicit alias migration are verified |
| R9 | Stable identity and digest | Implemented | Immutable coordinate/version/digest revisions and activation pointers are enforced |
| R10 | Explainable authorized search | Implemented | Deterministic lexical reasons and pre-disclosure permission filtering exist |
| R11 | Catalog/RAG index separation | Partial | Catalog stays metadata-only and local RAG scans bounded text; persistent ACL index remains open |
| R12 | Bounded RAG results and citations | Implemented | Local adapter enforces top-k, file/byte/deadline bounds and relative line citations |
| R13 | Dependency resolution and lock | Implemented | Constraints/cycles are validated and a deterministic exact-revision activation lock with transitive dependency closure detects missing, extra, or drifted active state |
| R14 | Conflict detection and policy | Partial | Declared conflicts plus deterministic hashed port/path/route/name/permission projection policies support deny/namespace/isolate/select-one; automatic registry admission wiring remains open |
| R15 | Lifecycle state machine | Partial | Persistent enabled/disabled/quarantined overrides plus a real service drain wrapper enforce accepting/draining/retired admission pins; install orchestration and default runtime wiring remain open |
| R16 | Staged update and rollback | Partial | SQLite stage/health/activate/rollback CAS pointers and in-flight pins drive live catalog generations; artifact acquisition, mandatory trust-verifier wiring, and worker draining remain open |
| R17 | Least-privilege permissions | Partial | The optional service authorizer now shares search/execute decisions and attenuates dependency permissions plus path/host/method/command/profile/secret-alias arguments; automatic grant derivation for every configured provider remains open |
| R18 | Exact-intent approvals | Partial | Durable single-use queue binds actor/task/revision/operation/argument digest/side effect/policy/expiry and has CLI/Dashboard decisions; authenticated remote approver identity remains open |
| R19 | Supply-chain trust | Partial | Artifact digests, publisher/registry policy, revocation, expiry and a local HMAC attestation profile exist; public-key/Sigstore identity and staged-acquisition enforcement remain open |
| R20 | Secret broker | Partial | An in-memory broker provides scoped, expiring, use-limited handles and resolves aliases only inside trusted callbacks; provider wiring and OS-backed storage remain open |
| R21 | Provider isolation | Partial | Local configured execution uses supervised spawned workers with bounded JSON IPC; OS resource sandboxing remains open |
| R22 | Typed failure and retry | Partial | Safe structured errors plus optional service retries/circuit breaking enforce typed retryability, explicit not-applied certainty, side-effect/idempotency gates and deadlines; real-adapter failure classification remains open |
| R23 | Reasoning-tier routing | Partial | Persistent CLI/Dashboard advice selects the cheapest safe affordable tier; actual model endpoint enforcement and measured quality remain open |
| R24 | Anti-loop escalation | Implemented | SQLite task orchestration persists only attempt/evidence digests, caps escalation and returns an observable stop decision across restarts and concurrent callers |
| R25 | Hierarchical budgets | Partial | Local runtime uses atomic restart-safe SQLite accounting; durable hierarchical child scopes remain open |
| R26 | Context residency and eviction | Partial | Local load records metadata-only residency, atomically persists access/pins and exposes real evictions; model-client context removal cannot be enforced by the current transport |
| R27 | Tenant/session isolation | Partial | References bind scopes and a generic SQLite KV/cache/event store HMAC-partitions tenant/principal/session/task with cross-tenant concurrency tests; business stores and authenticated principals remain open |
| R28 | Multi-client consistency | Partial | A strict real-service adapter covers library/CLI/MCP/HTTP envelope kinds and `http-serve` runs authenticated loopback roundtrips; the existing CLI/MCP implementations are not yet refactored onto that adapter and remote HTTP remains out of scope |
| R29 | Concurrency and idempotency | Partial | SQLite conservative idempotency plus accepting/draining/retired admission pins and cancellable deadline requests exist; service wiring and distributed storage remain open |
| R30 | Observable, private audit | Partial | Redacted views plus optional HMAC chain, checkpoint, bounded retention and verified export exist; external metrics/traces and OS-backed key management remain open |
| R31 | Degraded operation | Partial | Atomic last-good catalog fallback plus a TTL-aware registry/index/policy/provider matrix fail closed unless an explicit bounded safe fallback applies; live dependency observation wiring remains open |
| R32 | Scale evidence | Partial | CI measures fixed-seed 10k metadata, top-8 fixtures, cold/warm and 100 concurrent reads with p50/p95/max; 1m RAG chunks and production-provider scale remain open |
| R33 | Compatibility and migration | Partial | Feature handshake fails closed on unknown required semantics and v1alpha0 aliases migrate idempotently; published deprecation windows and old-client/new-server transport conformance remain open |
| R34 | Self-hosted operation | Partial | Clean local wheel, CLI, Dashboard and adapters run; production profile and isolated workers remain open |
| R35 | Release gates | Partial | Cross-platform CI adds disclosure and scale gates plus one five-kind real-provider security matrix; automated browser, external-provider adversarial and model-backed gates remain open |
| R36 | Context-external UI/plugin | Partial | Loopback Dashboard manages search/lifecycle/language/approvals/context and shows reasoning; packaged installation freshness and remaining menu action coverage remain open |

## Completion rule

The project may be marked complete only when every row is `Implemented`, the corresponding broad-scope
evidence is reproducible from a clean checkout, and no release document contradicts that result.
