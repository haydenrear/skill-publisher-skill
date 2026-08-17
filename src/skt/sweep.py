"""`skt ticket list|sweep` — retire an epic's worktrees in one pass.

An epic keeps its ticket worktrees alive until the end: a ticket's
worktree is where its reviewer looks, and the epic owner wants them all
standing until integration is done. So the retirement is not N
independent `skt ticket close` calls spread over a month — it is one
sweep at the end, over a dozen directories, and doing it by hand is
where the safety checks get skipped. The hand-loop that motivated this
was `for d in ../wt-*; do git worktree remove --force $d; done`, which
is `rm -rf` with extra steps: it takes the worktree's Skill Manager home
with it, and that home is gitignored, so the loss appears in no diff.

Two verbs, and the split is deliberate:

  `list`   read-only. Every ticket worktree this checkout created, with
           the flags that decide whether it may be retired. Nothing is
           removed and nothing is written.
  `sweep`  the pass. Refuses by default — with no `--yes` it prints the
           same plan `list` would justify and changes nothing.

The safety model
----------------

**Per worktree, re-checked immediately before that worktree is
removed.** Not once up front: a sweep over a dozen homes takes minutes
(the close-out gate is a full compare of two homes, per worktree), and
an agent finishing a ticket in one of them halfway through the pass is
the normal case, not the exotic one. A plan computed at t=0 is a
description of the past. So :func:`inspect` runs again inside the
removal loop and the second answer is the one that decides.

Five things make a worktree **skipped, not removed**, and a skip is a
normal outcome that keeps the pass going:

  locked           git itself refuses; say so rather than discover it.
  uncommitted      the only state `git worktree remove` alone protects.
  stash entries    attributed by branch — see `_stash_entries`.
  unpushed commits the branch ref outlives the worktree, so this is not
                   a data-loss gate; it is the workflow one. Work that
                   never reached the remote has not been reviewed, and
                   retiring its worktree is how it gets forgotten.
  not contained    commits not in the epic/target branch: the ticket did
                   not land, whatever its branch says.

and one more that is not a property of the repository at all: the
worktree's Skill Manager home. `git worktree remove` deletes
`<worktree>/.skill-manager` without asking. Nothing in git can see that,
so the gate is `skill-manager home close-out --home <worktree-home>
--into <destination>`, which is the same gate `skt ticket close` runs,
here run per worktree. A non-clean verdict skips.

**The destination home is the MAIN working tree's**, resolved from the
primary checkout — never `homes.find_home(".")`. Run from inside a
worktree, that helper walks up from `$PWD` and finds the worktree's OWN
home, and a close-out gate whose `--home` and `--into` are the same
directory compares a home against itself and reports clean for
everything. The one path that must not have a plausible-looking bug is
the one deciding whether it is safe to delete a directory.

Two exit codes from that gate mean things the summary must not flatten
into "not clean" (documented in git-issue-workflow's `complete.md`):

  exit 2  the path passed to `--home` is not a home at all. Nothing was
          assessed, so nothing is printed — and the classic cause is
          passing the worktree directory instead of its `.skill-manager`,
          which is also the path `git worktree remove` takes. Counted as
          a FAILURE here, not a safety skip: it means this command's own
          derivation was wrong, or the home is broken.
  exit 9  the destination home's policy is `frozen`, so the gate was
          refused and nothing was attempted. That is a statement about
          the destination, not about any worktree — it will repeat
          identically for every one — so the pass is abandoned rather
          than emitting N identical skips.

Removal is `git worktree remove`, never `rm -rf`, and `--force` is never
passed: it is the flag whose whole purpose is to make a blocker go away,
and every blocker above is one this command exists to respect. The
primary checkout is never a candidate, and the worktree the command is
running in is refused explicitly — removing the directory you are
standing in is how a sweep becomes an incident.

Why free-space delta and not `du`
---------------------------------

These homes are cloned copy-on-write (APFS `clonefile`), so `du`
attributes every shared block to every copy and over-reports by roughly
30x: a home `du` calls 1.1 GB was measured to cost 33.7 MB of real
space. Reporting that number would tell an operator they are about to
reclaim thirty times what they will. So the only size this module
reports is the free-space delta over the whole pass, from
`os.statvfs` before and after, and no per-worktree size at all.

(Nothing here is re-exported from `skt/__init__.py`, deliberately: the
module is `skt.sweep` and its main entry point is `sweep`, so a
re-export would shadow one with the other depending on import order —
the collision `build_cmd.py` was renamed to avoid. Import it as
`from skt.sweep import sweep`.)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import context as ctx_mod
from .publish import _cli, _cli_env

#: Budget for one read (`status`, `rev-list`, a reflog walk). Generous for
#: a local call and a hard stop for a git that hangs on a credential
#: helper — `check._run_git` carries the same note about git's children.
READ_TIMEOUT_SECONDS = 10.0

#: The gate is the slow step by an order of magnitude: a CLI start plus a
#: full compare of two homes, per worktree. Bounded anyway, because a
#: sweep that stops answering at worktree 4 of 12 is worse than one that
#: skips worktree 4 and says why. It writes nothing, so a killed gate
#: leaves nothing half-done.
GATE_TIMEOUT_SECONDS = 180.0

#: And nothing for the removal, deliberately — the same reasoning as
#: `artifacts.BUILD_TIMEOUT_SECONDS`. `_run` kills the whole process
#: group on a deadline, which is right for a read and is damage on a
#: write: a `git worktree remove` SIGKILLed partway through leaves a
#: half-deleted directory and an admin entry pointing into it.
REMOVE_TIMEOUT_SECONDS: float | None = None

#: `skill-manager home close-out`'s reserved exits. See the module
#: docstring — neither may be flattened into "not clean".
GATE_EXIT_NOT_A_HOME = 2
GATE_EXIT_FROZEN = 9

#: This command's own exits.
#:
#:   0  the pass completed. Removals AND safety skips both live here: a
#:      skip is the gate doing its job, and `build_cmd` sets the same
#:      precedent for `not buildable here` rows ("that is not a failure
#:      of this run and exiting non-zero for it would make every printed
#:      remedy fail after doing its job correctly").
#:   1  something FAILED — a removal errored, or a gate could not
#:      establish anything — or a precondition refused before the pass.
#:   9  the destination home is frozen; the pass was abandoned.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_FROZEN_DESTINATION = GATE_EXIT_FROZEN

TICKET_PLAN = "specs/desired_program_model/ticket_plan.yaml"

#: The worktree's own Skill Manager home, which is NOT part of the
#: repository's working-tree state for this purpose. Real repos gitignore
#: it, but a repo that does not would otherwise report its home as
#: "uncommitted changes" — the wrong reason for the right directory. The
#: home is what `home close-out` assesses, in far more detail than
#: `status --porcelain` could, so the gate owns it and this check does not
#: double-count it.
HOME_DIR = ".skill-manager"

#: `refs/stash` is SHARED between worktrees (measured on git 2.50: a
#: stash pushed in a linked worktree is listed by `git stash list` in the
#: primary and in every sibling). So a per-worktree stash count read from
#: `git -C <worktree> stash list` is the WHOLE repository's count, and
#: blocking on it would make one unrelated stash in the primary refuse
#: every worktree in the sweep. Attribution comes from the entry's own
#: reflog subject instead, which records the branch it was made on:
#: `WIP on feature/T-1: a666a10 init`, or `On feature/T-1: <message>` for
#: an explicit `git stash push -m`.
_STASH_SUBJECT = re.compile(r"^(?:WIP on|On) (?P<branch>.+?): ")

#: What a stash made on a detached HEAD looks like. It cannot be
#: attributed to a worktree, so it blocks only the detached ones.
_STASH_NO_BRANCH = "(no branch)"

#: Bound on how much of a probe's output is carried into a report.
MAX_REASON_ITEMS = 6


# ------------------------------------------------------------------ plumbing


def _run(
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None,
    env: dict | None = None,
) -> subprocess.CompletedProcess | None:
    """One bounded child. None on deadline; `timeout=None` is unbounded.

    `subprocess.run(timeout=)` kills only the direct child, and both
    programs run here spawn helpers that inherit the pipes and keep the
    caller open past its budget (git's credential and ssh helpers,
    skill-manager's package managers). Own session + killpg reaps the
    lot — the same treatment `check._run_git` and `artifacts._run` give
    it, and the same reason `REMOVE_TIMEOUT_SECONDS` is None.
    """
    if timeout is not None and timeout <= 0:
        return None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:  # group already gone, or unkillable
            proc.kill()
        try:
            proc.communicate(timeout=5)  # reap — SIGKILL cannot be blocked
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _git(*args: str, cwd: str | Path | None = None) -> subprocess.CompletedProcess | None:
    return _run(["git", *args], cwd=cwd, timeout=READ_TIMEOUT_SECONDS)


def _out(*args: str, cwd: str | Path | None = None) -> str | None:
    """Stdout of a successful git read; None when it did not succeed.

    None and "" are different answers and both happen: `rev-parse
    --verify --quiet` on a missing ref exits 1 with no output, while
    `status --porcelain` on a clean tree exits 0 with no output. A probe
    that could not run must never read as "nothing to worry about".
    """
    proc = _git(*args, cwd=cwd)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _count(*args: str, cwd: str | Path | None = None) -> int | None:
    raw = _out(*args, cwd=cwd)
    if raw is None:
        return None
    try:
        return int(raw.splitlines()[0]) if raw else 0
    except (ValueError, IndexError):
        return None


def _free_bytes(path: Path) -> int | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def human_bytes(value: int | None) -> str:
    """Signed, so a delta reads as one. `None` when it was not measured."""
    if value is None:
        return "unmeasured"
    sign = "-" if value < 0 else ""
    size = float(abs(value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            precision = 0 if unit == "B" else 1
            return f"{sign}{size:.{precision}f} {unit}"
        size /= 1024
    return f"{sign}{size:.1f} TiB"  # unreachable; keeps the type checker honest


# -------------------------------------------------------------- the worktrees


@dataclass(frozen=True)
class Worktree:
    """One record from `git worktree list --porcelain`."""

    path: Path
    head: str
    branch: str | None  # short name; None when detached or bare
    primary: bool
    bare: bool = False
    locked: str | None = None
    prunable: str | None = None

    @property
    def home(self) -> Path:
        return self.path / HOME_DIR

    @property
    def missing(self) -> bool:
        """The admin entry survives a directory somebody deleted by hand."""
        return not self.path.is_dir()

    @property
    def ticket(self) -> str | None:
        if not self.branch:
            return None
        match = ctx_mod._TICKET_RE.match(self.branch)
        return match.group(1) if match else None

    @property
    def epic_slug(self) -> str | None:
        if not self.branch:
            return None
        match = ctx_mod._EPIC_RE.match(self.branch)
        return match.group(1) if match else None

    #: `branch` when there is one, else the recorded HEAD — whatever the
    #: reachability probes below can name this worktree's tip by from the
    #: common repository, which still works after the directory is gone.
    @property
    def rev(self) -> str:
        return self.branch or self.head


def list_worktrees(start: str | Path = ".") -> list[Worktree]:
    """Every worktree of this repository. The primary is always first.

    That ordering is git's own and `publish._parent_home` already relies
    on it; it is what lets the primary checkout be identified without a
    second git call.
    """
    listing = _out("worktree", "list", "--porcelain", cwd=start)
    if not listing:
        return []
    records: list[Worktree] = []
    fields: dict[str, str | None] = {}

    def flush() -> None:
        if not fields.get("worktree"):
            return
        branch = fields.get("branch")
        if branch and branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        records.append(
            Worktree(
                path=Path(str(fields["worktree"])),
                head=fields.get("HEAD") or "",
                branch=branch,
                primary=not records,
                bare="bare" in fields,
                locked=fields.get("locked"),
                prunable=fields.get("prunable"),
            )
        )
        fields.clear()

    for line in listing.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        fields[key] = value.strip() or ""
    flush()
    return records


def primary_checkout(start: str | Path = ".") -> Path | None:
    """The main working tree — NOT `$PWD`'s nearest git toplevel.

    Run from inside a linked worktree those are different paths, and
    every destination this module derives hangs off this one.
    """
    trees = list_worktrees(start)
    return trees[0].path if trees else None


def current_worktree(start: str | Path = ".") -> Path | None:
    top = _out("rev-parse", "--show-toplevel", cwd=start)
    return Path(top) if top else None


# ------------------------------------------------------------ the target ref


def resolve_epic_ref(root: Path, slug: str) -> str | None:
    """`epic/<slug>` locally, else on any remote. None when unknown."""
    for candidate in (f"epic/{slug}", f"origin/epic/{slug}"):
        if _out("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", cwd=root):
            return candidate
    refs = _out(
        "for-each-ref", "--format=%(refname:short)", f"refs/remotes/*/epic/{slug}", cwd=root
    )
    if refs:
        return refs.splitlines()[0].strip()
    return None


def discover_epic_slug(root: Path) -> str | None:
    """The epic this checkout is about, if exactly one is discoverable.

    Same derivation as `context.gather`: the current branch when it is an
    `epic/*`, else any `epic/*` ref local or remote — a fresh clone has
    no local one until somebody checks it out.
    """
    branch = _out("branch", "--show-current", cwd=root) or ""
    match = ctx_mod._EPIC_RE.match(branch)
    if match:
        return match.group(1)
    refs = _out(
        "for-each-ref", "--format=%(refname:short)",
        "refs/heads/epic/*", "refs/remotes/*/epic/*", cwd=root,
    )
    slugs = []
    for ref in (refs or "").splitlines():
        head, _, tail = ref.partition("epic/")
        if tail and tail not in slugs:
            slugs.append(tail)
    return slugs[0] if len(slugs) == 1 else None


def epic_ticket_ids(root: Path, epic_ref: str) -> set[str] | None:
    """The ticket ids in the epic branch's shared plan, read from the ref.

    `git show <ref>:<path>` rather than a file read: the plan lives on
    the epic branch, and the primary checkout that runs the sweep is
    usually sitting on `main`. None means no plan could be read, which
    makes `--epic` fall back to a name match.
    """
    text = _out("show", f"{epic_ref}:{TICKET_PLAN}", cwd=root)
    if not text:
        return None
    _, _, all_tickets = ctx_mod.parse_ticket_plan(text)
    return set(all_tickets) or None


# ----------------------------------------------------------------- the status


@dataclass(frozen=True)
class Status:
    """Everything about one worktree that decides whether it may go."""

    dirty: tuple[str, ...] = ()
    stashes: tuple[str, ...] = ()
    upstream: str | None = None
    unpushed: int | None = None
    target: str | None = None
    not_in_target: int | None = None
    missing: bool = False
    locked: str | None = None
    prunable: str | None = None
    unmeasured: tuple[str, ...] = ()
    #: False when the repository has NO remote at all. "Unpushed" is then
    #: a question with no meaning — and the branch ref outlives the
    #: worktree either way — so it is reported and not enforced.
    remotes: bool = True

    @property
    def blockers(self) -> tuple[str, ...]:
        """Ordered most-actionable first; each names what to do about it."""
        out: list[str] = []
        if self.locked is not None:
            reason = f": {self.locked}" if self.locked else ""
            out.append(f"locked{reason} — `git worktree unlock` first")
        if self.dirty:
            shown = ", ".join(self.dirty[:MAX_REASON_ITEMS])
            more = f" +{len(self.dirty) - MAX_REASON_ITEMS} more" if len(self.dirty) > MAX_REASON_ITEMS else ""
            out.append(f"{len(self.dirty)} uncommitted path(s): {shown}{more}")
        if self.stashes:
            out.append(
                f"{len(self.stashes)} stash entr{'y' if len(self.stashes) == 1 else 'ies'} "
                f"made here: {', '.join(self.stashes[:MAX_REASON_ITEMS])}"
            )
        if self.unpushed and self.remotes:
            where = self.upstream or "any remote"
            out.append(f"{self.unpushed} commit(s) not pushed to {where}")
        if self.not_in_target:
            out.append(f"{self.not_in_target} commit(s) not contained in {self.target}")
        # An evidence gap is not a clean bill of health. `_local_state`
        # makes the same call for `unknown`: at a gate whose job is to
        # refuse while work still exists, "could not tell" must never
        # read as "nothing here".
        for probe in self.unmeasured:
            out.append(f"could not determine {probe} — refusing to guess")
        return tuple(out)

    @property
    def warnings(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.target is None:
            out.append(
                "no epic/target branch known, so containment was not checked "
                "(pass --epic <slug> or --target <ref>)"
            )
        if not self.remotes:
            out.append(
                f"this repository has no remote, so `pushed` has no meaning here — "
                f"{self.unpushed or 0} commit(s) exist only in this clone, and its "
                "branch ref outlives the worktree"
            )
        return tuple(out)

    @property
    def clean(self) -> bool:
        return not self.blockers


def _stash_entries(root: Path, branch: str | None) -> tuple[tuple[str, ...], bool]:
    """Stash entries attributable to `branch`. (entries, measured).

    See `_STASH_SUBJECT`: the stash is shared between worktrees, so the
    branch recorded in each entry's reflog subject is the only thing
    that says which worktree made it.
    """
    if not _out("rev-parse", "--verify", "--quiet", "refs/stash", cwd=root):
        return (), True  # no stash ref at all: nothing to attribute
    proc = _git("log", "-g", "--format=%gd\t%gs", "refs/stash", cwd=root)
    if proc is None or proc.returncode != 0:
        return (), False
    entries: list[str] = []
    for line in proc.stdout.splitlines():
        name, _, subject = line.partition("\t")
        match = _STASH_SUBJECT.match(subject.strip())
        made_on = match.group("branch") if match else None
        if branch is None:
            # A detached worktree can only own an unattributable entry —
            # and cannot prove it does not, so it blocks.
            if made_on is None or made_on == _STASH_NO_BRANCH:
                entries.append(name or subject.strip())
        elif made_on == branch:
            entries.append(name or subject.strip())
    return tuple(entries), True


def inspect(wt: Worktree, *, root: Path, target: str | None) -> Status:
    """Measure one worktree NOW.

    Called for the plan and then AGAIN immediately before that worktree
    is removed, because a pass over a dozen homes takes minutes and the
    first answer describes the past. Every probe is bounded and an
    unbounded-time failure lands in `unmeasured`, which blocks.
    """
    unmeasured: list[str] = []
    dirty: tuple[str, ...] = ()
    if not wt.missing:
        porcelain = _out("status", "--porcelain", cwd=wt.path)
        if porcelain is None:
            unmeasured.append("the working tree state")
        elif porcelain:
            dirty = tuple(
                line.strip()
                for line in porcelain.splitlines()
                if line.strip() and not _is_home_path(line)
            )

    stashes, measured = _stash_entries(root, wt.branch)
    if not measured:
        unmeasured.append("the stash")

    remotes = bool(_out("remote", cwd=root))
    upstream = _out(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{wt.rev}@{{upstream}}", cwd=root
    ) or None
    if upstream:
        unpushed = _count("rev-list", "--count", f"{upstream}..{wt.rev}", cwd=root)
    else:
        # No upstream is not "nothing to push": ask what this tip holds
        # that no remote-tracking ref does. Cheap, and it is the honest
        # generalisation of the question.
        unpushed = _count("rev-list", "--count", wt.rev, "--not", "--remotes", cwd=root)
    if unpushed is None:
        unmeasured.append("whether its commits are pushed")

    not_in_target = None
    if target is not None:
        not_in_target = _count("rev-list", "--count", f"{target}..{wt.rev}", cwd=root)
        if not_in_target is None:
            unmeasured.append(f"whether its commits are contained in {target}")

    return Status(
        dirty=dirty,
        stashes=stashes,
        upstream=upstream,
        unpushed=unpushed,
        target=target,
        not_in_target=not_in_target,
        missing=wt.missing,
        locked=wt.locked,
        prunable=wt.prunable,
        unmeasured=tuple(unmeasured),
        remotes=remotes,
    )


def _is_home_path(porcelain_line: str) -> bool:
    """Does this `status --porcelain` line describe the worktree's home?

    See :data:`HOME_DIR`. Porcelain lines are `XY <path>` with a possible
    `-> <path>` rename tail, and a quoted path when it needs escaping.
    """
    body = porcelain_line[2:].strip().strip('"')
    body = body.split(" -> ")[-1].strip().strip('"')
    return body == HOME_DIR or body.startswith(HOME_DIR + "/")


# ------------------------------------------------------------------- the gate


@dataclass(frozen=True)
class Verdict:
    """What `skill-manager home close-out` established, if anything."""

    ran: bool
    clean: bool
    exit_code: int | None = None
    reason: str = ""
    fix: str = ""
    blockers: tuple[str, ...] = ()
    #: This is OUR fault or a broken home, not a safety skip — see the
    #: module docstring on gate exit 2.
    fault: bool = False
    #: A statement about the DESTINATION, identical for every worktree.
    frozen: bool = False


def _tail(proc: subprocess.CompletedProcess, limit: int = 400) -> str:
    blob = ((proc.stderr or "") + (proc.stdout or "")).strip()
    line = next((ln.strip() for ln in reversed(blob.splitlines()) if ln.strip()), "")
    return line[:limit]


def _render_blockers(payload: dict) -> tuple[str, ...]:
    """The CLI's own remedy strings, verbatim.

    close-change.sh carries the scar that motivates "verbatim": a regex
    over someone else's sentence rewrote the conflicted-file list — where
    `skill-manager.toml`, the most commonly conflicted file, matched the
    token — into a path in a different repository. The CLI owns the
    remedy; a second opinion here is a second thing to keep in step.
    """
    out: list[str] = []
    for entry in payload.get("blockers") or []:
        if not isinstance(entry, dict):
            continue
        line = f"{entry.get('unit', '?')} ({entry.get('status', '?')})"
        detail = entry.get("detail")
        if detail:
            line += f" — {detail}"
        for conflict in (entry.get("conflicts") or [])[:MAX_REASON_ITEMS]:
            line += f"\n    conflict  {conflict}"
        remedy = entry.get("remedy")
        if remedy:
            line += f"\n    run: {remedy}"
        out.append(line)
    return tuple(out)


def _verdict_says_unsafe(payload: dict) -> bool:
    """Did the verdict document itself say no?

    Two spellings, measured against the shipped CLI: the live payload
    carries `safe` (with `exitCode`, `units`, `blockers`), and its
    `not_a_home` refusal carries BOTH `safe` and `clean`. `clean` is also
    what git-issue-workflow's `complete.md` documents. Reading only one of
    them would make a refusal from the other spelling read as consent, so
    both are honoured and neither is required.
    """
    return payload.get("clean") is False or payload.get("safe") is False


def cli_has_close_out(cli: Path) -> bool:
    """Does this CLI answer `home close-out --into`?

    Probed the way close-change.sh probes it — on the flag, not on a
    version — because a pin that predates the gate would otherwise
    "succeed" at running it.
    """
    proc = _run(
        [str(cli), "home", "close-out", "--help"],
        timeout=READ_TIMEOUT_SECONDS,
        env=_cli_env(),
    )
    if proc is None:
        return False
    return "--into" in ((proc.stdout or "") + (proc.stderr or ""))


def resolve_gate_cli(destination: Path, *also: Path) -> tuple[Path | None, str]:
    """A skill-manager that can answer the gate. (cli, why-not).

    The destination home's own pin first — it is the home the verdict is
    about — then any other home the caller offers (the primary
    checkout's, which is the destination unless `--into` moved it), then
    whatever is on PATH. Which executable runs the gate does not change
    the verdict: `--home` and `--into` are what the verdict is about.

    A sweep with NO such CLI refuses outright rather than removing
    anything. close-change.sh states the rule and it is the right one: a
    gate that cannot run has established nothing, and "nothing
    established" must never be spent as "safe".
    """
    candidates: list[Path] = []
    pin = _cli(destination)
    for home in (destination, *also):
        candidate = _cli(home)
        if candidate.is_file() and candidate not in candidates:
            candidates.append(candidate)
    on_path = shutil.which("skill-manager")
    if on_path and Path(on_path) not in candidates:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if cli_has_close_out(candidate):
            return candidate, ""
    if not candidates:
        return None, (
            f"no skill-manager CLI found — {pin} does not exist and none is on PATH"
        )
    return None, (
        "no skill-manager with a `home close-out` subcommand was found (tried: "
        + ", ".join(str(c) for c in candidates)
        + ")"
    )


def close_out(cli: Path, home: Path, destination: Path) -> Verdict:
    """Run the home gate for one worktree. Writes nothing; re-runnable."""
    if not home.is_dir():
        # Nothing to destroy. A worktree made by a bare `git worktree
        # add` has no home, and saying "gate refused" about a directory
        # that does not exist would be a lie in the safe direction that
        # still stops a legitimate sweep.
        return Verdict(ran=False, clean=True, reason=f"no Skill Manager home at {home}")
    argv = [
        str(cli), "home", "close-out",
        "--home", str(home),
        "--into", str(destination),
        "--json",
    ]
    proc = _run(argv, timeout=GATE_TIMEOUT_SECONDS, env=_cli_env())
    if proc is None:
        return Verdict(
            ran=True, clean=False,
            reason=f"the home gate did not finish inside {GATE_TIMEOUT_SECONDS:.0f}s",
            fix=" ".join(argv[:-1]),
        )
    # Decoded BEFORE the exit codes are classified, because the CLI's own
    # `not_a_home` payload carries the best sentence anybody could write
    # about it — including the corrected path. Re-deriving that message
    # here would be a worse copy of one already in hand.
    payload: dict = {}
    if proc.stdout.strip():
        try:
            decoded = json.loads(proc.stdout)
            payload = decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            payload = {}
    if proc.returncode == GATE_EXIT_NOT_A_HOME:
        told = str(payload.get("message") or "").strip()
        return Verdict(
            ran=True, clean=False, exit_code=proc.returncode, fault=True,
            reason=(
                f"skill-manager says {home} is not a Skill Manager home (exit 2), so "
                "NOTHING about this worktree was assessed"
                + (f" — {told}" if told else "")
            ),
            fix=(
                "check that path is the home and not the worktree directory — exit 2 is "
                "the one standing between a typo and a silent 'safe' verdict"
            ),
        )
    if proc.returncode == GATE_EXIT_FROZEN:
        return Verdict(
            ran=True, clean=False, exit_code=proc.returncode, frozen=True,
            reason=(
                f"the destination home {destination} has policy `frozen`, so the gate was "
                "refused and nothing was attempted"
            ),
            fix=f"skill-manager home policy live --home {destination}   # or pass --into <home>",
        )
    if not payload:
        # close-change.sh's rule: an empty verdict establishes nothing,
        # whatever the exit code was. `--json` swallowed the reason, so
        # the fix is to re-run it without.
        return Verdict(
            ran=True, clean=False, exit_code=proc.returncode,
            reason=f"the home gate produced no verdict (exit {proc.returncode}); {_tail(proc)}".strip("; "),
            fix=" ".join(argv[:-1]),
        )
    if payload.get("error"):
        return Verdict(
            ran=True, clean=False, exit_code=proc.returncode, fault=True,
            reason=str(payload.get("message") or payload["error"]),
            fix=" ".join(argv[:-1]),
        )
    blockers = _render_blockers(payload)
    clean = proc.returncode == 0 and not _verdict_says_unsafe(payload) and not blockers
    if clean:
        return Verdict(ran=True, clean=True, exit_code=0, reason="home gate clean")
    return Verdict(
        ran=True, clean=False, exit_code=proc.returncode,
        reason=(
            f"the home gate refused: this worktree's home still holds work that removing "
            f"it would destroy ({len(blockers)} blocker(s))"
            if blockers else "the home gate refused"
        ),
        blockers=blockers,
        fix=" ".join(argv[:-1]),
    )


# ------------------------------------------------------------------- the plan


@dataclass
class Candidate:
    worktree: Worktree
    status: Status
    #: Why this worktree is not a candidate at all, if it is not.
    excluded: str | None = None


@dataclass
class Plan:
    root: Path
    destination: Path | None
    target: str | None
    epic: str | None
    current: Path | None
    #: True when `--epic` was passed, so the candidate set was NARROWED.
    #: A discovered epic sets `target` only. See `_matches_epic`.
    scoped: bool = False
    candidates: list[Candidate] = field(default_factory=list)
    error: str | None = None
    fix: str = ""

    @property
    def sweepable(self) -> list[Candidate]:
        return [c for c in self.candidates if c.excluded is None]


def _matches_epic(
    wt: Worktree, slug: str, epic_ref: str | None, tickets: set[str] | None, root: Path
) -> bool:
    """Is this worktree part of epic `slug`?

    Four answers, strongest first, because there is no single fact in git
    that says "this worktree belongs to that epic":

    1. it IS the epic's own worktree (`epic/<slug>` checked out);
    2. its ticket id is in the epic branch's shared ticket plan. That IS
       the question, so when a plan can be read it is the whole answer
       and nothing below widens it;
    3. the slug appears in the branch name or the worktree directory
       name — the epic workflow's declared worktree paths usually spell
       it out;
    4. the epic ref is an ancestor of the worktree's tip, i.e. it was
       branched from the epic at or after the epic's current tip.

    (4) is the weak one and is honest about it: a ticket branched from an
    OLD epic tip that the epic has since moved past will not match, and a
    repository whose epic branch has not yet diverged from `main` will
    match everything. That is why `--epic` narrows only when it is passed
    explicitly — a discovered slug sets the containment target and never
    shrinks the candidate set, so an imprecise guess can only ever make
    the sweep MORE careful, never less.
    """
    if wt.epic_slug == slug:
        return True
    if tickets is not None and wt.ticket is not None:
        return wt.ticket in tickets
    needle = slug.lower()
    if needle in (wt.branch or "").lower() or needle in wt.path.name.lower():
        return True
    if epic_ref is not None:
        proc = _git("merge-base", "--is-ancestor", epic_ref, wt.rev, cwd=root)
        return proc is not None and proc.returncode == 0
    return False


def build_plan(
    *,
    start: str | Path = ".",
    epic: str | None = None,
    target: str | None = None,
    into: str | Path | None = None,
) -> Plan:
    """Enumerate and measure. Read-only: this is what `list` prints."""
    trees = list_worktrees(start)
    if not trees:
        return Plan(
            root=Path(start).resolve(), destination=None, target=None, epic=epic,
            current=None,
            error="not inside a git repository, or git could not list its worktrees",
            fix="cd into the primary checkout of the repository whose worktrees you want",
        )
    root = trees[0].path
    current = current_worktree(start)

    slug = epic or discover_epic_slug(root)
    epic_ref = resolve_epic_ref(root, slug) if slug else None
    if epic and epic_ref is None:
        return Plan(
            root=root, destination=None, target=None, epic=epic, current=current,
            error=f"no branch epic/{epic} exists locally or on any remote",
            fix="git fetch origin, then pass --epic <slug> exactly as the branch spells it",
        )
    resolved_target = target or epic_ref

    if into is not None:
        destination = Path(into).expanduser().resolve()
    else:
        # THE MAIN WORKING TREE'S home, never `homes.find_home(".")` —
        # see the module docstring.
        destination = root / HOME_DIR

    tickets = epic_ticket_ids(root, epic_ref) if epic_ref else None
    plan = Plan(
        root=root, destination=destination, target=resolved_target, epic=slug,
        current=current, scoped=epic is not None,
    )
    for wt in trees:
        excluded: str | None = None
        if wt.primary:
            excluded = "the primary checkout — never removed"
        elif wt.bare:
            excluded = "a bare worktree — never removed"
        elif current is not None and wt.path.resolve() == current.resolve():
            excluded = (
                "the worktree this command is running IN — run the sweep from the "
                f"primary checkout instead: git -C {root} … / cd {root}"
            )
        elif epic and not _matches_epic(wt, epic, epic_ref, tickets, root):
            # Only an EXPLICIT --epic narrows. A DISCOVERED slug sets the
            # containment target and nothing else: silently sweeping a
            # subset because one epic branch happened to exist is the
            # kind of surprise a destructive command must not have.
            excluded = f"not part of epic {epic}"
        status = inspect(wt, root=root, target=resolved_target) if excluded is None else Status(
            missing=wt.missing, locked=wt.locked, prunable=wt.prunable, target=resolved_target
        )
        plan.candidates.append(Candidate(worktree=wt, status=status, excluded=excluded))
    return plan


# ------------------------------------------------------------------ the sweep


@dataclass
class Step:
    ticket: str | None
    path: Path
    branch: str | None
    #: planned | removed | skipped | failed | excluded
    action: str
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    fix: str = ""


@dataclass
class SweepResult:
    plan: Plan
    dry_run: bool
    steps: list[Step] = field(default_factory=list)
    pruned: bool = False
    prune_error: str = ""
    free_before: int | None = None
    free_after: int | None = None
    abandoned: str = ""
    #: Sweepable worktrees the abandoned pass never reached. Reported so a
    #: summary after an abandonment is not silently short.
    not_reached: int = 0
    exit_code: int = EXIT_OK

    @property
    def free_delta(self) -> int | None:
        if self.free_before is None or self.free_after is None:
            return None
        return self.free_after - self.free_before

    def count(self, action: str) -> int:
        return sum(1 for s in self.steps if s.action == action)


def sweep(
    *,
    start: str | Path = ".",
    epic: str | None = None,
    target: str | None = None,
    into: str | Path | None = None,
    yes: bool = False,
) -> SweepResult:
    """Retire every sweepable worktree that passes its own gate.

    Refuses by default: without `yes` this returns the plan with every
    sweepable worktree marked `planned` and nothing touched.
    """
    plan = build_plan(start=start, epic=epic, target=target, into=into)
    result = SweepResult(plan=plan, dry_run=not yes)
    if plan.error:
        result.exit_code = EXIT_FAILED
        return result
    for candidate in plan.candidates:
        if candidate.excluded is not None:
            result.steps.append(
                Step(
                    ticket=candidate.worktree.ticket,
                    path=candidate.worktree.path,
                    branch=candidate.worktree.branch,
                    action="excluded",
                    reasons=(candidate.excluded,),
                )
            )
    if result.dry_run:
        for candidate in plan.sweepable:
            status = candidate.status
            result.steps.append(
                Step(
                    ticket=candidate.worktree.ticket,
                    path=candidate.worktree.path,
                    branch=candidate.worktree.branch,
                    action="planned" if status.clean else "skipped",
                    reasons=status.blockers,
                    notes=status.warnings + _prunable_note(candidate.worktree),
                )
            )
        return result

    if plan.destination is None or not plan.destination.is_dir():
        plan.error = f"the destination home does not exist at {plan.destination}"
        plan.fix = (
            f"the main working tree {plan.root} has no home — bootstrap it, or pass "
            "--into <home>"
        )
        result.exit_code = EXIT_FAILED
        return result
    cli, why_not = resolve_gate_cli(plan.destination, plan.root / HOME_DIR)
    if cli is None:
        # A gate that cannot run has established nothing, and "nothing
        # established" must never be spent as "safe".
        plan.error = f"{why_not}, so the home gate cannot run and nothing was removed"
        plan.fix = "skt sync skill-manager   # then re-run"
        result.exit_code = EXIT_FAILED
        return result

    result.free_before = _free_bytes(plan.root)
    queue = plan.sweepable
    for index, candidate in enumerate(queue):
        wt = candidate.worktree
        step = Step(ticket=wt.ticket, path=wt.path, branch=wt.branch, action="skipped")
        # RE-MEASURED here, immediately before this worktree is removed.
        # The plan above is minutes old by now.
        status = inspect(wt, root=plan.root, target=plan.target)
        candidate.status = status
        step.notes = status.warnings + _prunable_note(wt)
        if not status.clean:
            step.reasons = status.blockers
            result.steps.append(step)
            continue
        verdict = close_out(cli, wt.home, plan.destination)
        if verdict.frozen:
            step.action = "skipped"
            step.reasons = (verdict.reason,)
            step.fix = verdict.fix
            result.steps.append(step)
            result.abandoned = verdict.reason
            result.not_reached = len(queue) - index - 1
            result.exit_code = EXIT_FROZEN_DESTINATION
            break
        if not verdict.clean:
            step.action = "failed" if verdict.fault else "skipped"
            step.reasons = (verdict.reason, *verdict.blockers)
            step.fix = verdict.fix
            result.steps.append(step)
            continue
        if not verdict.ran:
            step.notes += (verdict.reason,)
        removed = _run(
            ["git", "worktree", "remove", str(wt.path)],
            cwd=plan.root,
            timeout=REMOVE_TIMEOUT_SECONDS,
        )
        if removed is None or removed.returncode != 0:
            step.action = "failed"
            detail = _tail(removed) if removed is not None else "git did not return"
            step.reasons = (f"git worktree remove refused: {detail}",)
            step.fix = (
                f"git -C {plan.root} worktree remove {wt.path}   # run it yourself to see why "
                "— --force is NOT the answer here"
            )
            result.steps.append(step)
            continue
        step.action = "removed"
        kept = (
            f"branch {wt.branch} kept — delete it once the change has landed"
            if wt.branch
            else "detached HEAD; there was no branch to keep"
        )
        step.notes += (kept,)
        result.steps.append(step)

    pruned = _run(["git", "worktree", "prune"], cwd=plan.root, timeout=READ_TIMEOUT_SECONDS)
    result.pruned = pruned is not None and pruned.returncode == 0
    if not result.pruned:
        result.prune_error = _tail(pruned) if pruned is not None else "git did not return"
    result.free_after = _free_bytes(plan.root)
    if result.exit_code == EXIT_OK and result.count("failed"):
        result.exit_code = EXIT_FAILED
    return result


def _prunable_note(wt: Worktree) -> tuple[str, ...]:
    if wt.prunable is not None:
        return (f"prunable: {wt.prunable or 'git says so'} — `git worktree prune` clears the entry",)
    if wt.missing:
        return ("the directory is gone; only git's admin entry remains",)
    return ()


# ---------------------------------------------------------------- rendering


def _label(wt_ticket: str | None) -> str:
    return (wt_ticket or "-")[:16].ljust(16)


def _header(plan: Plan) -> list[str]:
    """The three facts a reader has to check before trusting the rest."""
    if plan.epic is None:
        epic = "epic       none discoverable"
    else:
        scope = "scoped to it" if plan.scoped else "discovered; the candidate set is NOT narrowed"
        epic = f"epic       {plan.epic} ({scope})"
    target = (
        f"target     {plan.target} — commits must be contained in it"
        if plan.target
        else "target     none — containment NOT checked (pass --epic <slug> or --target <ref>)"
    )
    return [epic, target, f"home       close-out destination: {plan.destination}"]


def render_list(plan: Plan) -> str:
    if plan.error:
        lines = [f"error: {plan.error}"]
        if plan.fix:
            lines.append(f"fix:   {plan.fix}")
        return "\n".join(lines)
    lines = [f"skt ticket list — {plan.root}", *_header(plan)]
    linked = [c for c in plan.candidates if not c.worktree.primary]
    if not linked:
        lines.append("  no linked worktrees — nothing to sweep")
        return "\n".join(lines)
    for candidate in plan.candidates:
        wt, status = candidate.worktree, candidate.status
        flags = []
        if wt.primary:
            flags.append("primary")
        if plan.current is not None and wt.path.resolve() == plan.current.resolve():
            flags.append("current")
        if wt.missing:
            flags.append("MISSING")
        if wt.prunable is not None:
            flags.append("prunable")
        if wt.locked is not None:
            flags.append("locked")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {_label(wt.ticket)} {wt.path}{suffix}")
        lines.append(f"      branch  {wt.branch or '(detached)'} @{(wt.head or '')[:8]}")
        if candidate.excluded is not None:
            lines.append(f"      excluded  {candidate.excluded}")
            continue
        if status.clean:
            lines.append("      clean   no blocker — this worktree may be retired")
        for blocker in status.blockers:
            lines.append(f"      BLOCKED {blocker}")
        for warning in status.warnings:
            lines.append(f"      note    {warning}")
        for note in _prunable_note(wt):
            lines.append(f"      note    {note}")
    sweepable = plan.sweepable
    lines.append("")
    lines.append(
        f"{len(sweepable)} sweepable, {sum(1 for c in sweepable if c.status.clean)} with no "
        f"blocker, {len(plan.candidates) - len(sweepable)} excluded"
    )
    lines.append("next       skt ticket sweep            # the plan, nothing removed")
    lines.append("           skt ticket sweep --yes      # retire the unblocked ones")
    return "\n".join(lines)


def render_sweep(result: SweepResult) -> str:
    plan = result.plan
    if plan.error:
        lines = [f"error: {plan.error}"]
        if plan.fix:
            lines.append(f"fix:   {plan.fix}")
        return "\n".join(lines)
    lines = [
        f"skt ticket sweep{' (dry run)' if result.dry_run else ''} — {plan.root}",
        *_header(plan),
    ]
    mark = {
        "planned": "would remove",
        "removed": "removed     ",
        "skipped": "SKIPPED     ",
        "failed": "FAILED      ",
        "excluded": "excluded    ",
    }
    for step in result.steps:
        lines.append(f"  {mark.get(step.action, step.action)} {_label(step.ticket)} {step.path}")
        for reason in step.reasons:
            # A gate blocker is multi-line by construction (its conflict
            # list and its remedy), and the CLI's own indentation inside
            # it is preserved rather than re-flowed.
            lines.extend(f"      {part}" for part in reason.splitlines())
        if step.fix:
            lines.append(f"      fix: {step.fix}")
        for note in step.notes:
            lines.append(f"      note: {note}")
    lines.append("")
    if result.dry_run:
        planned = result.count("planned")
        lines.append(
            f"{planned} would be removed, {result.count('skipped')} skipped for safety, "
            f"{result.count('excluded')} excluded"
        )
        lines.append(
            "dry run — NOTHING was removed. Re-run with --yes to retire the "
            f"{planned} worktree(s) marked `would remove`; every check above is re-run "
            "immediately before each removal."
        )
        # Said out loud, because `would remove` otherwise reads as a
        # promise the dry run has not actually established. The gate is
        # the slow half (a full compare of two homes, per worktree) and a
        # dry run that paid for it would not be one people ran.
        lines.append(
            "The `skill-manager home close-out` gate is NOT run by a dry run, so a "
            "`would remove` row can still be skipped for unpublished skill work in its home."
        )
        return "\n".join(lines)
    lines.append(
        f"{result.count('removed')} removed, {result.count('skipped')} skipped for safety, "
        f"{result.count('failed')} failed, {result.count('excluded')} excluded"
    )
    if not result.pruned:
        lines.append(f"git worktree prune did not run cleanly: {result.prune_error}")
    lines.append(
        f"free space {human_bytes(result.free_delta)} "
        f"({human_bytes(result.free_before)} -> {human_bytes(result.free_after)} on {plan.root})"
    )
    lines.append(
        "That delta is the only size reported, deliberately: these homes are cloned "
        "copy-on-write, so `du` bills every shared block to every copy and over-reports "
        "by ~30x (a home `du` called 1.1 GB cost 33.7 MB of real space)."
    )
    if result.abandoned:
        lines.append(
            f"pass ABANDONED: {result.abandoned}"
            + (f" — {result.not_reached} further worktree(s) were never assessed"
               if result.not_reached else "")
        )
    if result.count("skipped"):
        lines.append(
            "Skipped is a normal outcome, not an error: clear each blocker above and "
            "re-run. `--force` is never passed here and is not the remedy."
        )
    return "\n".join(lines)


def _status_json(status: Status) -> dict:
    return {
        "clean": status.clean,
        "dirty": list(status.dirty),
        "stashes": list(status.stashes),
        "upstream": status.upstream,
        "unpushed": status.unpushed,
        "target": status.target,
        "not_in_target": status.not_in_target,
        "missing": status.missing,
        "locked": status.locked,
        "prunable": status.prunable,
        "unmeasured": list(status.unmeasured),
        "remotes": status.remotes,
        "blockers": list(status.blockers),
        "warnings": list(status.warnings),
    }


def list_json(plan: Plan) -> dict:
    return {
        "schema": 1,
        "root": str(plan.root),
        "destination_home": str(plan.destination) if plan.destination else None,
        "epic": plan.epic,
        "epic_scoped": plan.scoped,
        "target": plan.target,
        "current": str(plan.current) if plan.current else None,
        "error": plan.error,
        "fix": plan.fix or None,
        "worktrees": [
            {
                "path": str(c.worktree.path),
                "ticket": c.worktree.ticket,
                "branch": c.worktree.branch,
                "head": c.worktree.head,
                "primary": c.worktree.primary,
                "excluded": c.excluded,
                "status": _status_json(c.status),
            }
            for c in plan.candidates
        ],
    }


def sweep_json(result: SweepResult) -> dict:
    return {
        "schema": 1,
        "root": str(result.plan.root),
        "destination_home": (
            str(result.plan.destination) if result.plan.destination else None
        ),
        "epic": result.plan.epic,
        "epic_scoped": result.plan.scoped,
        "target": result.plan.target,
        "dry_run": result.dry_run,
        "error": result.plan.error,
        "fix": result.plan.fix or None,
        "abandoned": result.abandoned or None,
        "not_reached": result.not_reached,
        "exit_code": result.exit_code,
        "summary": {
            "planned": result.count("planned"),
            "removed": result.count("removed"),
            "skipped": result.count("skipped"),
            "failed": result.count("failed"),
            "excluded": result.count("excluded"),
        },
        "pruned": result.pruned,
        "free_bytes_before": result.free_before,
        "free_bytes_after": result.free_after,
        "free_bytes_delta": result.free_delta,
        "size_note": (
            "no per-worktree size is reported: the homes are cloned copy-on-write, so "
            "`du` over-reports by ~30x. free_bytes_delta is real space."
        ),
        "steps": [
            {
                "ticket": s.ticket,
                "path": str(s.path),
                "branch": s.branch,
                "action": s.action,
                "reasons": list(s.reasons),
                "notes": list(s.notes),
                "fix": s.fix or None,
            }
            for s in result.steps
        ],
    }


# ------------------------------------------------------------------ the verbs


def run_list(
    *,
    start: str | Path = ".",
    epic: str | None = None,
    target: str | None = None,
    into: str | Path | None = None,
    as_json: bool = False,
) -> int:
    plan = build_plan(start=start, epic=epic, target=target, into=into)
    print(json.dumps(list_json(plan), indent=2) if as_json else render_list(plan))
    return EXIT_FAILED if plan.error else EXIT_OK


def run_sweep(
    *,
    start: str | Path = ".",
    epic: str | None = None,
    target: str | None = None,
    into: str | Path | None = None,
    yes: bool = False,
    as_json: bool = False,
) -> int:
    result = sweep(start=start, epic=epic, target=target, into=into, yes=yes)
    print(json.dumps(sweep_json(result), indent=2) if as_json else render_sweep(result))
    return result.exit_code
