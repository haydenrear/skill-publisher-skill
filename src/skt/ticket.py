"""`skt ticket` — the worktree lifecycle, imported from git-issue-workflow.

skt does not reimplement or shell out to the `wt` path by hand: it
imports the typed Python surface that git-issue-workflow registers
(SKT-2) and adds skt's framing — orientation after `new`, guided
remedies on a refused `close`.

`list` and `sweep` are the FLEET verbs and live in :mod:`skt.sweep`.
They take no TICKET, they do not go through `wt` — an epic retires a
dozen worktrees at once and `wt close` resolves exactly one ticket by
search — and `list` in particular must work in a repository where
git-issue-workflow is not importable at all, since answering "what is
still standing here?" is how an operator finds out what is wrong.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from . import homes

UNIT = "git-issue-workflow"
UNIT_SOURCE = "github:haydenrear/git-issue-workflow-skill"


def _cli_name(home: Path | None) -> str:
    """The command that writes THIS home: its pin when it has one."""
    if home is not None:
        pin = home / "bin" / "cli" / "skill-manager"
        if pin.is_file():
            return str(pin)
    return "skill-manager"


def _declared_in_manifest(manifest: Path, unit: str) -> bool:
    """Does `skill-project.toml` name this unit under any unit-kind table?"""
    try:
        data = tomllib.loads(manifest.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    for kind in ("skills", "plugins", "docs", "harnesses"):
        table = data.get(kind)
        if isinstance(table, dict) and unit in table:
            return True
    return False


def _manifest_path(start: str | Path = ".") -> Path | None:
    from . import context as ctx_mod

    candidate = ctx_mod.checkout_root(start) / "skill-project.toml"
    return candidate if candidate.is_file() else None


def _giw_remedy(home: Path | None, start: str | Path = ".") -> list[str]:
    """The commands that actually fix this home, for THIS home's state.

    `sync` was named unconditionally, and `sync` cannot install: it pulls
    an already-installed unit to its latest source. In every home that
    hit this the unit was neither installed NOR declared, so the remedy
    named a unit that does not exist there — measured identically in
    `constituents/skill-manager`'s home and in
    `constituents/meta-orchestrator`'s, and hit five times over this
    epic. Not-installed and not-synced are different faults with
    different fixes, so they are told apart here.
    """
    cli = _cli_name(home)
    if home is None:
        return [
            "no skill-manager home was found from here",
            "fix:   create this checkout's home first — scripts/agent-home.sh, or "
            "git-issue-workflow's scripts/bootstrap-home.sh --root <repo-root>",
        ]
    installed = (home / "skills" / UNIT).is_dir() or (
        home / "installed" / f"{UNIT}.json"
    ).is_file()
    if installed:
        return [
            f"{UNIT} is installed in {home} but carries no importable python surface",
            f"fix:   {cli} sync {UNIT} --git-latest   # needs the SKT-2 version or later",
        ]
    manifest = _manifest_path(start)
    if manifest is not None and _declared_in_manifest(manifest, UNIT):
        return [
            f"{UNIT} is declared in {manifest} but is not installed in {home}",
            f"fix:   SKILL_MANAGER_HOME={home} {cli} project resolve",
        ]
    lines = [
        f"{UNIT} is neither installed in {home} nor declared in "
        f"{manifest if manifest is not None else 'any skill-project.toml above here'}"
        " — `sync` cannot install it",
        f"fix:   SKILL_MANAGER_HOME={home} {cli} install {UNIT_SOURCE}",
    ]
    if manifest is not None:
        lines.append(
            f"       and add it to {manifest} so the home can be rebuilt:\n"
            f"           [skills.{UNIT}]\n"
            f'           source = "{UNIT_SOURCE}"\n'
            f"       then: SKILL_MANAGER_HOME={home} {cli} project resolve"
        )
    return lines


def _import_wrapper(start: str | Path = "."):
    """Import git_issue_workflow from the environment or the home's store copy."""
    try:
        import git_issue_workflow  # noqa: F401

        return sys.modules["git_issue_workflow"]
    except ImportError:
        pass
    home = homes.find_home(start)
    if home is not None:
        candidate = home / "skills" / UNIT / "src"
        if (candidate / "git_issue_workflow").is_dir():
            sys.path.insert(0, str(candidate))
            import git_issue_workflow

            return git_issue_workflow
    raise SystemExit(
        "\n".join(
            [
                f"skt ticket: the {UNIT} python surface is not importable.",
                *_giw_remedy(home, start),
            ]
        )
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


def _bootstrap_script() -> Path | None:
    giw = homes.find_home(".")
    if giw is None:
        return None
    candidate = giw / "skills" / UNIT / "scripts" / "bootstrap-home.sh"
    return candidate if candidate.is_file() else None


def epic_new(ticket_id: str, base: str | None, path: str) -> int:
    """Create a DECLARED-path worktree the way an epic assignment requires.

    Epic assignments name the exact worktree path and base, which the
    conventional `wt new` cannot produce (it derives its own path) — the
    docs hand-roll `git worktree add` + `bootstrap-home.sh` for this one
    case. This subsumes that pair, with the index-base pinning
    conventions (clean tree; OIDs resolved once; create-only retention
    ref; branch from the pinned commit, never the moving ref) and the
    same roll-back-on-bootstrap-failure contract as new-change.sh.
    """
    import subprocess

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], capture_output=True, text=True)

    dirty = git("status", "--porcelain")
    if dirty.stdout.strip():
        print("error: working tree is not clean — an epic worktree pins its base from a clean slate")
        print("fix:   commit or stash, then re-run")
        return 1
    base_ref = base or "HEAD"
    commit = git("rev-parse", base_ref)
    tree = git("rev-parse", f"{base_ref}^{{tree}}")
    if commit.returncode != 0 or tree.returncode != 0:
        print(f"error: cannot resolve base {base_ref!r}")
        print("fix:   git fetch origin, then pass --base <existing-ref>")
        return 1
    commit_oid, tree_oid = commit.stdout.strip(), tree.stdout.strip()
    toplevel = git("rev-parse", "--show-toplevel").stdout.strip()
    repo_id = Path(toplevel).name if toplevel else "repo"
    ref_name = f"refs/index-bases/{repo_id}/{tree_oid}"
    existing = git("rev-parse", "--verify", "--quiet", ref_name)
    if existing.returncode == 0 and existing.stdout.strip() != commit_oid:
        print(f"error: retention ref {ref_name} already points at {existing.stdout.strip()[:8]}, not {commit_oid[:8]}")
        print("fix:   the declared base disagrees with an earlier pin — reconcile with the epic owner")
        return 1
    if existing.returncode != 0:
        made = git("update-ref", ref_name, commit_oid, "")
        if made.returncode != 0:
            print(f"error: could not create retention ref {ref_name}: {made.stderr.strip()}")
            return 1
    branch = f"feature/{ticket_id}"
    added = git("worktree", "add", path, "-b", branch, commit_oid)
    if added.returncode != 0:
        print(f"error: git worktree add failed: {added.stderr.strip().splitlines()[-1] if added.stderr.strip() else added.returncode}")
        print(f"fix:   git worktree add {path} -b {branch} {commit_oid[:12]}   # then bootstrap-home.sh --root {path}")
        return 1
    bootstrap = _bootstrap_script()
    if bootstrap is None:
        git("worktree", "remove", "--force", path)
        git("branch", "-D", branch)
        print("error: bootstrap-home.sh not found in this home; worktree rolled back")
        # Same fault, same distinction: a home that never installed the
        # unit cannot sync it. See _giw_remedy.
        for line in _giw_remedy(homes.find_home(".")):
            print(line if line.startswith("fix:") else f"       {line}")
        print("       then re-run")
        return 3
    proc = subprocess.run([str(bootstrap), "--root", path], capture_output=True, text=True)
    if proc.returncode != 0:
        git("worktree", "remove", "--force", path)
        git("branch", "-D", branch)
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        print(f"error: home bootstrap failed (exit {proc.returncode}); worktree and branch rolled back")
        if tail:
            print("       " + tail[-1])
        print(f"fix:   {bootstrap} --root <repo-root>   # once per repository, then re-run")
        return 3
    print(f"created epic worktree {path}")
    print(f"branch     {branch} (pinned base {commit_oid[:12]}; retention ref {ref_name})")
    launch = Path(path) / ".skill-manager" / "bin" / "launch" / "claude"
    if launch.is_file():
        print(f"launch     {launch}")
    print(f"close      skt ticket close {ticket_id}   # resolves declared paths by search")
    print(
        "\nA skill edit inside that worktree's home is in no git diff; "
        "run `skt publish` there before closing, or the close gate will refuse."
    )
    return 0


