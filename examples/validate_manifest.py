"""Validate and activate the bundled API manifest without executing a provider."""

from __future__ import annotations

from pathlib import Path

from capabilityhub.manifest import load_manifest
from capabilityhub.registry import CapabilityRegistry


def main() -> None:
    manifest = load_manifest(Path(__file__).with_name("manifest-api.json"))
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    print(f"active: {registry.active(manifest.identity.coordinate).identity.revision}")


if __name__ == "__main__":
    main()
