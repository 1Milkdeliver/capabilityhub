# CapabilityHub

CapabilityHub is a pre-alpha Python control core for progressively disclosing agent capabilities. It has one manifest model for five capability kinds:

- Skills (`SKILL.md` content)
- MCP-described capabilities
- local CLI capabilities
- HTTP/API capabilities
- RAG/retrieval capabilities

The implemented core keeps immutable revisions, activates one revision per coordinate, discovers active records with deterministic lexical search, issues scope-bound references, loads requested sections, applies reference policy, tracks budgets, and records compact audit events. Provider execution is mediated by the service; the included static provider is deterministic and side-effect free for local fixtures and tests.

## Status and non-goals

This is `0.1.0a0`, not a production release. The public surface includes Python APIs,
a small local CLI, and an experimental MCP server adapter; none is a stable protocol
compatibility guarantee. MCP framing and transports come from the official Python SDK.

CapabilityHub does not execute discovered Skills. The bundled Skill provider reads `SKILL.md` only and treats it as loadable content. Explicit project manifests can opt into bounded CLI-process, fixed-origin HTTP, ACL-scoped indexed RAG, and MCP stdio adapters. Separate authenticated data/admin planes now have loopback and optional mTLS reference transports; scoped grant/state/budget controls, OS-backed local secret stores, spawned-worker CPU/memory limits, and process-tree cancellation also exist. Filesystem/network confinement and a hardened production profile remain future work. See [release readiness](docs/release-readiness.md) before considering any deployment.

## Install from source

Requires Python 3.11+.

```bash
python -m venv .venv
```

Activate the environment using your platform's normal command, then install the editable package and development tools:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the test suite:

```bash
python -m pytest
```

If you run directly from an uninstalled checkout, include `src` on `PYTHONPATH` (PowerShell: `$env:PYTHONPATH = "src"`).

## First local check

The following validates an API manifest and activates its immutable revision. It makes no network or provider call:

```bash
python examples/validate_manifest.py
```

The fixture is in [examples/manifest-api.json](examples/manifest-api.json). It demonstrates the JSON-only `capabilityhub.io/v1alpha1` parser used by the current core.

## Python service flow

`CapabilityHubService` is transport-neutral. An embedding application constructs a registry, an approved provider set, a `ReferenceSigner`, an audit sink, and a `BudgetLedger`; it then calls:

1. `search(query, ...)` to receive compact `SearchCard` objects and revision-bound load references;
2. `load(capability_ref, ...)` to choose sections and operations; and
3. `execute(request, ...)` only for non-Skill capability kinds with an execution reference issued by `load`.

Search and load are not permission grants. Search cards are filtered against the caller's granted permissions before disclosure. Skill content is load-only, and execution authorization is checked by the reference policy. Approval-required operations accept only short-lived approval references bound to the exact capability revision, operation, normalized arguments, actor scope, and task. Inline input and output contracts are validated with JSON Schema. The unit tests under `tests/test_service.py` are the most complete executable example of this flow today.

Embedders can additionally attach a `ParameterAuthorizer` to the caller context. The same deny-by-default decision then filters search and execution, intersects dependency privileges, and constrains normalized filesystem roots, hosts, HTTP methods, commands, profiles, and secret aliases. Raw secret-bearing argument fields are rejected and authorization results contain only stable reason codes.

The transport-neutral protocol module defines one request/response/error envelope and feature handshake for library, CLI, MCP, and HTTP adapters, including explicit streaming and cancellation negotiation. The optional mTLS reference transport maps client certificates to tenant/principal identities and keeps data and role-scoped admin listeners separate; it is still a reference deployment boundary, not a production-service claim.

## CLI and MCP

The source install exposes twenty-nine local commands:

```bash
capabilityhub validate examples/manifest-api.json
capabilityhub export-manifest examples/manifest-api.json --pretty
capabilityhub import-openapi /path/to/openapi.json --operation-id listPets --allow-host api.example.com --name pets --pretty
capabilityhub migrate-manifest /path/to/legacy.json --pretty
capabilityhub compatibility --required-feature security.example --pretty
capabilityhub activation-lock export --pretty
capabilityhub discover-skills /path/to/approved/skills
capabilityhub inventory --pretty
capabilityhub search "work with PDF files" --kind skill --limit 5 --pretty
capabilityhub health --pretty
capabilityhub connections --pretty
capabilityhub connections --probe --pretty
capabilityhub loaded --limit 20 --pretty
capabilityhub providers --pretty
capabilityhub routing "work with PDF files" --kind skill --pretty
capabilityhub language show --pretty
capabilityhub lifecycle list --pretty
capabilityhub updates list --pretty
capabilityhub audit --limit 50 --pretty
capabilityhub secure-audit verify --pretty
capabilityhub load REVISION --section contract --pretty
capabilityhub execute REVISION read --arguments '{"id": 1}' --fixture-output '{"name": "demo"}' --idempotency-key demo-1 --pretty
capabilityhub approvals list --status pending --pretty
capabilityhub context list --pretty
capabilityhub reasoning state TASK_ID --pretty
capabilityhub budget-report --pretty
capabilityhub benchmark
capabilityhub benchmark --scale
capabilityhub dashboard --project-root /absolute/project/path
capabilityhub http-serve --project-root /absolute/project/path
capabilityhub mcp-serve
```

