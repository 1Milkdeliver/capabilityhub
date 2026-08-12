# Production reference profile

This repository provides a reproducible **reference profile**, not production
certification. `examples/production-reference.json` records the minimum topology and
fail-closed choices that a production deployment must preserve.

The profile requires separate mutual-TLS data and admin listeners, current observations
for registry, index, policy and provider dependencies, and worker process-tree cleanup.
Filesystem and network isolation are `required-or-fail-closed`: an operator must select
and verify a supported platform backend before enabling third-party execution. The
example loopback bindings are safe test defaults, not a remote exposure recipe.

Validation is offline and deterministic. It needs no external service or private
credential and therefore makes no claim about SaaS availability, external identity
providers, distributed state or internet-facing hardening. A successful check proves
only that the checked profile preserves these declared structural constraints.

The adversarial gate exercises the real local service boundary for tampered and
cross-principal references, oversized provider output, and policy disconnection. It
must fail if any attack unexpectedly succeeds. Live external-provider tests belong in
an explicitly provisioned environment and must report `not run` when credentials are
absent; absence must never be recorded as success.

Run the reproducible gates with:

```bash
python -c "from capabilityhub.production_profile import load_production_profile, profile_digest; print(profile_digest(load_production_profile('examples/production-reference.json')))"
python -c "from benchmarks.adversarial_gate import run_adversarial_gate; run_adversarial_gate()"
```
