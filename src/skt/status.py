"""`skt status` — the startup report. Local disk only; no network."""

from __future__ import annotations

import json
from pathlib import Path

from . import context as ctx_mod
from . import homes

SCHEMA_VERSION = 1
MAX_TEXT_UNITS = 15


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
        "policy": homes.read_policy(home),
        "drift_pending": homes.drift_pending(home),
        "checkout": {
            "root": str(ctx_mod.checkout_root(start)),
            "kind": tctx.kind,
            "branch": tctx.branch,
            "ticket": tctx.ticket,
            "epic": tctx.epic,
        },
        "spec_workflow": {
            "name": tctx.spec_workflow,
            "open_tickets": tctx.spec_open_tickets,
        },
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
        place += f" (epic {checkout['epic']})"
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
        lines.append(f"spec       workflow '{spec['name']}' active{open_part}")
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
    plugins = report["plugins"]
    lines.append(f"plugins    {', '.join(plugins) if plugins else 'none'}")
    lines.append("next       skt check — new-version and sync notifications")
    return "\n".join(lines)


def run(as_json: bool, start: str | Path = ".") -> int:
    report = collect(start)
    print(json.dumps(report, indent=2) if as_json else render_text(report))
    return 0 if report.get("home") else 1