Project manifests can opt into real, bounded CLI, HTTP API, local RAG, and MCP stdio adapters. See
[project provider configuration](docs/provider-configuration.md). Discovery remains inert unless a
supported driver is explicitly configured; `execute` uses that provider by default, while
`--fixture-output` is reserved for deterministic tests.

`load` exercises the real reference, permission, section, disclosure-budget, and resident-context path. Its response includes bounded dependency/conflict notices, total omission counts, and at most four opaque signed handles for exact section/operation rehydration; conflict values are digested and large manifests cannot expand the notice/handle lists without bound. `execute` uses an explicitly configured project Provider by default and runs it through a supervised spawned worker; `--fixture-output` remains a deterministic test path. Write-like operations require an idempotency key. Approval-required configured operations use the durable `approvals request` → `approve`/`deny` → `execute --approval-id` flow; the `--approved` shortcut is fixture-only.

Project manifests can opt into the CLI process, fixed-origin HTTP API, local RAG, and MCP stdio adapters. The process supervisor enforces wall-clock termination, bounded JSON IPC, process-tree cleanup, Windows Job Object or POSIX CPU/memory limits, and fail-closed capability reporting. Filesystem and network confinement still require a supported sandbox backend.

The supply-chain module verifies artifact bytes against the manifest digest and an explicit publisher/registry policy. Stage, health recording, and activation each re-acquire and reverify bytes. HMAC-SHA256 remains explicitly local shared-key evidence. The optional `supply-chain` extra adds Ed25519 verification with pinned publisher/registry and optional issuer/subject/transparency policy; it does not claim X.509-chain or online Rekor-proof verification.

Manifests may be JSON, `.yaml`, or `.yml`. YAML intake uses `safe_load` only after enforcing byte, node, and depth limits and rejecting aliases, custom tags, and multiple documents. `activation-lock export` captures exact active revisions plus dependency closure without loading providers; `activation-lock verify FILE` fails closed on missing, extra, or drifted capabilities.

`import-openapi` is an offline preview: it reads one local OpenAPI 3 JSON/YAML file, projects only explicitly selected operation IDs from one allowlisted fixed origin, and emits an inert API manifest. It rejects remote references, callbacks, webhooks, server overrides, embedded credentials, and security bindings; it never fetches a URL or writes an activation file.

`ScopedSecretBroker` issues short-lived, scope-bound, use-limited handles for trusted local provider callbacks. Worker envelopes carry aliases rather than plaintext; Windows DPAPI, macOS Keychain, and Linux Secret Service stores are selected strictly and unsafe or unavailable backends fail closed. `ResilientProviderExecutor` adds bounded circuit breaking and retries only when a typed error is retryable, the operation is safe or idempotent, and the adapter classifies the failure as not applied; uncertain failures are never retried.

Registry admission applies automatic projection analysis over hashed routes and roots before activation/search visibility. `SqliteScopedState` partitions generic state by an HMAC of tenant, principal, session, and task; approvals, idempotency and authenticated audit queries use this boundary. `DegradedModePolicy` permits degradation only for an explicit bounded safe fallback.

`SQLiteHierarchicalBudgetStore` supplies restart-safe parent/child reservations whose admission is atomic across every ancestor and whose raw tenant/task scope never lands in SQLite. The loopback HTTP runtime uses it for tenant→principal→session→task accounting while preserving the existing CLI ledger. `InMemoryObservability` and `SqliteMetricStore` provide bounded, low-cardinality spans and aggregate metrics with hashed correlation domains; the shared service adapter can attach them without accepting arguments, outputs, URLs, paths, secrets, or raw identities as telemetry fields. Observability remains opt-in and is not an external telemetry exporter.

