"""Checkout classification: repo kind, home tier, ticket/epic/spec context.

Marker files and git plumbing only — mirrors git-issue-workflow's
lib.sh semantics (integration.toml presence, never its contents).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .homes import root_home


def _git(*args: str, cwd: str | Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def checkout_root(start: str | Path = ".") -> Path:
    top = _git("rev-parse", "--show-toplevel", cwd=start)
    return Path(top) if top else Path(start).resolve()


def is_linked_worktree(root: Path) -> bool:
    git_dir = _git("rev-parse", "--git-dir", cwd=root)
    common = _git("rev-parse", "--git-common-dir", cwd=root)
    return bool(git_dir and common) and Path(git_dir).resolve() != Path(common).resolve()


def checkout_kind(root: Path) -> str:
    if (root / "integration.toml").is_file():
        return "integration"
    for parent in root.parents:
        if (parent / "integration.toml").is_file():
            return "constituent"
    return "standalone"


def classify_tier(home: Path, root: Path) -> str:
    try:
        if home.resolve() == root_home().resolve():
            return "root"
    except OSError:
        pass
    if is_linked_worktree(root) and _inside(home, root):
        return "worktree"
    return "project"


def _inside(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class TicketContext:
    branch: str
    ticket: str | None
    epic: str | None
    kind: str
    tier: str
    spec_workflow: str | None = None
    spec_open_tickets: list[str] = field(default_factory=list)


_TICKET_RE = re.compile(r"^feature/(.+)$")
_EPIC_RE = re.compile(r"^epic/(.+)$")


def spec_workflow(root: Path) -> tuple[str | None, list[str]]:
    plan = root / "specs" / "desired_program_model" / "ticket_plan.yaml"
    if not plan.is_file():
        return None, []
    name = None
    open_tickets: list[str] = []
    current_id = None
    try:
        for line in plan.read_text().splitlines():
            if name is None and re.match(r"^name:\s*\S", line):
                name = line.split(":", 1)[1].strip().strip("'\"")
            m = re.match(r"^\s*-\s*id:\s*(\S+)", line)
            if m:
                current_id = m.group(1).strip("'\"")
            m = re.match(r"^\s*status:\s*(\S+)", line)
            if m and current_id and m.group(1).strip("'\"").lower() in ("open", "in_progress"):
                open_tickets.append(current_id)
                current_id = None
    except OSError:
        return None, []
    return name or "(unnamed)", open_tickets


def gather(start: str | Path, home: Path) -> TicketContext:
    root = checkout_root(start)
    branch = _git("branch", "--show-current", cwd=root) or "(detached)"
    ticket = None
    match = _TICKET_RE.match(branch)
    if match:
        ticket = match.group(1)
    epic = None
    match = _EPIC_RE.match(branch)
    if match:
        epic = match.group(1)
    upstream_epic = _git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/epic/*", cwd=root
    )
    if not epic and upstream_epic:
        epic = upstream_epic.splitlines()[0].split("/", 1)[1] + " (branch present)"
    name, open_tickets = spec_workflow(root)
    return TicketContext(
        branch=branch,
        ticket=ticket,
        epic=epic,
        kind=checkout_kind(root),
        tier=classify_tier(home, root),
        spec_workflow=name,
        spec_open_tickets=open_tickets,
    )
