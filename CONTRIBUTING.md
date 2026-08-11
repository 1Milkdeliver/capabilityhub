# Contributing to CapabilityHub

Thanks for helping with this pre-alpha project. Small, well-scoped changes with tests are especially useful while interfaces are still settling.

## Local setup

Use Python 3.11 or newer and install from the repository root:

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest
```

If the package has not been installed, run tests with `src` on `PYTHONPATH`. The project aims to keep the control core standard-library-first; do not add a dependency when a small, audited built-in solution is sufficient.

## Change guidelines

- Keep manifest parsing, registry, policy, budget, and provider execution boundaries explicit.
- Do not cause provider discovery to import third-party modules, run scripts, start processes, or contact a network service.
- Keep error messages safe for model-visible callers. Do not place secrets, raw provider diagnostics, prompts, or retrieved content in logs or fixtures.
- Add deterministic tests for behavior changes. Network, clock, model, and paid-service dependencies must be injected or fixture-backed.
- Preserve immutable revision semantics and scope-bound references. Do not bypass the service admission path in an adapter.
- Update user-facing documentation when an interface becomes available or is intentionally unavailable.

## Checks

Before opening a change, run the relevant tests and, where installed:

```bash
python -m pytest
python -m ruff check src tests benchmarks
python -m mypy
```

Do not edit generated benchmark reports merely to improve a result. Change the harness/fixture with a clear rationale, rerun it, and record the new report and limitations.

## Issues and pull requests

Describe the problem, expected behavior, threat or compatibility implications, and tests run. Keep pull requests focused. For a security-sensitive issue, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
