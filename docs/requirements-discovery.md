# CapabilityHub Requirement Discovery

Status: proposed baseline  
Audience: maintainers, integrators, client authors, security reviewers  
Scope: an open-source hub that discovers, loads, governs, and executes Skills, MCP tools, CLI commands, APIs, and RAG sources while reducing model-visible tokens, context occupancy, and unnecessary reasoning.

## Method and decision rules

These rounds turn the product brief into falsifiable requirements. “Evidence” distinguishes facts supplied by the brief from assumptions that must be validated with prototypes or users. Requirements use **MUST**, **SHOULD**, and **MAY** in the RFC 2119 sense. An assumption remains an explicit product risk until its validation criterion passes.

### Round 1 — Primary actor

- **Question:** Who directly operates CapabilityHub: the model, an application developer, or a platform administrator?
- **Why it matters:** Each actor needs a different surface, and mixing them would enlarge the model context and the security boundary.
- **Evidence/assumption:** The brief asks to control model-facing capabilities and reduce reasoning cost; it also asks for permissions and observability. This implies at least a model/runtime actor and an administrative actor.
- **Decision/answer:** Treat the model/runtime as the data-plane caller and developers/operators as control-plane callers. Humans may use the CLI for both, but authorization remains plane-specific.
- **Resulting requirement:** The system MUST expose separate control-plane and data-plane interfaces and credentials; model-facing schemas MUST omit administrative operations by default.
- **Validation criterion:** A data-plane credential cannot install, enable, grant, or mutate policy, and a model session can operate using only the three meta-tools.
- **Risk if wrong:** A single combined surface leaks privileged actions into prompts and creates an escalation path.

### Round 2 — Primary product outcome

- **Question:** Is the goal merely fewer tool definitions, or lower total task cost without unacceptable quality loss?
- **Why it matters:** Minimizing visible schemas alone can increase search calls, latency, and reasoning, making the overall task more expensive.
- **Evidence/assumption:** The brief names token, context, and reasoning cost, which are related but distinct. We assume task success and latency cannot be sacrificed indiscriminately.
- **Decision/answer:** Optimize end-to-end successful-task cost, using model-visible tokens, context occupancy, reasoning tier usage, tool calls, latency, and success rate as a balanced scorecard.
- **Resulting requirement:** CapabilityHub MUST measure per-task discovery/load/execute cost and MUST support policy constraints for success-rate and latency floors alongside budget ceilings.
- **Validation criterion:** A benchmark report compares the hub with an “all tools eagerly exposed” baseline and shows cost changes without breaching configured quality/latency guardrails.
- **Risk if wrong:** The project reports impressive prompt shrinkage while real workloads become slower, less reliable, or more expensive.

### Round 3 — Canonical capability abstraction

- **Question:** Can Skills, MCP tools, CLIs, APIs, and RAG sources share one abstraction without erasing important differences?
- **Why it matters:** Separate registries make search and policy inconsistent; an over-general abstraction cannot express execution or content semantics safely.
- **Evidence/assumption:** All five types have identity, descriptions, inputs, permissions, lifecycle, and provenance, but only some are executable and RAG has retrieval-specific behavior.
- **Decision/answer:** Use one `Capability` envelope with kind-specific driver configuration and operations.
- **Resulting requirement:** Every registered item MUST implement the common metadata contract; drivers MUST declare supported operations such as `describe`, `execute`, `retrieve`, or `expand`.
- **Validation criterion:** One search query can return ranked results across all five kinds, while invalid operations are rejected before driver invocation.
- **Risk if wrong:** Either clients need five bespoke integrations or unsafe generic execution paths appear.

### Round 4 — Model-visible surface

