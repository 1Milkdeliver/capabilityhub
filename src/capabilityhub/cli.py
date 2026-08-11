"""Standard-library command line interface for local CapabilityHub operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from threading import Event

from capabilityhub import runtime
from capabilityhub.errors import CapabilityHubError, ErrorCategory


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
    language = commands.add_parser("language", help="show or save the static menu language")
    language.add_argument("action", nargs="?", choices=("show", "set"), default="show")
    language.add_argument("locale", nargs="?", choices=("auto", "en", "zh-CN"))
    language.add_argument("--scope", choices=("project", "global"), default="project")
    _project_argument(language)
    _pretty_argument(language)
    lifecycle = commands.add_parser(
        "lifecycle", help="list or change persistent capability activation overrides"
    )
    lifecycle.add_argument("action", nargs="?", choices=("list", "set"), default="list")
    lifecycle.add_argument("coordinate", nargs="?")
    lifecycle.add_argument("state", nargs="?", choices=("enabled", "disabled", "quarantined"))
    lifecycle.add_argument("--scope", choices=("project", "global"), default="project")
    _project_argument(lifecycle)
    _pretty_argument(lifecycle)
    audit = commands.add_parser("audit", help="show a redacted tail of project audit events")
    audit.add_argument("--limit", type=_audit_limit, default=50)
    _project_argument(audit)
    _pretty_argument(audit)
    load = commands.add_parser("load", help="load selected sections from an active revision")
    load.add_argument("revision")
    load.add_argument("--section", action="append")
    load.add_argument("--operation", action="append")
    load.add_argument("--grant", action="append")
    load.add_argument("--max-output-tokens", type=_positive_int, default=2_000)
    _project_argument(load)
    _pretty_argument(load)
    execute = commands.add_parser(
        "execute", help="execute a deterministic static fixture through policy gates"
    )
    execute.add_argument("revision")
    execute.add_argument("operation")
    execute.add_argument("--arguments", type=_json_object, default={})
    execute.add_argument("--fixture-output", type=_json_value, required=True)
    execute.add_argument("--grant", action="append")
    execute.add_argument("--approved", action="store_true")
    execute.add_argument("--allow-irreversible", action="store_true")
    execute.add_argument("--idempotency-key")
    execute.add_argument("--max-output-tokens", type=_positive_int, default=2_000)
    _project_argument(execute)
    _pretty_argument(execute)
    budget = commands.add_parser("budget-report", help="show fresh local hard budget limits")
    budget.add_argument("--bytes", type=_non_negative_int)
    budget.add_argument("--portable-tokens", type=_non_negative_int)
    budget.add_argument("--loads", type=_non_negative_int)
    budget.add_argument("--executions", type=_non_negative_int)
    _pretty_argument(budget)
    benchmark = commands.add_parser(
        "benchmark", help="run the deterministic eager-versus-lazy release gate"
    )
    benchmark.add_argument("--no-enforce", action="store_true")
    _pretty_argument(benchmark)
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
    try:
        return _main(argv)
    except CapabilityHubError as error:
        print(
            json.dumps(error.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2


def _main(argv: Sequence[str] | None = None) -> int:
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
    if args.command == "language":
        if args.action == "set":
            if args.locale is None:
                raise _usage("language set requires a locale")
            payload = runtime.local_set_locale(
                args.locale,
                scope=args.scope,
                project_root=args.project_root,
            )
        else:
            if args.locale is not None:
                raise _usage("language show does not accept a locale")
            payload = runtime.local_preferences(args.project_root)
        _print_json(payload, pretty=args.pretty)
        return 0
    if args.command == "lifecycle":
        if args.action == "set":
            if args.coordinate is None or args.state is None:
                raise _usage("lifecycle set requires a coordinate and state")
            payload = runtime.local_set_lifecycle(
                args.coordinate,
                args.state,
                scope=args.scope,
                project_root=args.project_root,
            )
        else:
            if args.coordinate is not None or args.state is not None:
                raise _usage("lifecycle list does not accept a coordinate or state")
            payload = runtime.local_lifecycle(args.project_root)
        _print_json(payload, pretty=args.pretty)
        return 0
    if args.command == "audit":
        _print_json(runtime.local_audit(args.project_root, limit=args.limit), pretty=args.pretty)
        return 0
    if args.command == "load":
        _print_json(
            runtime.local_load(
                args.revision,
                section_names=args.section,
                operation_names=args.operation,
                granted_permissions=args.grant,
                max_output_tokens=args.max_output_tokens,
                project_root=args.project_root,
            ),
            pretty=args.pretty,
        )
        return 0
    if args.command == "execute":
        _print_json(
            runtime.local_execute_static(
                args.revision,
                args.operation,
                args.arguments,
                args.fixture_output,
                granted_permissions=args.grant,
                approved=args.approved,
                allow_irreversible=args.allow_irreversible,
                idempotency_key=args.idempotency_key,
                max_output_tokens=args.max_output_tokens,
                project_root=args.project_root,
            ),
            pretty=args.pretty,
        )
        return 0
    if args.command == "budget-report":
        requested = {
            key: value
            for key, value in {
                "bytes": args.bytes,
                "executions": args.executions,
                "loads": args.loads,
                "portable_tokens": args.portable_tokens,
            }.items()
            if value is not None
        }
        _print_json(runtime.local_budget_report(requested or None), pretty=args.pretty)
        return 0
    if args.command == "benchmark":
        _print_json(
            runtime.local_benchmark(enforce_thresholds=not args.no_enforce),
            pretty=args.pretty,
        )
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


def _project_argument(parser: argparse.ArgumentParser, *, must_exist: bool = True) -> None:
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


def _audit_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 500:
        raise argparse.ArgumentTypeError("must be between 1 and 500")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _usage(message: str) -> CapabilityHubError:
    return CapabilityHubError(
        code="invalid_command_arguments",
        category=ErrorCategory.INPUT,
        safe_message=message,
    )


def _json_value(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError("must be valid JSON") from error


def _json_object(value: str) -> dict[str, object]:
    parsed = _json_value(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _existing_directory(value: str) -> str:
    if not Path(value).is_dir():
        raise argparse.ArgumentTypeError("must be an existing directory")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
