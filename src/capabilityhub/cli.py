"""Standard-library command line interface for local CapSift operations."""

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
    parser = argparse.ArgumentParser(
        prog="capsift",
        description="Load only the capabilities your agent needs.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate JSON or YAML manifests")
    validate.add_argument("paths", nargs="+")
    export_manifest = commands.add_parser(
        "export-manifest", help="export one validated manifest as deterministic JSON"
    )
    export_manifest.add_argument("path")
    _pretty_argument(export_manifest)
    import_openapi = commands.add_parser(
        "import-openapi", help="preview a safe offline OpenAPI capability projection"
    )
    import_openapi.add_argument("path")
    import_openapi.add_argument("--operation-id", action="append", required=True)
    import_openapi.add_argument("--allow-host", action="append", required=True)
    import_openapi.add_argument("--namespace", default="imported")
    import_openapi.add_argument("--name", required=True)
    import_openapi.add_argument("--version", default="0.1.0")
    _pretty_argument(import_openapi)
    migrate_manifest = commands.add_parser(
        "migrate-manifest", help="preview an explicit legacy JSON manifest migration"
    )
    migrate_manifest.add_argument("path")
    _pretty_argument(migrate_manifest)
    compatibility = commands.add_parser(
        "compatibility", help="negotiate API versions and required features"
    )
    compatibility.add_argument("--api-version", action="append")
    compatibility.add_argument("--supported-feature", action="append")
    compatibility.add_argument("--required-feature", action="append")
    _pretty_argument(compatibility)
    activation_lock = commands.add_parser(
        "activation-lock", help="export or verify the exact active revision lock"
    )
    activation_lock.add_argument(
        "action", nargs="?", choices=("export", "verify"), default="export"
    )
    activation_lock.add_argument("path", nargs="?")
    _project_argument(activation_lock)
    _pretty_argument(activation_lock)
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
        "connections", help="show configured connections; probing is explicit and bounded"
    )
    connections.add_argument(
        "--probe",
        action="store_true",
        help="perform safe TCP/TLS setup only; never invoke a capability",
    )
    connections.add_argument("--probe-timeout-ms", type=_probe_timeout, default=1_000)
    connections.add_argument("--probe-concurrency", type=_probe_concurrency, default=4)
    connections.add_argument(
        "--allow-loopback-probe",
        action="store_true",
        help="allow explicitly configured loopback endpoints",
    )
    _project_argument(connections)
    _pretty_argument(connections)
    loaded = commands.add_parser("loaded", help="show recent successful capability loads")
    loaded.add_argument("--limit", type=_loaded_limit, default=20)
    _project_argument(loaded)
    _pretty_argument(loaded)
    providers = commands.add_parser("providers", help="group capabilities by real Provider name")
    _project_argument(providers)
    _pretty_argument(providers)
    routing = commands.add_parser("routing", help="explain deterministic capability selection")
    routing.add_argument("query")
    routing.add_argument("--kind", action="append", choices=("skill", "mcp", "cli", "api", "rag"))
    routing.add_argument("--limit", type=_search_limit, default=8)
    _project_argument(routing)
    _pretty_argument(routing)
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
    updates = commands.add_parser(
        "updates", help="stage, health-gate, activate, rollback, or pin revisions"
    )
    updates.add_argument(
        "action",
        nargs="?",
        choices=(
            "list",
            "stage",
            "health-pass",
            "health-fail",
            "activate",
            "rollback",
            "pin",
            "release",
        ),
        default="list",
    )
    updates.add_argument("target", nargs="?")
    updates.add_argument("--expected-active")
    updates.add_argument("--pin-id")
    updates.add_argument(
        "--artifact",
        type=_bounded_artifact_file,
        help="local artifact bytes to verify for stage, health, or activate",
    )
    updates.add_argument("--publisher")
    updates.add_argument("--artifact-registry")
    updates.add_argument(
        "--trust-mode",
        choices=("strict", "development"),
        default="strict",
        help="strict requires an injected verifier; development explicitly permits unsigned bytes",
    )
    updates.add_argument("--limit", type=_positive_int, default=100)
    _project_argument(updates)
    _pretty_argument(updates)
    app_update = commands.add_parser(
        "app-update", help="check for or safely download a verified CapSift release"
    )
    app_update.add_argument("action", nargs="?", choices=("check", "fetch"), default="check")
    app_update.add_argument(
        "--force", action="store_true", help="ignore the 24-hour local check interval"
    )
    _pretty_argument(app_update)
    audit = commands.add_parser("audit", help="show a redacted tail of project audit events")
    audit.add_argument("--limit", type=_audit_limit, default=50)
    _project_argument(audit)
    _pretty_argument(audit)
    secure_audit = commands.add_parser(
        "secure-audit", help="verify, rotate, list, or export the opt-in HMAC audit ledger"
    )
    secure_audit.add_argument(
        "action", nargs="?", choices=("verify", "list", "rotate", "export"), default="verify"
    )
    secure_audit.add_argument("--source", default="current")
    secure_audit.add_argument("--destination")
    secure_audit.add_argument("--max-segments", type=_positive_int, default=10)
    _project_argument(secure_audit)
    _pretty_argument(secure_audit)
    observability = commands.add_parser(
        "observability", help="list or export privacy-minimized aggregate metrics"
    )
    observability.add_argument("action", nargs="?", choices=("list", "export"), default="list")
    observability.add_argument("--destination")
    observability.add_argument("--limit", type=_positive_int, default=500)
    _project_argument(observability)
    _pretty_argument(observability)
    load = commands.add_parser("load", help="load selected sections from an active revision")
    load.add_argument("revision")
    load.add_argument("--section", action="append")
    load.add_argument("--operation", action="append")
    load.add_argument("--grant", action="append")
    load.add_argument("--max-output-tokens", type=_positive_int, default=2_000)
    _project_argument(load)
    _pretty_argument(load)
    execute = commands.add_parser(
        "execute", help="execute a configured provider through policy and budget gates"
    )
    execute.add_argument("revision")
    execute.add_argument("operation")
    execute.add_argument("--arguments", type=_json_object, default={})
    execute.add_argument("--fixture-output", type=_json_value)
    execute.add_argument("--grant", action="append")
    execute.add_argument("--approved", action="store_true", help="fixture-only approval shortcut")
    execute.add_argument("--approval-id", help="consume one approved exact-intent request")
    execute.add_argument("--allow-irreversible", action="store_true")
    execute.add_argument("--idempotency-key")
    execute.add_argument("--max-output-tokens", type=_positive_int, default=2_000)
    _project_argument(execute)
    _pretty_argument(execute)
    approvals = commands.add_parser("approvals", help="request, review, or decide local approvals")
    approvals.add_argument(
        "action", nargs="?", choices=("list", "request", "approve", "deny"), default="list"
    )
    approvals.add_argument(
        "target", nargs="?", help="revision for request, approval ID for decisions"
    )
    approvals.add_argument("operation", nargs="?")
    approvals.add_argument("--arguments", type=_json_object, default={})
    approvals.add_argument("--ttl-seconds", type=_positive_int, default=300)
    approvals.add_argument(
        "--status", choices=("pending", "approved", "denied", "consumed", "expired")
    )
    approvals.add_argument("--limit", type=_audit_limit, default=50)
    _project_argument(approvals)
    _pretty_argument(approvals)
    context = commands.add_parser("context", help="show or manage resident context metadata")
    context.add_argument(
        "action",
        nargs="?",
        choices=(
            "list",
            "access",
            "pin",
            "unpin",
            "remove",
            "removals",
            "request-removal",
            "retry-removal",
            "ack-removal",
        ),
        default="list",
    )
    context.add_argument("key", nargs="?")
    context.add_argument("--generation", type=int)
    context.add_argument("--idempotency-key")
    context.add_argument("--acknowledgement-id")
    context.add_argument("--removed", choices=("yes", "no"))
    _project_argument(context)
    _pretty_argument(context)
    reasoning = commands.add_parser("reasoning", help="get budget-aware reasoning tier advice")
    reasoning.add_argument("action", choices=("state", "recommend", "reset"))
    reasoning.add_argument("task_id")
    reasoning.add_argument("--eligible", action="append", choices=("low", "medium", "high"))
    reasoning.add_argument(
        "--risk", choices=("none", "read", "reversible_write", "irreversible"), default="none"
    )
    reasoning.add_argument("--policy-minimum", choices=("low", "medium", "high"), default="low")
    reasoning.add_argument("--escalation-reason")
    reasoning.add_argument(
        "--attempt-id", help="opaque attempt identifier; only its digest is stored"
    )
    reasoning.add_argument(
        "--evidence-id", help="opaque evidence identifier; only its digest is stored"
    )
    _project_argument(reasoning)
    _pretty_argument(reasoning)
    budget = commands.add_parser("budget-report", help="show fresh local hard budget limits")
    budget.add_argument("--bytes", type=_non_negative_int)
    budget.add_argument("--portable-tokens", type=_non_negative_int)
    budget.add_argument("--loads", type=_non_negative_int)
    budget.add_argument("--executions", type=_non_negative_int)
    budget.add_argument("--reasoning-tokens", type=_non_negative_int)
    _project_argument(budget)
    _pretty_argument(budget)
    benchmark = commands.add_parser(
        "benchmark", help="run the deterministic eager-versus-lazy release gate"
    )
    benchmark.add_argument("--no-enforce", action="store_true")
    benchmark.add_argument(
        "--scale", action="store_true", help="run the 10k metadata/100-read scale evidence"
    )
    _pretty_argument(benchmark)
    dashboard = commands.add_parser("dashboard", help="start the local management dashboard")
    dashboard.add_argument("--port", type=int, default=0)
    _project_argument(dashboard)
    http = commands.add_parser(
        "http-serve", help="start the bearer-protected loopback protocol endpoint"
    )
    http.add_argument("--port", type=int, default=0)
    http.add_argument("--grant", action="append")
    _project_argument(http)
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
    if args.command == "export-manifest":
        _print_json(runtime.local_manifest_export(args.path), pretty=args.pretty)
        return 0
    if args.command == "import-openapi":
        _print_json(
            runtime.local_openapi_import(
                args.path,
                operation_ids=args.operation_id,
                allowed_hosts=args.allow_host,
                namespace=args.namespace,
                name=args.name,
                version=args.version,
            ),
            pretty=args.pretty,
        )
        return 0
    if args.command == "migrate-manifest":
        _print_json(runtime.local_manifest_migrate(args.path), pretty=args.pretty)
        return 0
    if args.command == "compatibility":
        _print_json(
            runtime.local_compatibility(
                api_versions=args.api_version,
                supported_features=args.supported_feature,
                required_features=args.required_feature,
            ),
            pretty=args.pretty,
        )
        return 0
    if args.command == "activation-lock":
        if args.action == "verify":
            if args.path is None:
                raise _usage("activation-lock verify requires a path")
            payload = runtime.local_activation_lock_verify(
                args.path, project_root=args.project_root
            )
        else:
            if args.path is not None:
                raise _usage("activation-lock export does not accept a path")
            payload = runtime.local_activation_lock(args.project_root)
        _print_json(payload, pretty=args.pretty)
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
        _print_json(
            runtime.local_connections(
                args.project_root,
                probe=args.probe,
                probe_timeout_ms=args.probe_timeout_ms,
                probe_concurrency=args.probe_concurrency,
                allow_loopback=args.allow_loopback_probe,
            ),
            pretty=args.pretty,
        )
        return 0
    if args.command == "loaded":
        _print_json(runtime.local_loaded(args.project_root, limit=args.limit), pretty=args.pretty)
        return 0
    if args.command == "providers":
        _print_json(runtime.local_providers(args.project_root), pretty=args.pretty)
        return 0
    if args.command == "routing":
        _print_json(
            runtime.local_routing(
                args.query,
                kinds=args.kind,
                limit=args.limit,
                project_root=args.project_root,
            ),
            pretty=args.pretty,
        )
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
            payload = runtime.local_admin_dispatch(
                "lifecycle.set",
                {
                    "coordinate": args.coordinate,
                    "state": args.state,
                    "scope": args.scope,
                },
                roles=("lifecycle-operator",),
                source="admin-cli",
                project_root=args.project_root,
            )
        else:
            if args.coordinate is not None or args.state is not None:
                raise _usage("lifecycle list does not accept a coordinate or state")
            payload = runtime.local_admin_dispatch(
                "lifecycle.list",
                {},
                roles=("lifecycle-operator",),
                source="admin-cli",
                project_root=args.project_root,
            )
        _print_json(payload, pretty=args.pretty)
        return 0
    if args.command == "updates":
        trust_options_used = (
            args.artifact is not None
            or args.publisher is not None
            or args.artifact_registry is not None
            or args.trust_mode != "strict"
        )
        if args.action == "list":
            if args.target is not None or trust_options_used:
                raise _usage("updates list does not accept a target or trust options")
            payload = runtime.local_updates(
                args.project_root,
                limit=args.limit,
            )
        else:
            if args.target is None:
                raise _usage(f"updates {args.action} requires a target")
            if args.action == "pin" and args.pin_id is None:
                raise _usage("updates pin requires --pin-id")
            if args.action != "pin" and args.pin_id is not None:
                raise _usage(f"updates {args.action} does not accept --pin-id")
            action = args.action
            health_passed = None
            if action in {"health-pass", "health-fail"}:
                health_passed = action == "health-pass"
                action = "health"
            trust_action = action in {"stage", "health", "activate"}
            supplied = (args.artifact, args.publisher, args.artifact_registry)
            if trust_action and any(item is None for item in supplied):
                raise _usage(
                    f"updates {args.action} requires --artifact, --publisher, "
                    "and --artifact-registry"
                )
            if not trust_action and trust_options_used:
                raise _usage(f"updates {args.action} does not accept trust options")
            payload = runtime.local_update_action(
                action,
                args.target,
                expected_active_revision=args.expected_active,
                health_passed=health_passed,
                pin_id=args.pin_id,
                project_root=args.project_root,
                artifact=(None if args.artifact is None else _read_artifact(args.artifact)),
                publisher=args.publisher,
                artifact_registry=args.artifact_registry,
                trust_mode=args.trust_mode,
            )
        _print_json(payload, pretty=args.pretty)
        return 0
    if args.command == "app-update":
        _print_json(
            runtime.local_app_update(args.action, force=args.force),
            pretty=args.pretty,
        )
        return 0
    if args.command == "audit":
        _print_json(runtime.local_audit(args.project_root, limit=args.limit), pretty=args.pretty)
        return 0
    if args.command == "secure-audit":
        if args.action == "export" and args.destination is None:
            raise _usage("secure-audit export requires --destination")
        if args.action != "export" and (args.source != "current" or args.destination is not None):
            raise _usage(f"secure-audit {args.action} does not accept export options")
        _print_json(
            runtime.local_secure_audit(
                args.action,
                source=args.source,
                destination=args.destination,
                max_segments=args.max_segments,
                project_root=args.project_root,
            ),
            pretty=args.pretty,
        )
        return 0
    if args.command == "observability":
        if args.action == "export" and args.destination is None:
            raise _usage("observability export requires --destination")
        if args.action != "export" and args.destination is not None:
            raise _usage("observability list does not accept --destination")
        _print_json(
            runtime.local_observability(
                args.action,
                destination=args.destination,
                limit=args.limit,
                project_root=args.project_root,
            ),
            pretty=args.pretty,
        )
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
        if args.fixture_output is None:
            if args.approved:
                raise _usage("--approved is fixture-only; use an approved --approval-id")
            result = runtime.local_execute(
                args.revision,
                args.operation,
                args.arguments,
                granted_permissions=args.grant,
                approval_id=args.approval_id,
                allow_irreversible=args.allow_irreversible,
                idempotency_key=args.idempotency_key,
                max_output_tokens=args.max_output_tokens,
                project_root=args.project_root,
            )
        else:
            if args.approval_id is not None:
                raise _usage("--approval-id cannot be combined with --fixture-output")
            result = runtime.local_execute_static(
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
            )
        _print_json(
            result,
            pretty=args.pretty,
        )
        return 0
    if args.command == "approvals":
        if args.action == "list":
            if args.target is not None or args.operation is not None:
                raise _usage("approvals list does not accept a target or operation")
            approval_payload = {"task_id": "local-cli", "limit": args.limit}
            if args.status is not None:
                approval_payload["status"] = args.status
            payload = runtime.local_admin_dispatch(
                "approval.list",
                approval_payload,
                roles=("approver",),
                source="admin-cli",
                project_root=args.project_root,
            )
        elif args.action == "request":
            if args.target is None or args.operation is None:
                raise _usage("approvals request requires a revision and operation")
            if args.status is not None:
                raise _usage("approvals request does not accept --status")
            payload = runtime.local_approval_request(
                args.target,
                args.operation,
                args.arguments,
                ttl_seconds=args.ttl_seconds,
                project_root=args.project_root,
            )
        else:
            if args.target is None or args.operation is not None:
                raise _usage(f"approvals {args.action} requires one approval ID")
            if args.status is not None:
                raise _usage(f"approvals {args.action} does not accept --status")
            payload = runtime.local_admin_dispatch(
                "approval.decide",
                {
                    "task_id": "local-cli",
                    "approval_id": args.target,
                    "decision": args.action,
                },
                roles=("approver",),
                source="admin-cli",
                project_root=args.project_root,
            )
        _print_json(payload, pretty=args.pretty)
        return 0
    if args.command == "context":
        if args.action == "list":
            if args.key is not None:
                raise _usage("context list does not accept a key")
            payload = runtime.local_context(args.project_root)
        elif args.action == "removals":
            if args.key is not None:
                raise _usage("context removals does not accept a key")
            payload = runtime.local_context_removal(
                "list", project_root=args.project_root
            )
        elif args.action in {"request-removal", "retry-removal", "ack-removal"}:
            if args.key is None or args.generation is None:
                raise _usage(
                    f"context {args.action} requires a key and --generation"
                )
            removal_action = args.action.removesuffix("-removal")
            payload = runtime.local_context_removal(
                removal_action,
                args.key,
                expected_generation=args.generation,
                idempotency_key=args.idempotency_key,
                acknowledgement_id=args.acknowledgement_id,
                removed=(None if args.removed is None else args.removed == "yes"),
                project_root=args.project_root,
            )
        else:
            if args.key is None:
                raise _usage(f"context {args.action} requires an exact key")
            payload = runtime.local_context_action(
                args.action,
                args.key,
                project_root=args.project_root,
            )
        _print_json(payload, pretty=args.pretty)
        return 0
    if args.command == "reasoning":
        if args.action != "recommend" and (
            args.eligible
            or args.risk != "none"
            or args.policy_minimum != "low"
            or args.escalation_reason is not None
            or args.attempt_id is not None
            or args.evidence_id is not None
        ):
            raise _usage(f"reasoning {args.action} does not accept recommendation options")
        payload = runtime.local_reasoning(
            args.task_id,
            action=args.action,
            eligible_tiers=args.eligible,
            risk=args.risk,
            policy_minimum=args.policy_minimum,
            escalation_reason=args.escalation_reason,
            attempt_id=args.attempt_id,
            evidence_id=args.evidence_id,
            project_root=args.project_root,
        )
        _print_json(payload, pretty=args.pretty)
        return 0
    if args.command == "budget-report":
        requested = {
            key: value
            for key, value in {
                "bytes": args.bytes,
                "executions": args.executions,
                "loads": args.loads,
                "portable_tokens": args.portable_tokens,
                "reasoning_tokens": args.reasoning_tokens,
            }.items()
            if value is not None
        }
        _print_json(
            runtime.local_budget_report(requested or None, args.project_root),
            pretty=args.pretty,
        )
        return 0
    if args.command == "benchmark":
        if args.scale:
            if args.no_enforce:
                raise _usage("benchmark --scale does not accept --no-enforce")
            _print_json(runtime.local_scale_benchmark(), pretty=args.pretty)
            return 0
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
    if args.command == "http-serve":
        http_server, access = runtime.local_http_control(
            args.project_root,
            port=args.port,
            granted_permissions=args.grant,
        )
        _print_json(
            {"bearer_token": access.bearer_token, "url": access.url},
            pretty=False,
        )
        try:
            Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            http_server.close()
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


def _loaded_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return parsed


def _probe_timeout(value: str) -> int:
    parsed = int(value)
    if not 50 <= parsed <= 5_000:
        raise argparse.ArgumentTypeError("must be between 50 and 5000")
    return parsed


def _probe_concurrency(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 16:
        raise argparse.ArgumentTypeError("must be between 1 and 16")
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


def _bounded_artifact_file(value: str) -> str:
    path = Path(value)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise argparse.ArgumentTypeError("must be a readable artifact file") from error
    if not path.is_file() or size > 64 * 1024 * 1024:
        raise argparse.ArgumentTypeError("must be a file no larger than 64 MiB")
    return value


def _read_artifact(value: str) -> bytes:
    try:
        return Path(value).read_bytes()
    except OSError as error:
        raise _usage("the artifact file could not be read") from error


if __name__ == "__main__":
    raise SystemExit(main())
