"""`skt publish` — a home-edited skill, one tier up, then to its own repo.

The two moves are not alternatives (git-issue-workflow's documented
rule): `home sync` moves an edit ONE tier up so teardown cannot destroy
it, and `unit publish` is the only route to the unit's own repository —
the only copy that survives this machine. This command runs them in that
order through the home's own pinned CLI, with wt-style refusals: one
error line, one fix line.

The `home sync` is NARROWED to the unit being published
(skill-manager#182): carrying every unit meant one unrelated conflicted
unit failed the command and stopped the publish. A CLI too old to know
`--unit` falls back to the whole-home sync, loudly — see
`_lacks_unit_flag` for why that decision cannot be made on the exit code.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from . import context as ctx_mod
from . import homes
from .check import (
    LOCAL_TIMEOUT_SECONDS,
    NETWORK_BUDGET_SECONDS,
    STATE_UPSTREAM_STALE,
    _is_ancestor,
    _local_state,
    _remote_tip_safe,
    _store_dir,
)

CHECK_EXIT = 10


def edited_units(home: Path | None) -> list[dict]:
    """Units in this home carrying work their own repository does not have.

    A store whose `@{upstream}` is behind looks "ahead" to a bare
    rev-list even when every one of those commits is published
    (skill-publisher-skill#15). That false verdict is not cosmetic here:
    `skt publish` with no unit name REFUSES when more than one unit is
    edited, and `skt ticket close` warns on each — so a home with a
    handful of stale refs could not publish anything at all.

    One wall-clock deadline covers the whole pass, the way `collect()`
    bounds `skt check`. Without it this walked every still-ahead unit
    serially at `_remote_tip`'s 10s fallback: on the measured home, six
    falsely-ahead units meant an offline `skt publish --check` — or the
    `skt ticket close` leftovers gate, which is where it hurts most —
    could stall about a minute where it used to be instant. Nothing here
    is on a hook path, so the budget is the whole command's rather than a
    hook's, but "bounded" is not optional at a teardown gate.

    A probe the budget cut short leaves the unit reported, not dropped.
    That is the same direction `_local_state` chooses for `unknown`: at a
    gate whose job is to refuse while unpublished work exists, an
    evidence gap must never read as "nothing here".
    """
    if home is None:
        return []
    deadline = time.monotonic() + NETWORK_BUDGET_SECONDS
    out: list[dict] = []
    for unit in homes.read_units(home):
        unit_dir = _store_dir(home, unit)
        if unit_dir is None:
            continue
        state = _local_state(unit_dir, deadline=deadline)
        if state == "ahead" and unit.change_managed:
            # Adjudicate in place rather than re-running `_local_state`
            # with a tip: that repeated the `status` and `rev-list` calls
            # whose answers are already in hand, for every ahead unit.
            tip = _remote_tip_safe(unit.origin, unit.git_ref, deadline=deadline)
            timeout = min(float(LOCAL_TIMEOUT_SECONDS), deadline - time.monotonic())
            if tip and _is_ancestor(unit_dir, "HEAD", tip, timeout) is True:
                state = STATE_UPSTREAM_STALE
        if state not in ("clean", STATE_UPSTREAM_STALE):
            out.append({"unit": unit.name, "state": state, "dir": str(unit_dir)})
    return out


def _parent_home(home: Path, start: str | Path) -> tuple[Path | None, str | None]:
    """One tier up, resolved the way close-change.sh resolves --into.

    Returns (parent, error). (None, None) means the ROOT tier — nothing
    above by design. (None, "<why>") means the tier REQUIRES a parent and
    none could be named; callers must fail loudly, never skip the sync.
    """
    root = ctx_mod.checkout_root(start)
    tier = ctx_mod.classify_tier(home, root)
    if tier == "worktree":
        # The clone source: the main working tree's own home — the same
        # derivation bootstrap-home.sh and close-change.sh use, so the
        # tiers agree by construction.
        main_tree = ctx_mod._git("worktree", "list", "--porcelain", cwd=root)
        if main_tree:
            first = main_tree.splitlines()[0].split(" ", 1)[1]
            candidate = Path(first) / ".skill-manager"
            if candidate.is_dir():
                return candidate, None
            return None, (
                f"this worktree's project home is missing at {candidate} — "
                "the main working tree has no home"
            )
        return None, "git worktree list named no main working tree"
    if tier == "project":
        root_h = homes.root_home()
        if root_h.is_dir():
            return root_h, None
        return None, f"operator root home not found at {root_h}"
    return None, None  # root tier: nothing above; publish goes straight to the unit repo


def _cli(home: Path) -> Path:
    return home / "bin" / "cli" / "skill-manager"


def _cli_env() -> dict:
    """Env for invoking a home pin, livelock-guarded.

    Older pins are the unguarded `cli="${SKILL_MANAGER_CLI:-<abs>}"` form:
    if the session exports SKILL_MANAGER_CLI naming a pin, the pin execs
    itself forever (measured in close-change.sh's history). The pin embeds
    its own absolute CLI path as the fallback, so stripping the variable
    is always correct here — mirroring close-change.sh run_cli().
    """
    import os

    env = dict(os.environ)
    env.pop("SKILL_MANAGER_CLI", None)
    return env


def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_cli(home)), *args], capture_output=True, text=True, env=_cli_env()
    )


# skill-manager's UnknownUnitException.EXIT_CODE. Unambiguous: only a CLI
# that HAS `--unit` can produce it.
UNKNOWN_UNIT_EXIT = 12
# picocli's usage exit. A CLI without `--unit` returns this for the unknown
# option — and skill-manager#182 as first merged ALSO returned it for a unit
# name neither home holds, so this code alone decides nothing.
USAGE_EXIT = 2


def _lacks_unit_flag(proc: subprocess.CompletedProcess) -> bool:
    """Did this CLI refuse because it does not KNOW `--unit`?

    Measured against the pre-#182 CLI:

        exit=2
        Unknown options: '--unit', 'alpha'
        Usage: skill-manager home sync [-hv] [--agent-context] ...

    and against the first merged #182 CLI, for a name neither home holds:

        exit=2
        ✗ home sync --unit X: no unit named 'X' in either home (...)

    Same exit code, opposite meanings — picocli's usage code and
    `UnknownUnitException`'s were both 2. So the discrimination cannot be
    the exit code, and getting it wrong is not cosmetic: treating a typo'd
    unit name as "old CLI" would silently fall back to the whole-home sync
    that skill-manager#182 exists to avoid, which is the one outcome worse
    than failing. Keyed on picocli's own signature, which the narrowed
    refusal never emits. `"unknown option"` matches the plural form too.
    """
    if proc.returncode != USAGE_EXIT:
        return False
    blob = (proc.stdout + proc.stderr).lower()
    return "unknown option" in blob and "--unit" in blob


def _sync_one_tier_up(home: Path, parent: Path, unit_name: str) -> tuple[subprocess.CompletedProcess, bool]:
    """`home sync` the edited unit up a tier. Returns (proc, narrowed).

    Narrowed by default, because a whole-home sync is all-or-nothing: one
    unrelated conflicted unit failed the command and stopped the publish
    (skill-manager#182). Every project and worktree home currently carries
    a pin older than that change, so a hard failure against one would take
    `skt publish` from "blocked when a neighbour conflicts" to "blocked
    always" — strictly worse. Hence the fallback, which is announced
    rather than silent: on an old CLI the caller is back to the
    all-or-nothing behaviour and should know why.
    """
    targeted = _run_cli(
        home, "home", "sync", "--from", str(home), "--to", str(parent), "--merge",
        "--unit", unit_name,
    )
    if not _lacks_unit_flag(targeted):
        return targeted, True
    print(
        f"note: {_cli(home)} predates `home sync --unit`, so this falls back to a "
        f"whole-home sync — an unrelated conflicted unit can still block it. "
        f"Upgrade the CLI this home is pinned to, then re-run."
    )
    whole = _run_cli(home, "home", "sync", "--from", str(home), "--to", str(parent), "--merge")
    return whole, False


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

    parent, parent_error = _parent_home(home, start)
    if parent is None and parent_error is not None:
        print(f"error: cannot resolve the parent home — {parent_error}")
        print(
            "fix:   run the sync yourself with an explicit destination, then re-run: "
            f"{_cli(home)} home sync --from {home} --to <parent-home> --merge "
            f"--unit {unit_name}"
        )
        return 1
    if parent is not None:
        proc, narrowed = _sync_one_tier_up(home, parent, unit_name)
        if proc.returncode != 0:
            print(f"error: home sync into {parent} failed (exit {proc.returncode})")
            if proc.returncode == UNKNOWN_UNIT_EXIT:
                # The narrowed sync refused the NAME. Never retry whole-home
                # here: that would publish under a different reconciliation
                # than the one asked for.
                print(f"fix:   no unit named {unit_name!r} in this home or its parent — "
                      f"check `skt publish --check` for the name")
            elif narrowed:
                print(f"fix:   resolve the conflict reported in {unit_name}, then re-run "
                      f"skt publish {unit_name}")
            else:
                print("fix:   this CLI cannot sync one unit, so an unrelated unit can block "
                      f"it — resolve the reported conflict, or upgrade {_cli(home)}, then "
                      f"re-run skt publish {unit_name}")
            sys.stdout.write(proc.stdout[-1500:] + proc.stderr[-1500:])
            return proc.returncode
        scope = f"{unit_name} only" if narrowed else "whole home"
        print(f"synced: this home -> {parent} ({scope}; one tier up; teardown-safe)")

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
