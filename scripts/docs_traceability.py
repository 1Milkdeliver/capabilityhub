"""Fail CI when release claims lose their runtime or test evidence."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IMPLEMENTED_EVIDENCE = {
    "R3": (
        ("src/capabilityhub/models.py", "class CapabilityKind"),
        ("tests/test_registry.py", "CapabilityKind"),
    ),
    "R6": (
        ("src/capabilityhub/service.py", "def rehydrate"),
        ("tests/test_progressive_rehydration.py", "rehydration"),
    ),
    "R7": (
        ("src/capabilityhub/references.py", "class ReferenceSigner"),
        ("tests/test_references.py", "tampered"),
    ),
    "R9": (
        ("src/capabilityhub/registry.py", "class CapabilityRegistry"),
        ("tests/test_registry.py", "immutable"),
    ),
    "R13": (
        ("src/capabilityhub/activation_lock.py", "def export_activation_lock"),
        ("tests/test_activation_lock.py", "dependency"),
    ),
    "R14": (
        ("src/capabilityhub/registry.py", "resolve_projections"),
        ("tests/test_projection_admission.py", "conflict"),
    ),
    "R24": (
        ("src/capabilityhub/orchestration.py", "class ReasoningOrchestrator"),
        ("tests/test_orchestration.py", "restart"),
    ),
}

REQUIRED_RUNTIME_CLAIMS = {
    "separate admin plane": (
        ("src/capabilityhub/runtime.py", "def local_admin_control"),
        ("tests/test_admin_control.py", "not_interchangeable"),
    ),
    "three data operations": (
        ("src/capabilityhub/http_control.py", "capability.execute"),
        ("tests/test_http_control.py", "capability.execute"),
    ),
    "helpme menu": (
        ("plugins/capabilityhub/skills/helpme/SKILL.md", "/helpme"),
        ("tests/test_plugin_package.py", "/helpme"),
    ),
    "myskills menu": (
        ("plugins/capabilityhub/skills/myskills/SKILL.md", "/myskills"),
        ("tests/test_plugin_package.py", "/myskills"),
    ),
}

STALE_PHRASES = {
    "authenticated control-plane credentials remain open",
    "public-key/Sigstore identity remains open",
    "not yet an automatic registry admission gate",
    "no 1m RAG",
    "authenticated tenant identity remains open",
}


def traceability_errors(root: Path = ROOT) -> tuple[str, ...]:
    errors: list[str] = []
    matrix = _read(root, "docs/completion-matrix.md")
    implemented = set(
        re.findall(r"^\| (R\d+) \|[^\n]+\| Implemented \|", matrix, flags=re.MULTILINE)
    )
    unknown = implemented - IMPLEMENTED_EVIDENCE.keys()
    if unknown:
        errors.append("Implemented rows lack traceability rules: " + ", ".join(sorted(unknown)))
    for requirement in sorted(implemented & IMPLEMENTED_EVIDENCE.keys()):
        _require_pairs(root, requirement, IMPLEMENTED_EVIDENCE[requirement], errors)
    for claim, pairs in REQUIRED_RUNTIME_CLAIMS.items():
        _require_pairs(root, claim, pairs, errors)
    release_text = "\n".join(
        _read(root, path)
        for path in (
            "README.md",
            "docs/completion-matrix.md",
            "docs/release-readiness.md",
        )
    )
    for phrase in sorted(STALE_PHRASES):
        if phrase.casefold() in release_text.casefold():
            errors.append(f"stale release wording remains: {phrase}")
    return tuple(errors)


def _require_pairs(
    root: Path,
    label: str,
    pairs: tuple[tuple[str, str], ...],
    errors: list[str],
) -> None:
    for path, marker in pairs:
        target = root / path
        if not target.is_file() or marker not in target.read_text(encoding="utf-8"):
            errors.append(f"{label}: missing {path} marker {marker!r}")


def _read(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


def main() -> int:
    errors = traceability_errors()
    if errors:
        print("\n".join(errors))
        return 1
    print("documentation traceability passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
