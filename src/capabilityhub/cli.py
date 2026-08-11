"""Standard-library command line interface for local CapabilityHub operations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from threading import Event

from capabilityhub import runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capabilityhub")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate JSON manifests")
    validate.add_argument("paths", nargs="+")
    discover = commands.add_parser("discover-skills", help="discover local SKILL.md packages")
    discover.add_argument("directories", nargs="+")
    inventory = commands.add_parser("inventory", help="show live local capability counts")
    _project_argument(inventory)
    _pretty_argument(inventory)
    search = commands.add_parser("search", help="search compact local capability metadata")
    search.add_argument("query")
    search.add_argument("--kind", action="append", choices=("skill", "mcp", "cli", "api", "rag"))
    search.add_argument("--limit", type=_search_limit, default=8)
    _project_argument(search)
    _pretty_argument(search)
    health = commands.add_parser("health", help="check local wiring without loading the catalog")
    _project_argument(health, must_exist=False)
    _pretty_argument(health)
    connections = commands.add_parser(
        "connections", help="show configured capability connection state without probing"
    )
    _project_argument(connections)
    _pretty_argument(connections)
    dashboard = commands.add_parser("dashboard", help="start a local read-only dashboard")
    dashboard.add_argument("--port", type=int, default=0)
    _project_argument(dashboard)
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
    if args.command == "inventory":
        _print_json(runtime.local_inventory(args.project_root), pretty=args.pretty)
        return 0
    if args.command == "search":
        _print_json(
            runtime.local_search(
                args.query,
                kinds=args.kind,
                limit=args.limit,
                project_root=args.project_root,
            ),
            pretty=args.pretty,
        )
        return 0
    if args.command == "health":
        _print_json(runtime.local_health(args.project_root), pretty=args.pretty)
        return 0
    if args.command == "connections":
        _print_json(runtime.local_connections(args.project_root), pretty=args.pretty)
        return 0
    if args.command == "dashboard":
        server = runtime.local_dashboard(
            args.project_root,
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


def _project_argument(
    parser: argparse.ArgumentParser, *, must_exist: bool = True
) -> None:
    if must_exist:
        parser.add_argument(
            "--project-root",
            type=_existing_directory,
            help="discover project manifests and Skills from this explicit directory",
        )
    else:
        parser.add_argument(
            "--project-root",
            help="discover project manifests and Skills from this explicit directory",
        )


def _pretty_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")


def _print_json(payload: object, *, pretty: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
        )
    )


def _search_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 50:
        raise argparse.ArgumentTypeError("must be between 1 and 50")
    return parsed


def _existing_directory(value: str) -> str:
    if not Path(value).is_dir():
        raise argparse.ArgumentTypeError("must be an existing directory")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