- **Question:** What is the smallest useful interface that lets a model discover and use an unbounded catalog?
- **Why it matters:** Every always-visible tool and schema consumes prompt space and expands the model’s decision tree.
- **Evidence/assumption:** Search, selective schema loading, and invocation are the irreducible phases. We assume clients can make multiple tool calls in a turn or task.
- **Decision/answer:** Expose three stable meta-tools: `capability.search`, `capability.load`, and `capability.execute` (with retrieval represented as an execute operation).
- **Resulting requirement:** A client MUST be able to complete discovery and invocation with only these three schemas present; direct capability projection MAY be supported as an optimization.
- **Validation criterion:** An integration test executes one capability of every kind without injecting its full schema before `load`.
- **Risk if wrong:** Too few operations make use awkward; too many restore the context-cost problem.

### Round 5 — Search result granularity

- **Question:** How much information should search return before a capability is loaded?
- **Why it matters:** Full schemas defeat lazy loading, while names alone cause repeated searches and bad selections.
- **Evidence/assumption:** A model generally needs identity, a short purpose, kind, trust, permissions summary, and estimated cost to shortlist a tool.
- **Decision/answer:** Return compact cards with bounded text and opaque revision-bound references, never full schemas by default.
- **Resulting requirement:** Search responses MUST enforce per-result and total token/byte limits and SHOULD include `id`, `revision`, `kind`, `summary`, `risk`, `cost_hint`, and `match_reason`.
- **Validation criterion:** With 10,000 registered capabilities, a default top-8 result stays within the configured response budget and leads to the correct top-3 shortlist on the benchmark set.
- **Risk if wrong:** Search either floods context or becomes too vague to guide selection.

### Round 6 — Progressive loading

- **Question:** Must loading be all-or-nothing?
- **Why it matters:** Complex skills and APIs may have large documentation, examples, and schemas that are rarely all needed.
- **Evidence/assumption:** The brief specifically targets context reduction. Capability artifacts naturally divide into summary, contract, instructions, examples, and references.
- **Decision/answer:** Support named load sections and bounded expansion, with the executable contract separated from optional guidance.
- **Resulting requirement:** `load` MUST accept sections, a maximum budget, and a revision; it MUST return only requested authorized material plus dependency/conflict notices.
- **Validation criterion:** Loading an executable contract does not return examples or long references unless requested, and the response never exceeds its hard budget.
- **Risk if wrong:** A single load can consume the entire context window or omit data needed for safe execution.

### Round 7 — Execution indirection

- **Question:** Should `execute` accept arbitrary commands/URLs, or only loaded capability references?
- **Why it matters:** Arbitrary targets bypass registry governance, permission checks, provenance, and auditability.
- **Evidence/assumption:** The hub is a control point, not a generic shell or proxy.
- **Decision/answer:** Execute only immutable, revision-bound capability references issued by the hub, subject to policy; raw driver escape hatches are administrative and off by default.
- **Resulting requirement:** Data-plane execution MUST reject unregistered commands, URLs, packages, and stale/tampered references before side effects.
- **Validation criterion:** Attempts to invoke a raw shell command, altered revision, or unregistered endpoint fail with a structured pre-execution error.
- **Risk if wrong:** CapabilityHub becomes a privilege-bypass mechanism.

### Round 8 — Manifest as source of truth

- **Question:** Where are identity, contracts, permissions, dependencies, and cost hints declared?
- **Why it matters:** Heuristic introspection is inconsistent across sources and cannot support reproducible policy decisions.
- **Evidence/assumption:** Open-source ecosystems need a portable, reviewable, versioned format.
- **Decision/answer:** Define a declarative YAML/JSON manifest with a versioned schema and optional generated fields clearly marked.
- **Resulting requirement:** Every capability MUST resolve to a validated manifest before activation; manifests MUST be exportable and reviewable without running provider code.
- **Validation criterion:** Schema validation catches unknown required semantics, malformed permissions, and invalid driver configuration before installation completes.
- **Risk if wrong:** Runtime behavior depends on opaque adapter guesses and becomes impossible to audit.

### Round 9 — Stable identity and revision pinning

