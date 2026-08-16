"""`skt status` — the startup report. Local disk only; no network.

The artifact dimension here is READ BACK, never measured. `skt check`'s
live path is the one place that spawns the CLI, and this command runs
first in the SessionStart hook and on every orientation request — so
measuring here would put a second process spawn in front of every
session for a number the next line of the same hook is about to
produce. What it prints is the counts `skt check` recorded, with their
age attached so a reader can see how old they are.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import context as ctx_mod
from . import homes

# 2: the report gained `artifacts`, read back from the check record.
SCHEMA_VERSION = 2
MAX_TEXT_UNITS = 15
# `MAX_TEXT_UNITS`' rule applied to the new dimension: the startup report is
# injected into every session, so the artifact line names a few and counts
# the rest.
MAX_TEXT_ARTIFACTS = 4


def read_artifact_counts(home: Path) -> dict | None:
    """The `artifacts` block `skt check` last recorded, or None.

    One file read and no subprocess — the same file `check --cached`
    serves from, read the same way. Deliberately NOT gated on the record's
    TTL: an artifact count that is fifteen minutes old is still a count,
    and the age goes on the line so nobody has to assume otherwise.
    """
    try:
        raw = json.loads((home / "cache" / "skt-check.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    block = raw.get("artifacts")
    if not isinstance(block, dict):
        return None  # a record written before ARTI-10 has no artifact half
    block = dict(block)
    block["measured_at"] = raw.get("checked_at")
    block["age_seconds"] = max(0, int(time.time() - (raw.get("checked_at") or 0)))
    return block


def _age(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def collect(start: str | Path = ".") -> dict:
    home = homes.find_home(start)
    if home is None:
        return {
            "schema": SCHEMA_VERSION,
            "home": None,
            "error": "no skill-manager home found (checked $SKILL_MANAGER_HOME, "
            "ancestor .skill-manager dirs, and the operator root)",
        }
    units = homes.read_units(home)
    tctx = ctx_mod.gather(start, home)
    return {
        "schema": SCHEMA_VERSION,
        "home": str(home),
        "tier": tctx.tier,
        "artifacts": read_artifact_counts(home),
        "policy": homes.read_policy(home),
        "drift_pending": homes.drift_pending(home),
        "checkout": {
            "root": str(ctx_mod.checkout_root(start)),
            "kind": tctx.kind,
            "branch": tctx.branch,
            "ticket": tctx.ticket,
            "epic": tctx.epic,
            "on_epic_branch": tctx.on_epic_branch,
        },
        "spec_workflow": {
            "name": tctx.spec_workflow,
            "open_tickets": tctx.spec_open_tickets,
            "ticket_in_plan": tctx.spec_ticket_in_plan,
        },
        "cli_tools": homes.read_cli_tools(home),
        "worktree_sync": (
            {
                "parent_head": sync.parent_head[:8],
                "merge_base": sync.merge_base[:8],
                "in_sync": sync.in_sync,
                "ahead": sync.ahead,
                "behind": sync.behind,
            }
            if (sync := ctx_mod.worktree_sync(ctx_mod.checkout_root(start))) is not None
            else None
        ),
        "units": [
            {
                "name": u.name,
                "version": u.version,
                "kind": u.unit_kind,
                "loaded": u.loaded,
                "change_managed": u.change_managed,
                "git_hash": (u.git_hash or "")[:8] or None,
                "errors": u.errors,
            }
            for u in units
        ],
        "plugins": homes.read_plugins(home),
    }


def _artifact_lines(block: dict | None) -> list[str]:
    """At most two lines, and only when there is something to act on.

    The counts separate the two things a home can mean by "stale", because
    they call for different actions and only one of them is news:

      rebuildable  on disk and no longer describing its inputs — the
                   `skt build` case, and the one worth a session's
                   attention;
      not built    declared and never materialized, which is a lazily
                   provisioned home working as designed.

    A home that has never run `skt check` prints nothing here rather than
    a placeholder: the same hook runs `skt check` two lines later, which
    is where the absence is fixed and reported.
    """
    if not block:
        return []
    state = block.get("state")
    if state != "ok":
        if state in (None, "off", "no-cli"):
            return []
        return [f"artifacts  not measured ({state}): {block.get('reason', '')}"]
    stale = block.get("stale") or 0
    if not stale:
        return [
            f"artifacts  {block.get('total', 0)} derived, none stale "
            f"(measured {_age(block.get('age_seconds', 0))})"
        ]
    line = (
        f"artifacts  {stale} stale of {block.get('total', 0)} — "
        f"{block.get('rebuildable', 0)} rebuildable, "
        f"{block.get('not_built', 0)} declared-not-built, "
        f"{block.get('unverifiable', 0)} unverifiable "
        f"(measured {_age(block.get('age_seconds', 0))})"
    )
    lines = [line]
    if block.get("rebuildable"):
        names = [row.get("name", "?") for row in (block.get("rows") or [])]
        shown = ", ".join(names[:MAX_TEXT_ARTIFACTS])
        if len(names) > MAX_TEXT_ARTIFACTS:
            shown += f" +{len(names) - MAX_TEXT_ARTIFACTS} more"
        lines.append(f"           rebuild with: skt build --stale   ({shown})")
    return lines


def render_text(report: dict) -> str:
    if report.get("home") is None:
        return f"skt status: {report['error']}"
    lines: list[str] = []
    checkout = report["checkout"]
    lines.append(f"skt status — {checkout['root']}")
    place = f"checkout   {checkout['kind']} repo, branch {checkout['branch']}"
    if checkout["ticket"]:
        place += f" (ticket {checkout['ticket']})"
    if checkout["epic"]:
        suffix = "" if checkout.get("on_epic_branch") else " available"
        place += f" (epic {checkout['epic']}{suffix})"
    lines.append(place)
    home_line = f"home       {report['home']} — tier: {report['tier']}, policy: {report['policy']}"
    if report["drift_pending"]:
        home_line += ", DRIFT PENDING (launch will refuse; ack with: skill-manager home drift --ack)"
    lines.append(home_line)
    spec = report["spec_workflow"]
    if spec["name"]:
        open_part = (
            f"; open tickets: {', '.join(spec['open_tickets'])}"
            if spec["open_tickets"]
            else "; no open tickets"
        )
        match = spec.get("ticket_in_plan")
        if match is True:
            open_part += " — this branch's ticket IS in the plan"
        elif match is False:
            open_part += " — this branch's ticket is NOT in the plan"
        lines.append(f"spec       workflow '{spec['name']}' active{open_part}")
    sync = report.get("worktree_sync")
    if sync is not None:
        if sync["in_sync"]:
            lines.append(
                f"base       in sync with parent @{sync['parent_head']}"
                f" ({sync['ahead']} commit(s) ahead)"
            )
        else:
            lines.append(
                f"base       BASE STALE: parent @{sync['parent_head']}, worktree base "
                f"@{sync['merge_base']} (behind {sync['behind']}) — reconcile before promoting"
            )
    tools = report.get("cli_tools") or []
    if tools:
        shown = ", ".join(tools[:10]) + (f" +{len(tools)-10} more" if len(tools) > 10 else "")
        lines.append(f"cli        {shown}")
    units = report["units"]
    cm = sum(1 for u in units if u["change_managed"])
    bad = sum(1 for u in units if u["errors"])
    summary = f"units      {len(units)} installed ({cm} change-managed"
    summary += f", {bad} with errors)" if bad else ")"
    lines.append(summary)
    for unit in units[:MAX_TEXT_UNITS]:
        flags = "".join(
            [" [loaded]" if unit["loaded"] else "", " [cm]" if unit["change_managed"] else ""]
        )
        marker = " [ERRORS]" if unit["errors"] else ""
        lines.append(
            f"  {unit['name']} {unit['version']} {unit['kind'].lower()}"
            f"{flags}{marker} {unit['git_hash'] or ''}".rstrip()
        )
    if len(units) > MAX_TEXT_UNITS:
        lines.append(f"  … +{len(units) - MAX_TEXT_UNITS} more (skt status --json for all)")
    lines += _artifact_lines(report.get("artifacts"))
    plugins = report["plugins"]
    lines.append(f"plugins    {', '.join(plugins) if plugins else 'none'}")
    lines.append("next       skt check — new-version and sync notifications")
    return "\n".join(lines)


def run(as_json: bool, start: str | Path = ".") -> int:
    report = collect(start)
    print(json.dumps(report, indent=2) if as_json else render_text(report))
    return 0 if report.get("home") else 1