`LoopbackHttpControl` is the local authenticated data plane and exposes only `POST /protocol` with search/load/execute. `LoopbackAdminControl` is a separate `POST /admin` plane with distinct short-lived single-use credentials and minimum lifecycle/update/approval/policy/audit roles. `RemoteTlsControl` provides an optional TLS 1.2+ mutual-TLS reference profile with separate listeners, certificate-bound tenant/principal/audience/role mapping, and non-interchangeable data/admin credentials. See [remote deployment](docs/remote-deployment.md) for its limits.

Local CLI and Dashboard management actions use the same authenticated admin request envelope and dispatcher as the loopback plane. Approval decisions are bound to the authenticated approver principal. Tenant, principal, session, and task scopes partition grants, approvals, idempotency, audit, budgets, reasoning, RAG, and context-residency state through opaque HMAC keys. The residency controller can evict CapabilityHub metadata and report that decision; it cannot remove content already held by a model client.

`LiveDependencyObserver` resamples policy revision and Provider circuit state at decision time and fails closed on unavailable or malformed evidence. The checked-in [production reference profile](docs/production-reference-profile.md) and credential-free adversarial gate make these boundaries reproducible, but they are evidence artifacts rather than a production certification.

`connections` remains configuration-only by default. `connections --probe` is an explicit bounded diagnostic for configured MCP HTTP(S) endpoints: it performs DNS, TCP, and TLS setup only, rejects private/link-local/reserved and mixed-DNS targets unless loopback is explicitly allowed, and never sends an HTTP request or invokes a capability. A successful result means transport reachability (and, for HTTPS, verified TLS); application authentication and health remain unknown.

`capabilityhub http-serve` connects that transport to a real immutable `CapabilityHubService` catalog snapshot and prints its URL and one-process bearer token once to the launching terminal. Restart it to pick up catalog changes. The same strict service adapter is tested for library, CLI, MCP, and HTTP envelope kinds; this does not turn the endpoint into a remote multi-user deployment. `DrainedCapabilityHubService` is available to embedders that need the drain controller around actual execution admission and provider calls.

`HttpApiProvider` is the opt-in real JSON API adapter. Each operation is tied to one configured HTTPS origin (cleartext is loopback-only), an allowlisted HTTP method and path template, named query/body fields, and an optional out-of-band header supplier. Path values are percent encoded, redirects are rejected, credentials are forbidden in base URLs, response reads are hard bounded before parsing, and errors expose only safe status metadata. It deliberately does not provide a generic URL-fetch capability.

`LocalRagProvider` provides real read-only retrieval over explicitly approved local `.md`/`.txt` roots. It reads bounded files only at execution time, rejects path escapes, ranks compact line chunks deterministically, emits relative-path and line-range citations, enforces `top_k`, deadline, and output budgets, and never returns an entire index or hidden absolute path. It is a small local reference adapter, not a vector database or production managed-RAG replacement.

Install the `mcp` extra to use `capabilityhub.providers.mcp.McpStdioProvider`, the real upstream MCP adapter. It delegates framing, initialization, tool discovery, calls, cancellation, and stdio process management to the official MCP Python SDK. The CapabilityHub MCP server's exact three public tools now dispatch through the same protocol envelope and strict service adapter used by the library and loopback HTTP path. Each upstream operation maps to one explicitly configured tool on an absolute command with fixed args and an explicit environment; stderr is suppressed from model-visible output, unadvertised tools are denied, the whole session has a deadline, and returned structured content is JSON-checked and budgeted. Persistent sessions, OAuth, HTTP transport, and production gateway pooling remain upstream responsibilities.

Menu language and activation overrides now persist without a model call. Use `capabilityhub language set zh-CN --scope project` (or `en`/`auto`) and `capabilityhub lifecycle set NAMESPACE/NAME disabled --scope project`. Lifecycle supports `enabled`, `disabled`, and `quarantined`; it changes only whether a discovered capability is active in the local catalog and never deletes, updates, or executes its files. Project settings override global settings, JSON writes are atomic, and unrelated configuration keys are preserved.

Local service operations append compact events to `.capabilityhub/audit.jsonl` with a synchronized, flushed write. `capabilityhub audit` returns a bounded redacted tail: task IDs are hashed, arguments and credentials are omitted, and incomplete/corrupt tail records are ignored. The Dashboard shows the latest ten safe project events without placing them in chat context.

`SqliteIdempotencyStore` adds atomic cross-process execution-key admission. On restart, abandoned `in_progress` records become `uncertain`, so unknown side effects are never retried automatically. The local fixture execute path enables this store by default. Provider results are **not** persisted by default: a completed duplicate is blocked with `idempotency_result_unavailable`; embedders may explicitly opt into result persistence only after reviewing output sensitivity and storage controls.