- **Question:** What identifies a capability across providers, versions, aliases, and updates?
- **Why it matters:** Display names collide and mutable references make execution non-reproducible.
- **Evidence/assumption:** Multiple registries and local packages will reuse common names.
- **Decision/answer:** Use canonical coordinates (`namespace/name`) plus semantic version and content digest; issue revision-bound load tokens.
- **Resulting requirement:** The registry MUST prevent coordinate collisions within a namespace and MUST record version plus digest for every activated revision.
- **Validation criterion:** Two packages with the same display name coexist, while changing artifact bytes changes the digest and invalidates old unpinned execution references according to policy.
- **Risk if wrong:** The wrong tool or changed code may run under a familiar name.

### Round 10 — Search ranking behavior

- **Question:** Should ranking be purely semantic?
- **Why it matters:** Semantic relevance alone can prefer risky, unavailable, expensive, or unauthorized capabilities.
- **Evidence/assumption:** Selection quality depends on intent match plus feasibility, trust, latency, and policy.
- **Decision/answer:** Use hybrid retrieval followed by policy filtering and a transparent reranker that accounts for operational fitness.
- **Resulting requirement:** Search MUST apply authorization and availability filters before returning results and MUST expose concise match/rank reasons; weights MUST be configurable.
- **Validation criterion:** An unauthorized exact semantic match is never returned, and ranking tests demonstrate deterministic tie-breaking for fixed index/configuration revisions.
- **Risk if wrong:** Models repeatedly choose unusable or unsafe capabilities.

### Round 11 — RAG indexing boundaries

- **Question:** Are capability discovery and user-document retrieval one index?
- **Why it matters:** They have different trust, freshness, chunking, permissions, and ranking requirements.
- **Evidence/assumption:** Capability manifests are curated control metadata; RAG documents can be large, tenant-private, and rapidly changing.
- **Decision/answer:** Keep catalog and content indexes logically separate, even if they share a storage engine.
- **Resulting requirement:** The hub MUST maintain separate index namespaces, ACL evaluation, retention, and freshness metadata for catalog entries versus RAG chunks.
- **Validation criterion:** A user-document ACL change affects RAG retrieval immediately without changing capability catalog visibility, and cross-tenant chunks never appear.
- **Risk if wrong:** Private content leaks through discovery or catalog quality degrades under document volume.

### Round 12 — RAG result shape

- **Question:** Should retrieval inject whole documents into context?
- **Why it matters:** Whole-document loading is one of the largest sources of context waste.
- **Evidence/assumption:** Most tasks need a few relevant passages and source metadata, with optional expansion.
- **Decision/answer:** Return budgeted passages with citations, deduplication, and expandable source handles.
- **Resulting requirement:** RAG drivers MUST support maximum tokens/bytes, top-k, filters, citation metadata, and a follow-up expansion mechanism.
- **Validation criterion:** Retrieval respects ACLs and hard response budgets, provides source/offset metadata, and can expand a selected passage without repeating unrelated chunks.
- **Risk if wrong:** Retrieval overwhelms context or yields claims that cannot be traced.

### Round 13 — Dependency model

- **Question:** How are transitive capability and runtime dependencies resolved?
- **Why it matters:** Skills can rely on tools; tools can require runtimes, servers, credentials, or other capabilities.
- **Evidence/assumption:** Dependency graphs will be unavoidable and may contain version conflicts or cycles.
- **Decision/answer:** Resolve a lockable directed graph during installation/activation, never ad hoc in the middle of model execution.
- **Resulting requirement:** Manifests MUST declare typed dependencies and version constraints; activation MUST produce a lock record or a structured conflict.
- **Validation criterion:** The resolver detects cycles, incompatible constraints, missing runtimes, and optional dependencies deterministically before activation.
- **Risk if wrong:** First-use failures become nondeterministic and difficult to diagnose.

### Round 14 — Conflict semantics

