"""skt command-line entry point.

Deliberately stdlib-only: the skill-script installer runs this file with
the system python3 (no venv), so nothing here may import beyond the
standard library until the install path grows a venv.
"""

from __future__ import annotations

import argparse
import sys

__version__ = "0.1.0"

# Subcommand -> (implementing ticket, issue URL) for honest stubs.
_PENDING = {
    "status": ("SKT-3", "haydenrear/skill-manager-integration-repository#69"),
    "check": ("SKT-4", "haydenrear/skill-manager-integration-repository#70"),
    "sync": ("SKT-4", "haydenrear/skill-manager-integration-repository#70"),
    "ticket": ("SKT-5", "haydenrear/skill-manager-integration-repository#71"),
    "publish": ("SKT-5", "haydenrear/skill-manager-integration-repository#71"),
}

NOT_IMPLEMENTED_EXIT = 2


def _stub(name: str) -> int:
    ticket, issue = _PENDING[name]
    print(
        f"skt {name}: not implemented yet — lands with {ticket} ({issue})",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED_EXIT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skt",
        description=(
            "Skill-lifecycle CLI: startup disclosure of loaded skills/plugins, "
            "home tier and epic/ticket state; new-version and sync-with-root "
            "notifications; worktree change management."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"skt {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser(
        "status",
        help="startup report: units, plugins, home tier, epic/ticket context (SKT-3)",
    )
    status.add_argument("--json", action="store_true", help="machine-readable output")

    check = sub.add_parser(
        "check",
        help="new-version and sync-with-root notifications (SKT-4)",
    )
    check.add_argument("--cached", action="store_true", help="throttled, no-network path")
    check.add_argument("--json", action="store_true")

    sync = sub.add_parser(
        "sync", help="pull a unit to its latest pushed source (SKT-4)"
    )
    sync.add_argument("unit", nargs="?")

    ticket = sub.add_parser(
        "ticket", help="worktree lifecycle: new/close/info via git-issue-workflow (SKT-5)"
    )
    ticket.add_argument("verb", nargs="?", choices=["new", "close", "info"])
    ticket.add_argument("ticket_id", nargs="?")

    publish = sub.add_parser(
        "publish", help="guided home-sync + unit-publish for edited skills (SKT-5)"
    )
    publish.add_argument("unit", nargs="?")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return _stub(args.command)


if __name__ == "__main__":
    sys.exit(main())
