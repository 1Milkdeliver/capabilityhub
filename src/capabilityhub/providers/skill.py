"""Safe, filesystem-only discovery of portable ``SKILL.md`` packages."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import measure_text
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
    OperationSpec,
    OperationType,
    SectionDescriptor,
)
from capabilityhub.providers.base import ProviderContext

_SAFE_FRONTMATTER = {
    "name",
    "description",
    "version",
    "license",
    "compatibility",
    "allowed-tools",
    "tags",
}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_FRONTMATTER_LINES = 256


class SkillProvider:
    """Index skill packages without importing modules or executing any scripts.

    Only the ``SKILL.md`` bytes are read. Adjacent resources and scripts remain
    untrusted files and are never opened by this provider.
    """

    def __init__(
        self,
        directories: tuple[str | Path, ...] | list[str | Path],
        *,
        namespace: str = "skills",
        name: str = "skill",
        max_file_bytes: int = 262_144,
    ) -> None:
        if not directories:
            raise ValueError("at least one skill directory is required")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self._roots = tuple(_resolve_root(Path(directory)) for directory in directories)
        self._namespace = namespace
        self._name = name
        self._max_file_bytes = max_file_bytes

    @property
    def name(self) -> str:
        return self._name

    def discover(self) -> tuple[CapabilityManifest, ...]:
        manifests: list[CapabilityManifest] = []
        seen_coordinates: set[str] = set()
        for root in self._roots:
            for candidate in sorted(root.rglob("SKILL.md"), key=lambda path: path.as_posix()):
                path = candidate.resolve(strict=True)
                if not path.is_relative_to(root):
                    raise ValueError("skill path escapes its configured directory")
                manifest = self._manifest_from_file(root, path)
                if manifest.identity.coordinate in seen_coordinates:
                    raise ValueError(f"duplicate skill coordinate: {manifest.identity.coordinate}")
                seen_coordinates.add(manifest.identity.coordinate)
                manifests.append(manifest)
        return tuple(manifests)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        del identity, request, context
        raise CapabilityHubError(
            code="skill_execution_not_supported",
            category=ErrorCategory.POLICY,
            safe_message=(
                "Discovered skills are content-only and cannot be executed by this provider."
            ),
        )

    def _manifest_from_file(self, root: Path, path: Path) -> CapabilityManifest:
        try:
            size = path.stat().st_size
        except OSError as error:
            raise ValueError("skill file cannot be inspected") from error
        if size > self._max_file_bytes:
            raise ValueError("skill file exceeds the configured size limit")
        try:
            raw = path.read_bytes()
            document = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError("skill file must be readable UTF-8") from error
        frontmatter, body = _split_frontmatter(document)
        parsed = _parse_frontmatter(frontmatter)
        skill_name = parsed.get("name") or path.parent.name
        if not isinstance(skill_name, str) or not _NAME.fullmatch(skill_name):
            raise ValueError("skill name must be a conservative identifier")
        summary = parsed.get("description")
        if not isinstance(summary, str) or not summary.strip():
            summary = f"Portable skill discovered from {path.parent.name}."
        digest = sha256(raw).hexdigest()
        relative = path.relative_to(root).as_posix()
        version = parsed.get("version")
        if not isinstance(version, str) or not version.strip():
            version = f"sha256-{digest[:12]}"
        permissions = _string_tuple(parsed.get("allowed-tools"))
        tags = _string_tuple(parsed.get("tags"))
        section = SectionDescriptor(
            name="instructions",
            media_type="text/markdown",
            content=body,
            portable_tokens=measure_text(body).portable_tokens,
        )
        metadata: dict[str, JsonValue] = {
            "content_digest": f"sha256:{digest}",
            "provenance": f"skill://{root.name}/{relative}",
            "relative_path": relative,
        }
        for key in ("license", "compatibility"):
            value = parsed.get(key)
            if isinstance(value, str):
                metadata[key] = value
        return CapabilityManifest(
            identity=CapabilityIdentity(self._namespace, skill_name, version, f"sha256:{digest}"),
            kind=CapabilityKind.SKILL,
            summary=summary.strip(),
            provider=self.name,
            operations=(OperationSpec(name="load", operation_type=OperationType.EXPAND),),
            sections=(section,),
            permissions=permissions,
            tags=tags,
            source=f"skill://{root.name}/{relative}",
            metadata=metadata,
        )


def _resolve_root(directory: Path) -> Path:
    root = directory.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("skill directory must be a directory")
    return root


def _split_frontmatter(document: str) -> tuple[str, str]:
    lines = document.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", document
    for index, line in enumerate(lines[1 : _MAX_FRONTMATTER_LINES + 1], start=1):
        if line.strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise ValueError("skill frontmatter is not closed within the allowed limit")


def _parse_frontmatter(frontmatter: str) -> dict[str, str | list[str]]:
    parsed: dict[str, str | list[str]] = {}
    active_list: str | None = None
    for raw_line in frontmatter.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith((" ", "\t")):
            item = raw_line.strip()
            if active_list is None or not item.startswith("- "):
                raise ValueError("skill frontmatter supports only simple scalar and list values")
            value = _scalar(item[2:])
            existing = parsed[active_list]
            assert isinstance(existing, list)
            existing.append(value)
            continue
        if ":" not in raw_line:
            raise ValueError("skill frontmatter line is invalid")
        key, value = raw_line.split(":", 1)
        key = key.strip()
        if key not in _SAFE_FRONTMATTER:
            active_list = None
            continue
        value = value.strip()
        if not value:
            if key not in {"allowed-tools", "tags"}:
                raise ValueError("only allowed-tools and tags may use list syntax")
            parsed[key] = []
            active_list = key
            continue
        active_list = None
        if key in {"allowed-tools", "tags"}:
            parsed[key] = _inline_list(value)
        else:
            parsed[key] = _scalar(value)
    return parsed


def _scalar(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    if not value or "\x00" in value or len(value) > 4_096:
        raise ValueError("skill frontmatter value is invalid")
    return value


def _inline_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [_scalar(item) for item in value.split(",") if item.strip()]


def _string_tuple(value: str | list[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


SkillDirectoryProvider = SkillProvider