- **Question:** What counts as a conflict beyond package-version incompatibility?
- **Why it matters:** Two capabilities may have overlapping names, tool projections, ports, routes, exclusive resources, permission scopes, or contradictory instructions.
- **Evidence/assumption:** The brief explicitly calls for conflict handling across heterogeneous mechanisms.
- **Decision/answer:** Model conflicts as declared resources and policy rules, with deterministic resolution and no silent winner.
- **Resulting requirement:** Activation MUST detect identity, projection-name, route, exclusive-resource, dependency, and declared semantic conflicts; policy MAY select, namespace, isolate, or reject.
- **Validation criterion:** Each conflict class has a fixture whose outcome and explanation are stable across repeated resolution.
- **Risk if wrong:** Load order changes behavior or a capability silently shadows another.

### Round 15 — Lifecycle state machine

- **Question:** Which states separate package presence from actual usability?
- **Why it matters:** “Installed” does not mean indexed, configured, authorized, healthy, or active.
- **Evidence/assumption:** Operators need safe staging, rollback, quarantine, and garbage collection.
- **Decision/answer:** Use explicit states: discovered, installed, validated, configured, indexed, active, degraded, quarantined, disabled, retired.
- **Resulting requirement:** State transitions MUST be validated, audited, idempotent where possible, and driven through the control plane; only active/degraded capabilities are searchable under policy.
- **Validation criterion:** Invalid transitions fail without partial activation, and restart recovery converges to the last committed state.
- **Risk if wrong:** Half-configured capabilities appear usable and fail during user tasks.

### Round 16 — Update and rollback

- **Question:** Can updates replace an active revision in place?
- **Why it matters:** In-place mutation breaks in-flight tasks and makes rollback unreliable.
- **Evidence/assumption:** Capability providers will evolve independently and can introduce regressions.
- **Decision/answer:** Install revisions immutably, validate them side by side, then switch an activation pointer; retain rollback candidates by policy.
- **Resulting requirement:** Updates MUST support staged validation, atomic activation, health-gated rollout, and rollback without redownloading when retained.
- **Validation criterion:** In-flight requests finish on their pinned revision while new requests switch atomically, and a failed health gate restores the previous revision.
- **Risk if wrong:** Updates corrupt running sessions or cause extended downtime.

### Round 17 — Permission model

- **Question:** At what granularity are permissions expressed and checked?
- **Why it matters:** A binary trusted/untrusted flag cannot distinguish read from write, network destinations, file paths, secrets, or side effects.
- **Evidence/assumption:** MCP, CLI, and APIs can perform powerful operations. Permissions must be understandable to both policy engines and humans.
- **Decision/answer:** Use explicit capabilities/scopes for filesystem, network, process, secrets, data classes, side effects, and control-plane actions, with deny-by-default policy.
- **Resulting requirement:** Install-time requested permissions and run-time effective permissions MUST be separately recorded; every execution MUST be authorized against caller, tenant, capability, operation, arguments, and environment.
- **Validation criterion:** A capability granted read-only access cannot write, spawn a process, access undeclared hosts, or read undeclared secrets, including through a dependency.
- **Risk if wrong:** A benign-looking capability becomes a confused deputy.

### Round 18 — Consent and side effects

- **Question:** When must a human approve execution?
- **Why it matters:** Read operations and irreversible external actions have different risk; repeatedly asking also destroys usability.
- **Evidence/assumption:** Clients vary in their ability to show approval UI, and models may misclassify side effects.
- **Decision/answer:** Manifests declare side-effect classes, while policy determines allow, deny, or approval using argument-aware checks; uncertainty escalates rather than defaults to allow.
- **Resulting requirement:** The hub MUST return a structured `approval_required` challenge before invocation and MUST bind approval to the exact capability revision, operation, normalized arguments, actor, and expiry.
- **Validation criterion:** Changing any bound field invalidates approval, and clients without an approval channel receive a safe denial rather than an implicit grant.
- **Risk if wrong:** Users unknowingly authorize broader or altered actions.

