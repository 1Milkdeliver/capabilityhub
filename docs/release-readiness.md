# Release readiness

## Current decision

CapabilityHub `0.1.0` is ready for the certified self-hosted reference scope. The
authoritative [completion matrix](completion-matrix.md) is 36/36 Implemented. GitHub
Release Certification reruns and signs fresh, same-revision evidence; missing, stale,
skipped, partial, mixed-revision, or failed evidence is rejected.

This is a production claim for the documented self-hosted profile, not a claim that
CapabilityHub is a hosted multi-region service, a universal Sigstore implementation,
or a replacement for an external identity provider, vault, gateway, or vector database.

## Certified evidence

| Area | Release evidence and boundary |
|---|---|
| Five capability kinds | Skill discovery/load plus configured CLI, fixed-origin API, ACL-scoped indexed RAG, and MCP stdio providers share search/load/execute admission. Skill scripts are never executed. |
| Progressive disclosure | Real Codex `gpt-5.4-mini` low-effort evidence covers 30 paired tasks. Lazy mode passed quality non-inferiority and used 40.65% of eager estimated cost with zero tool calls. |
| Search and scale | Hard token/total-byte/per-card limits, authorized pre-score filtering, correct top-3 on 10k metadata, 100 concurrent service execution, and a production `DiskRagIndex` replay with one million chunks. |
| Identity and policy | Immutable revisions, signed scoped references, default-deny principal grants, dependency intersections, exact-intent approvals, scoped idempotency, hierarchical budgets, and tenant/session HMAC partitioning. |
| Data/admin separation | Loopback and optional TLS 1.2+ mTLS listeners expose separate data and role-scoped admin paths. Credentials are not interchangeable. Dashboard management uses the same authenticated dispatcher. |
| Lifecycle and supply chain | Validated install, stage/health/activate/rollback, draining pins, worker cancellation, Ed25519 certificate chain, artifact signature, transparency inclusion/checkpoint, freshness, revocation, replay, and fork checks. |
| Worker boundary | Spawned workers use bounded JSON IPC, deadline, CPU/RAM and descendant termination. Ubuntu 24.04 must prove Landlock allow-root filesystem confinement and libseccomp deny-network inheritance. Unsupported filesystem/network confinement fails closed. |
| Secrets and audit | Windows DPAPI, macOS Keychain, and Linux Secret Service store aliases; scoped single-use worker envelopes avoid plaintext persistence. Chained redacted audit, bounded observability, retention, and verified export are available. |
| RAG security | Tenant HMAC partition, principal ACL, filters, digest dedupe, freshness/retention, byte/token/deadline bounds, relative citations, and ACL-rechecked expansion handles. |
| UI and plugin | `/helpme` and `/myskills` remain outside ordinary task context. Real Chrome covers language, search, lifecycle, approval, Context removal state, back/home, responsive layout, accessibility, console, and network errors. |
| Packaging | Python 3.11/3.12/3.13, Windows and Linux wheel smoke, base-install lazy imports, official MCP SDK path, plugin validation, fresh install and cache-busted upgrade. |
| Release certification | Full tests, Ruff, mypy, documentation traceability, 36/36 matrix, browser, wheel, 10k/1m scale, adversarial providers, model usage, Linux/Windows isolation, and supply bundle are aggregated into a signed manifest for one source revision. |

## Deployment boundaries

- Keep the Dashboard on loopback. Use the split mTLS listeners for remote reference
  deployment and bind certificates to explicit tenant/principal/role identities.
- Enable only providers whose normalized grants, approval policy, supply-chain policy,
  budgets, and confinement requirements are configured. Unknown state fails closed.
- Linux is the certified filesystem/network-confined worker platform. Windows provides
  Job Object CPU/RAM/process-tree control and rejects a production request that requires
  unavailable filesystem/network confinement.
- Platform key stores, certificate issuance, OS patching, backup, external identity
  lifecycle, and distributed coordination remain operator responsibilities.
- `SKILL.md` is loadable instruction content. CapabilityHub intentionally does not run
  arbitrary discovered Skill scripts.

## Reproduce the release gates

From the repository root after installing development dependencies:

```bash
python -m pytest
python -m ruff check src tests benchmarks
python -m mypy
python scripts/docs_traceability.py
python -m benchmarks.release_gate
python -m benchmarks.adversarial_gate
python -c "from benchmarks.codex_live_eval import validate_live_artifact; validate_live_artifact('benchmarks/artifacts/codex-live-eval.json')"
```

The GitHub `Release Certification` workflow additionally runs the real Chrome gate,
Ubuntu Landlock/libseccomp attack fixture, Windows boundary gate, clean wheel smoke,
one-million-chunk replay, and signed evidence aggregation.
