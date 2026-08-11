# Security policy

CapabilityHub mediates tools that may access files, networks, credentials, and external systems. Security reports should not be filed as public issues when they could expose a working exploit or secret.

## Reporting

Use GitHub's private vulnerability reporting feature for this repository. Include the affected revision, capability/provider type, deployment assumptions, reproduction steps, and impact. Do not include real credentials or private retrieved content.

## Supported versions

The project is pre-alpha. Only the latest `main` revision is currently supported for security fixes. No deployment should expose CapabilityHub directly to untrusted networks until an explicit stable release documents that profile.

## Current release posture

The current tree ships a pre-alpha local CLI, an experimental three-tool MCP server, and a loopback dashboard around the Python control core. The dashboard offers bounded metadata search, project language and activation overrides, exact-intent approval decisions, Context metadata controls, and Reasoning state; mutations require a random per-process CSRF header and same-origin browser requests. SQLite persists local budgets, idempotency, approvals, reasoning state, and metadata-only Context residency. These interfaces are supported only for local experimentation; there is no remote API listener, OS sandbox, secret broker, authenticated tenant control plane, or production deployment profile. Explicit project manifests may opt into supervised CLI-process, fixed-origin HTTP, local RAG, and MCP stdio providers; a static fixture remains available only for deterministic tests. Dashboard responses must not contain credentials, full capability content, retrieved passages, raw provider output, commands, endpoint URLs, argument digests, or absolute configuration paths.

Security invariants below describe the intended and tested core boundary; they are not a claim that a complete production deployment profile exists today.

## Security invariants

- Third-party Skill scripts are inactive during discovery.
- Model-facing execution accepts only revision-bound registered references.
- Search disclosure and execution permissions are denied by default.
- Approval-required execution accepts only a short-lived exact-intent reference bound to the actor, task, revision, operation, and normalized arguments.
- Inline input and output schemas are validated before and after provider invocation.
- Write-like operations require an idempotency key; uncertain provider outcomes are not automatically replayed.
- The local durable idempotency store records argument digests and outcome state, not raw arguments. Result persistence is disabled by default; completed duplicates are denied instead of re-executed.
- Optional secure audit uses an environment-supplied HMAC key, verifies the full chain and signed checkpoint before append/export, and allowlists metadata. The key value, arguments, credentials, and provider output are never written into its records.
- Staged revision activation requires an explicit health result and compare-and-swap active pointer; rollback retains the prior immutable revision. This is not artifact signature verification or a sandbox health probe.
- Optional parameter authorization applies the same dependency-aware eligibility decision before search disclosure and execution, then checks normalized path, host, method, command, profile, and secret-alias constraints. It never accepts raw secret values.
- Local artifact attestations use HMAC-SHA256 only for shared-key deployments and are not a substitute for public-key publisher identity. Production policy fails closed on unsigned, expired, revoked, untrusted, or digest-mismatched artifacts.
- YAML manifests are byte-, node-, and depth-bounded and reject aliases, custom tags, multiple documents, non-string keys, and non-JSON values before entering the common manifest parser.
- Secret broker handles bind caller and operation scope, expiry, use count, and policy revision. Plaintext is delivered only to a pre-registered in-process callback and is never returned through the broker API or persisted.
- Automatic retries require a typed retryable error, an explicitly not-applied failure, and a read-only or idempotent reversible operation. Unknown, uncertain, and irreversible failures are never retried.
- Automatic projection analysis hashes HTTP routes and filesystem roots before emitting decisions. It does not return raw endpoints or absolute paths in collisions or errors.
- Generic scoped state derives HMAC partitions from tenant, principal, session, and task and stores only scope/key digests. Expiry cleanup is always limited to one explicit scope and a bounded row count.
- Dependency health treats missing, future-dated, expired, and unknown policy/provider evidence as unsafe. Degraded operation requires a named, operation-specific, age-bounded fallback.
- The HTTP control adapter accepts only numeric loopback binding/peers/Hosts, one bounded JSON POST endpoint, and a high-entropy bearer token whose digest alone remains in the server. It is not a remote TLS/authentication profile.
- Draining blocks new admissions while preserving every in-flight revision pin. Cancellation is requested only for operations declared cancellable; forced retirement requires an explicit policy and bounded reason code.
- Secrets are references resolved outside model-visible payloads.
- Provider output is untrusted and budget bounded.
- External gateways, sandboxes, and credential stores remain separate integrations.