### Round 19 — Supply-chain trust

- **Question:** What proves where a capability came from and what code will run?
- **Why it matters:** Open-source distribution creates dependency-confusion, tampering, and malicious update risks.
- **Evidence/assumption:** Not every ecosystem will provide signatures, so trust must be graded rather than all-or-nothing.
- **Decision/answer:** Record origin, digest, signature/attestation when present, publisher identity, install method, scan results, and local trust decisions.
- **Resulting requirement:** Policy MUST be able to require pinned digests, approved registries/publishers, signatures, or attestations by environment; search cards MUST surface trust tier.
- **Validation criterion:** Tampered artifacts fail digest validation, and production policy rejects an unsigned source while development policy can quarantine or explicitly allow it.
- **Risk if wrong:** Search relevance legitimizes untrusted code and exposes the host.

### Round 20 — Secret handling

- **Question:** Are credentials stored in manifests or passed through model-visible arguments?
- **Why it matters:** Both approaches leak secrets into repositories, prompts, logs, and traces.
- **Evidence/assumption:** API, MCP, and CLI providers often require credentials; installations may already use external secret managers.
- **Decision/answer:** Manifests declare logical secret references only; a secret broker resolves them at execution and injects them out of band.
- **Resulting requirement:** Secrets MUST NOT appear in catalog indexes, load responses, normalized arguments, event payloads, or persisted execution results; providers receive the minimum scoped secret.
- **Validation criterion:** Automated canary-secret tests find no secret in prompts, logs, traces, cache entries, or APIs, while execution succeeds through the broker.
- **Risk if wrong:** The hub centralizes and amplifies credential leakage.

### Round 21 — Failure isolation

- **Question:** What failure boundary contains a crashing or hanging provider?
- **Why it matters:** Heterogeneous third-party code can exhaust memory, block workers, corrupt state, or take down unrelated tools.
- **Evidence/assumption:** Drivers and deployment environments support different isolation strengths.
- **Decision/answer:** Execute through supervised provider workers with per-call deadlines, cancellation, resource limits, circuit breakers, and configurable process/container/remote isolation.
- **Resulting requirement:** No third-party driver code MAY run in the control-plane process; failures MUST be attributed to a provider and must not block unrelated capability pools.
- **Validation criterion:** Crash, hang, memory-pressure, malformed-output, and network-partition fault injection leave the control plane and unrelated providers healthy.
- **Risk if wrong:** One bad plugin becomes a hub-wide outage.

### Round 22 — Error contract and fallback

- **Question:** How should clients distinguish retryable failure, policy denial, invalid input, and provider bugs?
- **Why it matters:** Undifferentiated text errors force models to reason and retry blindly, wasting tokens and risking duplicate side effects.
- **Evidence/assumption:** Machine-actionable errors can drive deterministic retries and tier escalation.
- **Decision/answer:** Use a stable structured error taxonomy with retryability, blame domain, safe message, correlation ID, and optional fallback hints.
- **Resulting requirement:** Every data-plane operation MUST return typed errors; automatic retry MUST be limited to idempotent/retry-token-protected operations under policy.
- **Validation criterion:** Contract tests cover every error class, and a non-idempotent timeout is never automatically replayed without an idempotency guarantee.
- **Risk if wrong:** Models loop, duplicate side effects, or expose internal diagnostics.

### Round 23 — Reasoning-tier routing objective

- **Question:** Should the hub choose models solely by a static capability label?
- **Why it matters:** Task complexity, uncertainty, risk, budget, prior failures, and required context vary per invocation.
- **Evidence/assumption:** The brief requests a reasoning-tier router. We assume clients can offer multiple model/tier choices, but some cannot.
- **Decision/answer:** Route using policy plus observable task features, beginning with the cheapest eligible tier and escalating on bounded, explicit signals.
- **Resulting requirement:** The router MUST support eligibility constraints, cost/latency/quality objectives, confidence thresholds, escalation budgets, and a deterministic pass-through mode when the client owns model selection.
- **Validation criterion:** Replay tests produce explainable tier decisions; high-risk operations never use a tier below policy minimum; unsupported clients still function.
- **Risk if wrong:** The router either overspends systematically or degrades hard tasks unpredictably.

