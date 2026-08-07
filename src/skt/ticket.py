"""`skt ticket` — the worktree lifecycle, imported from git-issue-workflow.

skt does not reimplement or shell out to the `wt` path by hand: it
imports the typed Python surface that git-issue-workflow registers
(SKT-2) and adds skt's framing — orientation after `new`, guided
remedies on a refused `close`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import homes


def _import_wrapper():
    """Import git_issue_workflow from the environment or the home's store copy."""
    try:
        import git_issue_workflow  # noqa: F401

        return sys.modules["git_issue_workflow"]
    except ImportError:
        pass
    home = homes.find_home(".")
    if home is not None:
        candidate = home / "skills" / "git-issue-workflow" / "src"
        if (candidate / "git_issue_workflow").is_dir():
            sys.path.insert(0, str(candidate))
            import git_issue_workflow

            return git_issue_workflow
    raise SystemExit(
        "skt ticket: the git-issue-workflow python surface is not importable.\n"
        "fix: skill-manager sync git-issue-workflow   # needs the SKT-2 version or later"
    )


def _print_contract(contract) -> None:
    print(f"worktree   {contract.worktree}")
    print(f"branch     {contract.branch}")
    if contract.launch:
        print(f"launch     {contract.launch}")
    if contract.if_exit_8:
        print(f"if-exit-8  {contract.if_exit_8}")
    print(f"close      skt ticket close — or: {contract.close}")
    if contract.propagate:
        print(f"propagate  {contract.propagate}")


def run(verb: str | None, ticket_id: str | None, base: str | None = None) -> int:
    if not verb or not ticket_id:
        print("usage: skt ticket new|close|info <TICKET> [--base <branch>]", file=sys.stderr)
        return 1
    giw = _import_wrapper()
    try:
        if verb == "new":
            contract = giw.wt_new(ticket_id, base)
            print(f"created ticket worktree for {ticket_id}:")
            _print_contract(contract)
            print(
                "\nA skill edit inside that worktree's home is in no git diff; "
                "run `skt publish` there before closing, or the close gate will refuse."
            )
            return 0
        if verb == "info":
            info = giw.wt_info(ticket_id)
            _print_contract(info)
            from . import context as ctx_mod

            sync = ctx_mod.worktree_sync(Path(info.worktree))
            if sync is not None:
                if sync.in_sync:
                    print(f"base       in sync with parent @{sync.parent_head[:8]} ({sync.ahead} ahead)")
                else:
                    print(
                        f"base       BASE STALE: parent @{sync.parent_head[:8]}, base "
                        f"@{sync.merge_base[:8]} (behind {sync.behind}) — reconcile before promoting"
                    )
            return 0
        if verb == "close":
            from . import publish as publish_mod

            # The advisory must inspect the TARGET worktree's home, not
            # cwd's — `skt ticket close <T>` runs from anywhere, and the
            # edited skills at risk live in the home being torn down.
            target_home = None
            try:
                info = giw.wt_info(ticket_id)
                candidate = Path(info.worktree) / ".skill-manager"
                if candidate.is_dir():
                    target_home = candidate
            except giw.WtError:
                pass
            leftovers = publish_mod.edited_units(target_home or homes.find_home("."))
            if leftovers:
                names = ", ".join(u["unit"] for u in leftovers)
                print(f"note: edited unit(s) in this home before close: {names}")
                print("      (`skt publish <unit>` moves them out; the gate below enforces it)")
            result = giw.wt_close(ticket_id)
            print(f"closed {result.worktree}")
            if result.branch:
                print(f"branch {result.branch} kept — delete once the change has landed")
            if result.home_work:
                print(f"home-work: {result.home_work}")
            return 0
        print(f"skt ticket: unknown verb {verb!r} (expected new, close or info)", file=sys.stderr)
        return 1
    except giw.CloseRefused as err:
        print(f"error: close refused — {err.reason}")
        print(f"fix:   {err.fix or 'skt publish   # then re-run skt ticket close'}")
        if err.log:
            print(f"log:   {err.log}")
        return 4
    except giw.BootstrapFailed as err:
        print(f"error: {err.reason}")
        print(f"fix:   {err.fix}")
        if err.log:
            print(f"log:   {err.log}")
        return 3
    except giw.WtError as err:
        print(f"error: {err.reason}")
        if err.fix:
            print(f"fix:   {err.fix}")
        return err.exit_code or 1
