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

Search and load are not permission grants. Skill content is load-only, and execution authorization is checked by the reference policy. The unit tests under `tests/test_service.py` are the most complete executable example of this flow today.

## CLI and MCP

The source install exposes four local commands:

```bash
capabilityhub validate examples/manifest-api.json
capabilityhub discover-skills /path/to/approved/skills
capabilityhub dashboard
capabilityhub mcp-serve
```

`mcp-serve` exposes exactly `capability.search`, `capability.load`, and
`capability.execute` through the official MCP Python SDK. Its zero-configuration CLI
mode builds a read-only startup snapshot from approved Codex/Agents Skill roots,
enabled plugin Skill roots, configured MCP server names, and project-local
`.capabilityhub/manifests` files. Discovery never executes capability code or exposes
MCP commands, URLs, or credentials. The default service has no execution providers,
uses temporary references, and enforces bounded per-task budgets. Embedders can
construct a configured service and call `create_mcp_server(...)`; production provider
wiring and persistent configuration are not part of this pre-alpha release.

Register a source checkout as a local Codex MCP server with an absolute interpreter
path so the public plugin remains portable:

```bash
codex mcp add capabilityhub-local -- /absolute/path/to/python -m capabilityhub.cli mcp-serve
```

Open a new Codex task after registration. The inventory is refreshed when the MCP
process starts; filesystem changes made during the same task require restarting that
task/runtime in this pre-alpha version.

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

`DashboardServer` is a small read-only, standard-library dashboard server. It binds to `127.0.0.1` by default and serves bundled assets plus `GET /api/status`; it does not accept mutations. Start the illustrative local snapshot:

```bash
python examples/dashboard.py
```

It intentionally shows only injected, JSON-serializable status fields. Do not include credentials, full manifests, Skill bodies, provider output, or sensitive sections in a snapshot. See [the dashboard note](docs/ui-plugin.md) for the same boundary.

## Deterministic disclosure benchmark

The benchmark is local, fixture-based, and has no model, network, or paid-service calls:

```bash
python -c "from benchmarks.harness import run_benchmark; print(run_benchmark())"
```

The pinned [reference run](benchmarks/reference-run.json) uses 100 definitions across all five kinds. It compares eager full-definition exposure with a lazy sequence of fixed meta-tools, one expected search card, and one selected definition. It proves structural disclosure/accounting properties under an **oracle-supplied target revision**. It does **not** measure semantic search accuracy, model reasoning quality, real provider latency, hidden reasoning tokens, or production monetary cost. Read [benchmarks/README.md](benchmarks/README.md) and [docs/validation-plan.md](docs/validation-plan.md) before making a performance claim.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md). The project is MIT licensed; integration notices are tracked in [THIRD_PARTY.md](THIRD_PARTY.md).