### Round 24 — Router feedback and anti-looping

- **Question:** What prevents repeated search/load/execute failures from endlessly escalating?
- **Why it matters:** Agent loops are a major hidden reasoning and token cost.
- **Evidence/assumption:** Repeated equivalent actions with unchanged evidence rarely improve outcomes.
- **Decision/answer:** Track normalized attempt signatures, evidence deltas, and escalation count in a task ledger; require progress or stop with a structured reason.
- **Resulting requirement:** Policy MUST cap repeated equivalent actions and reasoning escalations and MUST expose `no_progress`, `budget_exhausted`, or `human_input_required` outcomes.
- **Validation criterion:** A fixture with a permanently unavailable provider terminates within configured limits and reports the decisive missing condition.
- **Risk if wrong:** A low-cost routing strategy turns into expensive, unbounded retry behavior.

### Round 25 — Token budgeting

- **Question:** Is a global context limit sufficient?
- **Why it matters:** Search results, loaded contracts, RAG passages, provider output, and reserve for model reasoning compete for the same window.
- **Evidence/assumption:** Different task phases need explicit allocation and hard ceilings.
- **Decision/answer:** Maintain a hierarchical budget ledger: organization/tenant, task, phase, response, and context-slot budgets, with reservations before work.
- **Resulting requirement:** Search, load, retrieve, and execute MUST accept or inherit hard output budgets; the hub MUST reserve headroom and reject or truncate safely before exceeding limits.
- **Validation criterion:** Adversarially large provider output cannot exceed the call or task ceiling, and budget accounting reconciles estimated versus actual usage.
- **Risk if wrong:** One response crowds out reasoning or causes context overflow.

### Round 26 — Eviction and rehydration

- **Question:** What loaded material is removed when context or cache pressure rises?
- **Why it matters:** Simple LRU can evict a dependency still needed for an active plan; never evicting defeats bounded context.
- **Evidence/assumption:** Re-loading immutable sections is possible if references remain valid.
- **Decision/answer:** Separate model-context residency from server cache; pin active/approved dependencies, score other items by recency, reuse probability, size, reload cost, and sensitivity, and retain compact handles for rehydration.
- **Resulting requirement:** The hub MUST implement budget-triggered eviction with pinning and MUST make eviction decisions observable; sensitive entries MAY bypass persistent cache.
- **Validation criterion:** Under simulated pressure the system stays within budget, never evicts pinned contracts, and can rehydrate an evicted immutable section by digest.
- **Risk if wrong:** Tasks fail after needed context disappears or private content persists too long.

### Round 27 — Session and tenancy boundaries

- **Question:** Which state may be shared across calls, users, and tenants?
- **Why it matters:** Caching improves cost but can leak private search results, arguments, approvals, or RAG content.
- **Evidence/assumption:** Multi-client deployments are likely multi-user; capability metadata may be public while runtime material may not be.
- **Decision/answer:** Scope state explicitly as public, tenant, principal, session, or request; cache keys and authorization proofs include the relevant scope.
- **Resulting requirement:** No state MAY be promoted to a broader scope without policy and data classification; approvals and private retrieval results are session/principal bounded by default.
- **Validation criterion:** Cross-tenant cache-probing and concurrent-session tests cannot observe private identifiers, result sizes, or content.
- **Risk if wrong:** Optimization becomes a side channel or direct data leak.

### Round 28 — Multi-client support