`mcp-serve` exposes exactly `capability.search`, `capability.load`, and
`capability.execute` through the official MCP Python SDK. Its zero-configuration CLI
mode builds a read-only snapshot from approved Codex/Agents Skill roots,
enabled plugin Skill roots, configured MCP server names, and project-local
`.capabilityhub/manifests` files. It also reports the CapabilityHub CLI shipped with the
running package. Discovery never executes capability code or exposes
MCP commands, URLs, or credentials. The default service has no execution providers,
uses temporary references, and enforces bounded per-task budgets. Embedders can
construct a configured service and call `create_mcp_server(...)`; production provider
wiring and persistent configuration are not part of this pre-alpha release.

The plugin declares the portable `capabilityhub mcp-serve` console command. Install the
package with its MCP extra before installing the plugin:

```bash
python -m pip install '.[mcp]'
```

No separate `codex mcp add` step is needed. If `capabilityhub` is not on the Codex process
PATH, the static menus still work and live commands report unavailable. Open a new Codex
task after install or update. Before each search, the runtime checks a
lightweight filesystem fingerprint and atomically refreshes only when inputs changed;
same-task Skill and manifest changes therefore receive a new inventory generation. A
250 ms coalescing window lets burst requests share one fingerprint scan; it never loads
Skill bodies and bounds normal change visibility to roughly a quarter second.
For a fixed project catalog, append `--project-root /absolute/project/path` to the MCP
command instead of relying on the server process working directory.

## Local dashboard

After installing the repository's Codex plugin, enter `/helpme` in a new Codex task to
open the compact, progressive CapabilityHub menu. Use `/helpme language` to select
Simplified Chinese, English, automatic detection, preview, and task/project/global
preference scope. Stable menu text comes from static catalogs and does not spend model
tokens on repeated translation or preload the capability catalog.

Enter `/myskills` to open the direct Skill menu. It supports numbered choices, exact
commands, and explicit natural-language requests while leaving Codex's native `/skills`,
`/status`, `/mcp`, and other slash commands untouched. Inventory, Providers, Routing,
Lifecycle, risks, and conflicts remain visible with plain-language explanations.
`/helpme language` opens language settings, `/helpme back` returns to the previous menu,
and `/helpme home` returns to the CapabilityHub main menu. Every child menu and result
keeps these navigation choices visible; `/myskills back` returns to the My Skills menu.

`DashboardServer` is a small, standard-library local management server. It binds to `127.0.0.1` by default and serves bundled assets, safe status, bounded metadata search, project language settings, and activation lifecycle controls. Mutations require a per-process CSRF token and same-origin requests. Start the live local view:

```bash
capabilityhub dashboard --project-root /absolute/project/path
```

The page refreshes every three seconds and shows five-kind active counts, generation,
safe exclusion counts, local wiring checks, configuration-only connection state, compact search,
language, and enable/disable/quarantine controls. It performs no provider network probes and does
not show credentials, commands, URLs, full manifests, Skill bodies, provider output, or absolute
preference paths. Lifecycle actions only change project catalog activation; they never delete or
execute files. `examples/dashboard.py` remains a
small injected-snapshot fixture for embedders. See [the dashboard note](docs/ui-plugin.md)
for the same boundary.

## Deterministic disclosure benchmark

The benchmark is local, fixture-based, and has no model, network, or paid-service calls:

```bash
capabilityhub benchmark
```

`capabilityhub benchmark --scale` covers 10,000 metadata capabilities and 100 concurrent reads. The disk-backed million-chunk FTS benchmark reuses the production `DiskRagIndex`; tenant/ACL correctness is verified separately. This still proves neither model quality nor external-provider latency.

The pinned [reference run](benchmarks/reference-run.json) uses 100 definitions across all five kinds. Ten natural-language tasks are routed by the actual deterministic lexical search; expected revisions are used only for scoring. The current fixture records 10/10 selection, five expected failure paths, and 40 cold/warm/invalidation events with zero stale use. This is deterministic fixture evidence, not model reasoning quality, real provider latency, hidden reasoning tokens, or production monetary cost. Read [benchmarks/README.md](benchmarks/README.md) and [docs/validation-plan.md](docs/validation-plan.md) before making a performance claim.

## Contributing and security

The live [36-requirement completion matrix](docs/completion-matrix.md) separates implemented paths from
partial or open production gates. It is the authoritative scope audit; passing the structural benchmark
alone is not a completion claim.

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md). The project is MIT licensed; integration notices are tracked in [THIRD_PARTY.md](THIRD_PARTY.md).
