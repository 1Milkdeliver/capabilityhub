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

CapabilityHub does not execute discovered Skills. The bundled Skill provider reads `SKILL.md` only and treats it as loadable content. Real process, network, credential, sandbox, tenant-isolation, and production RAG integrations remain future work. See [release readiness](docs/release-readiness.md) before considering any deployment.

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

## CLI and MCP

The source install exposes twelve local commands:

```bash
capabilityhub validate examples/manifest-api.json
capabilityhub discover-skills /path/to/approved/skills
capabilityhub inventory --pretty
capabilityhub search "work with PDF files" --kind skill --limit 5 --pretty
capabilityhub health --pretty
capabilityhub connections --pretty
capabilityhub load REVISION --section contract --pretty
capabilityhub execute REVISION read --arguments '{"id": 1}' --fixture-output '{"name": "demo"}' --idempotency-key demo-1 --pretty
capabilityhub budget-report --pretty
capabilityhub benchmark
capabilityhub dashboard --project-root /absolute/project/path
capabilityhub mcp-serve
```

`load` exercises the real reference, permission, section, and disclosure-budget path. The current `execute` command is deliberately limited to a deterministic, side-effect-free static fixture supplied by the operator; it is a control-core verification command, not a shell, network, MCP, API, or RAG executor. Write-like fixture operations require an idempotency key, and approval-required operations additionally require `--approved`, which asks the trusted local control path to issue an exact-intent approval reference. Production adapters remain pending.

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

Register a source checkout as a local Codex MCP server with an absolute interpreter
path so the public plugin remains portable:

```bash
codex mcp add capabilityhub-local -- /absolute/path/to/python -m capabilityhub.cli mcp-serve
```

Open a new Codex task after registration. Before each search, the runtime checks a
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

`DashboardServer` is a small read-only, standard-library dashboard server. It binds to `127.0.0.1` by default and serves bundled assets plus `GET /api/status`; it does not accept mutations. Start the live local Inventory and Health view:

```bash
capabilityhub dashboard --project-root /absolute/project/path
```

The page refreshes every three seconds and shows five-kind active counts, generation,
safe exclusion counts, local wiring checks, and configuration-only connection state.
It performs no provider network probes and does not show credentials, commands,
URLs, full manifests, Skill bodies, or provider output. `examples/dashboard.py` remains a
small injected-snapshot fixture for embedders. See [the dashboard note](docs/ui-plugin.md)
for the same boundary.

## Deterministic disclosure benchmark

The benchmark is local, fixture-based, and has no model, network, or paid-service calls:

```bash
capabilityhub benchmark
```

The pinned [reference run](benchmarks/reference-run.json) uses 100 definitions across all five kinds. It compares eager full-definition exposure with a lazy sequence of fixed meta-tools, one expected search card, and one selected definition. It proves structural disclosure/accounting properties under an **oracle-supplied target revision**. It does **not** measure semantic search accuracy, model reasoning quality, real provider latency, hidden reasoning tokens, or production monetary cost. Read [benchmarks/README.md](benchmarks/README.md) and [docs/validation-plan.md](docs/validation-plan.md) before making a performance claim.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md). The project is MIT licensed; integration notices are tracked in [THIRD_PARTY.md](THIRD_PARTY.md).