- **Question:** Which clients are first-class, and how can they share behavior without sharing transport quirks?
- **Why it matters:** IDE agents, chat applications, CI jobs, SDKs, and MCP clients differ in streaming, approval UI, cancellation, and model control.
- **Evidence/assumption:** The brief explicitly requires multi-client support. A transport-neutral core avoids duplicating governance.
- **Decision/answer:** Define one internal operation contract and provide MCP server, HTTP/JSON API, CLI, and library adapters with negotiated features.
- **Resulting requirement:** All adapters MUST preserve identity, auth, budget, error, streaming, and cancellation semantics; unsupported features MUST be declared during handshake.
- **Validation criterion:** The same conformance suite runs against each adapter and yields equivalent authorization and execution outcomes.
- **Risk if wrong:** Security and behavior diverge by client, making incidents client-dependent.

### Round 29 — Concurrency and idempotency

- **Question:** How are duplicate requests and concurrent lifecycle changes handled?
- **Why it matters:** Clients retry on timeout, while operators may update or disable a capability during execution.
- **Evidence/assumption:** Network failures and concurrent requests are normal in multi-client systems.
- **Decision/answer:** Pin revisions at admission, require idempotency keys for replay-safe side effects, and use optimistic concurrency for control-plane mutations.
- **Resulting requirement:** Execution MUST record an admission revision and idempotency state; lifecycle mutation MUST require the current resource version/ETag.
- **Validation criterion:** Duplicate keyed requests produce one side effect and the same outcome; concurrent stale updates fail cleanly; disabling prevents new admissions but follows configured drain/cancel policy for in-flight work.
- **Risk if wrong:** Retries duplicate actions and administrative changes race unpredictably.

### Round 30 — Observability versus privacy

- **Question:** What must be observable without logging prompts, secrets, or retrieved content?
- **Why it matters:** Cost and failure optimization need detail, but raw payload capture creates a high-value sensitive dataset.
- **Evidence/assumption:** Most diagnosis can use structured metadata, hashes, sizes, classifications, timings, and opt-in redacted samples.
- **Decision/answer:** Emit correlated traces, metrics, audit events, and budget ledgers with privacy-safe defaults and explicit sampling/redaction policy.
- **Resulting requirement:** The hub MUST instrument every lifecycle and data-plane stage; payload bodies MUST be off by default, secrets always redacted, and retention/export configurable.
- **Validation criterion:** An operator can attribute latency, budget, routing, denial, retry, and provider errors by correlation ID without raw sensitive content; leak tests pass.
- **Risk if wrong:** Either outages are opaque or observability becomes a privacy incident.

### Round 31 — Availability and degraded operation

- **Question:** What happens if the registry, index, policy service, or a provider is unavailable?
- **Why it matters:** Centralization can turn the hub into a single point of failure, while permissive fallback can bypass policy.
- **Evidence/assumption:** Read-only cached metadata can often be used safely, but authorization and side-effect decisions cannot be guessed.
- **Decision/answer:** Define component-specific fail-open/fail-closed rules: authorization and unverified execution fail closed; signed immutable catalog reads may use bounded stale cache; provider failures are isolated.
- **Resulting requirement:** Every dependency MUST declare freshness and fallback policy, and responses MUST identify degraded/stale operation.
- **Validation criterion:** Dependency fault tests match the policy matrix, and no loss of policy connectivity enables a previously unauthorized operation.
- **Risk if wrong:** The system either becomes unnecessarily unavailable or silently unsafe.

### Round 32 — Performance and scale target

- **Question:** What scale and latency should the initial architecture be designed to meet?
- **Why it matters:** Without targets, indexing and isolation choices cannot be evaluated; premature distributed complexity also harms adoption.
- **Evidence/assumption:** An open-source baseline should run locally yet scale to a shared service. Initial assumed targets need field validation.
- **Decision/answer:** Target 10,000 active capability revisions per instance, 1,000,000 RAG chunks per tenant, 100 concurrent executions per node, search p95 under 150 ms, load p95 under 75 ms from cache, excluding provider time.
- **Resulting requirement:** Benchmarks MUST publish hardware, catalog size, tenant count, concurrency, cold/warm state, and percentile latency; storage interfaces MUST allow scale-out implementations.
- **Validation criterion:** The reference single-node deployment meets the stated targets on published benchmark hardware or documents the measured gap before release.
- **Risk if wrong:** The design is either unusable at realistic scale or burdened by infrastructure most adopters do not need.