USAGE = "\n".join(
    [
        "usage: skt ticket new|close|info <TICKET> [--base <branch>] [--path <dir>]",
        "       skt ticket list  [--epic <slug>] [--target <ref>] [--into <home>] [--json]",
        "       skt ticket sweep [--epic <slug>] [--target <ref>] [--into <home>] "
        "[-y|--yes] [--json]",
    ]
)

#: The verbs that address the WHOLE set of ticket worktrees rather than
#: one ticket, so they take no TICKET argument.
FLEET_VERBS = ("list", "sweep")


def run(
    verb: str | None,
    ticket_id: str | None,
    base: str | None = None,
    path: str | None = None,
    *,
    epic: str | None = None,
    target: str | None = None,
    into: str | None = None,
    yes: bool = False,
    as_json: bool = False,
    start: str | Path = ".",
) -> int:
    if verb in FLEET_VERBS:
        if ticket_id:
            print(
                f"skt ticket {verb}: takes no TICKET — it addresses every ticket worktree "
                f"of this repository. Did you mean --epic {ticket_id}?",
                file=sys.stderr,
            )
            return 1
        from . import sweep as sweep_mod

        if verb == "list":
            return sweep_mod.run_list(
                start=start, epic=epic, target=target, into=into, as_json=as_json
            )
        return sweep_mod.run_sweep(
            start=start, epic=epic, target=target, into=into, yes=yes, as_json=as_json
        )
    if not verb or not ticket_id:
        print(USAGE, file=sys.stderr)
        return 1
    if verb == "new" and path:
        return epic_new(ticket_id, base, path)
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
        print(
            f"skt ticket: unknown verb {verb!r} (expected new, close, info, list or sweep)",
            file=sys.stderr,
        )
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
