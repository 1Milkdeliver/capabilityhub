"""Run a local, read-only CapabilityHub dashboard with a deliberately safe snapshot."""

from __future__ import annotations

from capabilityhub.webui import DashboardServer


def snapshot() -> dict[str, object]:
    return {
        "active_capabilities": 1,
        "budget_remaining": {"portable_tokens": 2_000},
        "estimated_savings": "Not measured by this illustrative dashboard.",
        "loaded_capabilities": 0,
        "providers": ["application-owned-provider"],
        "reasoning_tier": "unconfigured",
    }


def main() -> None:
    with DashboardServer(snapshot) as dashboard:
        print(f"Read-only dashboard: {dashboard.url}")
        print("Press Ctrl+C to stop.")
        try:
            while True:
                input()
        except (EOFError, KeyboardInterrupt):
            return


if __name__ == "__main__":
    main()