### Round 33 — Compatibility and evolution

- **Question:** How can manifests, drivers, policies, and clients evolve independently?
- **Why it matters:** An open-source ecosystem cannot coordinate lockstep upgrades.
- **Evidence/assumption:** New capability kinds and permission scopes will appear; old clients must fail predictably.
- **Decision/answer:** Version external schemas and driver interfaces, negotiate features, preserve unknown extension fields, and reject unknown security semantics unless explicitly understood.
- **Resulting requirement:** The hub MUST publish compatibility rules, deprecation windows, migration tooling, and conformance fixtures for supported versions.
- **Validation criterion:** An older client interoperates with a newer server for shared features, while an unknown required permission or operation fails closed with an actionable error.
- **Risk if wrong:** Ecosystem upgrades become breaking events or new security semantics are silently ignored.

### Round 34 — Open-source operability

- **Question:** What is the minimum deployable system for contributors and small teams?
- **Why it matters:** A design that requires a distributed control plane, hosted vector database, or proprietary model blocks adoption and contribution.
- **Evidence/assumption:** “Open source” implies inspectable code and practical self-hosting, though managed components may be optional.
- **Decision/answer:** Ship a local single-process control service with isolated workers, embedded metadata/search defaults, filesystem artifacts, and optional external adapters for scale.
- **Resulting requirement:** Core discovery, governance, search, load, execute, audit, and RAG MUST run without a proprietary service; optional hosted integrations MUST be replaceable behind documented interfaces.
- **Validation criterion:** A clean-machine quickstart runs an end-to-end example locally, offline after dependencies are obtained, and the license/contributor documentation covers all core components.
- **Risk if wrong:** CapabilityHub is open-source in name but operationally dependent on a vendor.

### Round 35 — Validation strategy

- **Question:** How do we prove cost reduction without hiding regressions behind aggregate averages?
- **Why it matters:** Capability choice, security, and cost failures often occur only on particular task classes or long-tail providers.
- **Evidence/assumption:** A representative replay corpus and fault suite can expose regressions before production telemetry exists.
- **Decision/answer:** Maintain versioned scenario suites covering discovery, multi-step use, RAG, side effects, denial, conflicts, updates, outages, and adversarial payloads; report distributions by class.
- **Resulting requirement:** Release gates MUST include conformance, security, fault-injection, and end-to-end cost/quality benchmarks against an eager-exposure baseline.
- **Validation criterion:** CI produces comparable per-scenario success, token/context use, reasoning tier, calls, latency, and policy outcomes, with documented thresholds.
- **Risk if wrong:** Optimizations pass synthetic happy paths while harming real tasks or security.

## Consolidated acceptance outcomes

The discovery rounds imply five release-level outcomes:

1. **Bounded model surface:** a client sees three stable meta-tools, and every response is budgeted.
2. **Governed execution:** only validated, revision-pinned, authorized capabilities run, with approvals bound to exact intent.
3. **Graceful heterogeneity:** all five capability kinds share lifecycle, discovery, policy, and observability while retaining driver-specific semantics.
4. **Measurable efficiency:** task-level cost, quality, latency, routing, and context residency are measurable against an eager baseline.
5. **Safe self-hosting:** the core runs locally with open components, isolates third-party execution, and scales through replaceable interfaces.

## Open validation work

The numerical performance targets, the usefulness of top-8 compact search cards, the eviction scoring weights, and reasoning-tier thresholds are assumptions rather than discovered facts. They require benchmark and user-study evidence before being frozen as stable defaults. Security boundaries, deterministic conflict behavior, budget enforcement, and secret exclusion are not optional experiments; they are release gates.
