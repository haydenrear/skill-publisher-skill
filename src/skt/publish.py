"""`skt publish` — a home-edited skill, one tier up, then to its own repo.

The two moves are not alternatives (git-issue-workflow's documented
rule): `home sync` moves an edit ONE tier up so teardown cannot destroy
it, and `unit publish` is the only route to the unit's own repository —
the only copy that survives this machine. This command runs them in that
order through the home's own pinned CLI, with wt-style refusals: one
error line, one fix line.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import context as ctx_mod
from . import homes
from .check import _local_state, _store_dir

CHECK_EXIT = 10


def edited_units(home: Path | None) -> list[dict]:
    if home is None:
        return []
    out: list[dict] = []
    for unit in homes.read_units(home):
        unit_dir = _store_dir(home, unit)
        if unit_dir is None:
            continue
        state = _local_state(unit_dir)
        if state != "clean":
            out.append({"unit": unit.name, "state": state, "dir": str(unit_dir)})
    return out


def _parent_home(home: Path, start: str | Path) -> Path | None:
    """One tier up: worktree -> project home; project -> root; root -> None."""
    root = ctx_mod.checkout_root(start)
    tier = ctx_mod.classify_tier(home, root)
    if tier == "worktree":
        main_tree = ctx_mod._git("worktree", "list", "--porcelain", cwd=root)
        if main_tree:
            first = main_tree.splitlines()[0].split(" ", 1)[1]
            candidate = Path(first) / ".skill-manager"
            if candidate.is_dir():
                return candidate
        return None
    if tier == "project":
        root_h = homes.root_home()
        return root_h if root_h.is_dir() else None
    return None  # root tier: nothing above; publish goes straight to the unit repo


def _cli(home: Path) -> Path:
    return home / "bin" / "cli" / "skill-manager"


def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(_cli(home)), *args], capture_output=True, text=True)


def run(unit_name: str | None, *, check_only: bool = False, ticket: str | None = None,
        start: str | Path = ".") -> int:
    home = homes.find_home(start)
    if home is None:
        print("skt publish: no skill-manager home found")
        return 1
    edited = edited_units(home)
    if check_only:
        if not edited:
            print("skt publish --check: no edited units in this home")
            return 0
        for entry in edited:
            print(f"edited: {entry['unit']} ({entry['state']}) at {entry['dir']}")
        return CHECK_EXIT
    if not unit_name:
        if not edited:
            print("skt publish: nothing to publish — no edited units in this home")
            return 0
        if len(edited) > 1:
            names = ", ".join(e["unit"] for e in edited)
            print(f"error: several units are edited ({names})")
            print("fix:   skt publish <unit>   # one at a time, in dependency order")
            return 1
        unit_name = edited[0]["unit"]
    if not _cli(home).is_file():
        print(f"error: home CLI not found at {_cli(home)}")
        return 1

    parent = _parent_home(home, start)
    if parent is not None:
        proc = _run_cli(home, "home", "sync", "--from", str(home), "--to", str(parent), "--merge")
        if proc.returncode != 0:
            print(f"error: home sync into {parent} failed (exit {proc.returncode})")
            print("fix:   resolve the reported conflict, then re-run skt publish "
                  f"{unit_name}")
            sys.stdout.write(proc.stdout[-1500:] + proc.stderr[-1500:])
            return proc.returncode
        print(f"synced: this home -> {parent} (one tier up; teardown-safe)")

    ticket = ticket or _infer_ticket(start)
    publish_args = ["unit", "publish", unit_name]
    if ticket:
        publish_args += ["--ticket", ticket]
    proc = _run_cli(home, *publish_args)
    if proc.returncode != 0:
        print(f"error: unit publish for {unit_name} failed (exit {proc.returncode})")
        print(f"fix:   {_cli(home)} unit publish {unit_name} --ticket <ticket> --verbose")
        sys.stdout.write(proc.stdout[-1500:] + proc.stderr[-1500:])
        return proc.returncode
    print(f"published: {unit_name} -> its own repository (branch skill/{ticket or '<ticket>'}-{unit_name})")
    print("This is the only copy that survives this machine; the PR it opened still needs review.")
    return 0


def _infer_ticket(start: str | Path) -> str | None:
    branch = ctx_mod._git("branch", "--show-current", cwd=ctx_mod.checkout_root(start))
    if branch.startswith("feature/"):
        return branch[len("feature/"):]
    return None
