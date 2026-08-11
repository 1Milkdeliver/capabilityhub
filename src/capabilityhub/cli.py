"""Standard-library command line interface for local CapabilityHub operations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from threading import Event

from capabilityhub import runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capabilityhub")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate JSON manifests")
    validate.add_argument("paths", nargs="+")
    discover = commands.add_parser("discover-skills", help="discover local SKILL.md packages")
    discover.add_argument("directories", nargs="+")
    dashboard = commands.add_parser("dashboard", help="start a local read-only dashboard")
    dashboard.add_argument("--port", type=int, default=0)
    mcp = commands.add_parser("mcp-serve", help="start the optional MCP server")
    mcp.add_argument(
        "--project-root",
        help="discover project manifests and Skills from this explicit directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        print(json.dumps({"valid": runtime.validate(args.paths)}))
        return 0
    if args.command == "discover-skills":
        manifests = runtime.discover_skills(args.directories)
        print(
            json.dumps(
                {"count": len(manifests), "skills": [item.identity.revision for item in manifests]}
            )
        )
        return 0
    if args.command == "dashboard":
        server = runtime.dashboard(
            lambda: {"providers": [], "active_capabilities": [], "loaded_capabilities": []},
            port=args.port,
        )
        print(server.url)
        try:
            Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            server.close()
        return 0
    runtime.mcp_serve(args.project_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
