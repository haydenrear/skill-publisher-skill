"""`skt check` — new-version, sync-with-root, unit-error and stale-artifact.

Pull-side (every tier): compare each change-managed unit's installed
gitHash against its remote tip (`git ls-remote`). Push-side (ROOT tier
only): a unit's store checkout that is dirty or ahead of its remote is
work nobody else can see — prompt the publish. Project homes are
updated from `wt`-created imports, so they get pull-side messages only.

Both of those are inferences from hashes, and the home does not only
carry hashes: the installer records `errors[*].kind` when it leaves a
store in a state it could not finish. For the three kinds that describe
the STORE CHECKOUT itself (`STORE_BLOCKING_ERRORS`), that record is the
explanation for the disagreement the hash comparison would otherwise
diagnose on its own — so it is read FIRST, and it replaces both
verdicts rather than riding beside them.

The case this exists for, measured in the operator's project home:
three units record `MERGE_CONFLICT` with `stash@{0}` holding somebody's
uncommitted work, and `skt check` answered two of them with "new
version available — pull with: skt sync", which re-runs the merge that
made the conflict, and the third with "ahead of the remote tip", which
is true about hashes and silent about the unmerged paths. The remedy a
`unit-error` names is the one skill-manager's own `ReportUseCase.hint`
names — resolve in the store — never a skill-manager verb, because
`sync --merge` is documented as what SETS this state.

Artifact-side (every tier): a unit moving is only half the news. The
half that unblocks a fast fix is WHICH derived artifact went stale as a
result and the one command that lands it, so a third notification kind
rides beside the other two:

    artifact computeq is stale (deploy-helm moved a3f21c8 -> 9b17e40)
      rebuild with: skt build computeq

It is built ONLY from `skt.artifacts`' `rebuildable` set — an artifact
that is on disk, no longer describes its inputs, and that `skt build`
really produces. The operator's project home holds 55 stale artifacts
and 7 of those; 11 of the rest were declared and never built, which is a
lazily-built home's normal state and not news. Enumerating all 55 into
every session is the failure this filter exists to prevent, and the
counts for what it drops stay in the report under `artifacts`.

**The artifact probe is on the LIVE path and nowhere else.** It is a
subprocess, and `--cached` is contract-cache-only in every cache state
(below). `skt.artifacts` is imported INSIDE `collect()` rather than at
the top of this file, so nothing on the cached path can reach it — not
even by accident, because a module-scope import here would be circular
(`artifacts` -> `publish` -> `check`) and fail loudly at import time.

That is a statement about THIS module, not about the process. The real
hook path runs `cli.py`, which imports the `skt` package, whose
`__init__` re-exports the artifact surface — so `skt.artifacts` IS in
`sys.modules` by the time `check --cached` runs. Importing it costs no
spawn and no measurable time; what matters is that the cached path never
CALLS it, which is what the tests drive.

`--cached` is contract-cache-only: it reads the state file and NOTHING
else — no subprocess, no network, no fallback to a live check, whatever
state the cache is in. That is the path SKT-6 wires into tool-event
hooks, so a cold home must stay exactly as cheap as a warm one. The
report carries a typed `cache_state`:

  fresh    -> the cached result verbatim, plus `from_cache` metadata
  missing  -> no cache record exists; nothing was checked
  expired  -> a record exists but is past --ttl; its content is
              preserved under `stale` (text output labels each line
              [stale]) and is never presented as current

Exit codes: 0 = nothing to report; 10 = notifications exist. Both are
success — 10 exists so hooks can decide to inject without parsing.
`cache_state` missing/expired is the documented non-error outcome: exit
0 with empty top-level `notifications` (an absent result is "nothing to
report", and stale notifications must not re-fire as exit 10 on every
tool call). Refreshing is the live path's job — `skt check`, or the
SessionStart hook's bounded refresh — never the hook tool-event path's.

Both halves of the live path read the SAME clock. The remote tip comes
from `git ls-remote`; the local verdicts are then adjudicated against
that tip rather than against `@{upstream}`, which is a local ref only a
fetch moves. Units where the two agree and only the local ref disagrees
are reported under `upstream_stale` / `ahead_of_remote` and are NOT
notifications: a prompt to publish or pull with nothing behind it was
the defect. The adjudication is `merge-base --is-ancestor` in the store
checkout — local, clamped to the same deadline, no extra network — and
it runs only for units that would otherwise have produced a message.
`--cached` is untouched: it still costs exactly one state-file read.

The live path owns one wall-clock deadline (NETWORK_BUDGET_SECONDS),
shared by the remote and root-local phases: every git child runs in its
own process group and the whole group is SIGKILLed and reaped when its
clamped timeout fires, queued executor work is cancelled at the
deadline instead of awaited, and units left unresolved are reported
`unverifiable`. A refresh that resolved nothing writes no cache record,
and the record it does write lands by atomic rename — `--cached` must
never serve a failure, or a partial write, as a fresh success.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import shlex

from . import context as ctx_mod
from . import homes
from . import relay as relay_mod

# 2: the record gained `upstream_stale` and `ahead_of_remote`. Purely
# additive, and every reader uses `.get(...) or []`, so a v1 record still
# loads and nothing gates on the number — but the constant IS the
# record's description, and leaving it at 1 would have it describe a
# record that no longer exists.
# 3: the record gained `artifacts` (ARTI-10) and notifications of kind
# `stale-artifact`. Additive on the same terms: an older record has no
# `artifacts` key, `skt status` reads it with `.get`, and a reader that
# does not know the kind still gets a `message`.
# 4: notifications of kind `unit-error` (ARTI-23). Additive again — the
# kind carries `message` and `fix` like `stale-artifact` does — but a
# consumer filtering on `kind == "new-version"` sees FEWER rows than a v3
# record would have shown for the same home, because a store-blocking
# error now replaces that unit's pull prompt rather than riding beside it.
# 5: the record gained `cli` and notifications of kind `cli-version`
# (skill-manager#265's follow-on). Additive on the established terms — an
# older record has no `cli` key and every reader uses `.get` — and it fills
# the one gap that made every OTHER notification untrustworthy: skt could
# say a home's units were current while the binary reading them was a
# release behind, because skill-manager is a brew formula rather than a
# change-managed unit and nothing here had ever looked at it.
SCHEMA_VERSION = 5
DEFAULT_TTL_SECONDS = 900
NOTIFY_EXIT = 10
REMOTE_TIMEOUT_SECONDS = 10
NETWORK_BUDGET_SECONDS = 15  # total live wall budget (remote + root-local), under the SessionStart hook's 30s
LOCAL_TIMEOUT_SECONDS = 2  # per root-local git call — a hung `status` must not eat the whole budget

CACHE_FRESH = "fresh"
CACHE_MISSING = "missing"
CACHE_EXPIRED = "expired"

# Two verdicts that are NOT notifications: the store and the remote agree,
# and only a local ref disagrees. Reported so the state is visible and the
# `--json` consumer can see it, but never as work to do — a prompt to
# publish or pull that has nothing behind it is the defect, not the cure.
STATE_UPSTREAM_STALE = "upstream-stale"

# The artifact probe's slice of the SAME wall budget the git phases spend
# from — never an extension of it. The measured per-invocation CLI floor is
# ~1.2 s, so this is room for a read plus slack, and a probe that starts
# after the budget is gone spawns nothing at all and is reported `timeout`.
ARTIFACT_BUDGET_SECONDS = 6

# How many stale artifacts may be named individually. `skt status` and the
# hook injection both carry this text into every session, and a lazily-built
# home has dozens — the rest are counted, and render_text says how many.
MAX_ARTIFACT_NOTIFICATIONS = 3

# Opt-OUT, deliberately, and never an opt-in. skt#22 was an env-GATED
# behaviour that changed what a session got depending on a variable nobody
# set; the default here is on in every session, and the switch exists only
# so an operator who has measured a reason can turn one probe off.
ARTIFACTS_ENV = "SKT_ARTIFACTS"

# The CLI-version probe's slice of the shared budget: one call into this
# home's own pin (measured floor ~1.2 s) plus up to two brew reads, which
# are local formula-cache lookups and do not touch the network.
#
# MEASURED COST, 2026-08-27, operator root home: +2.2 s on a live pass
# (1.00 s -> 3.17 s). That is affordable because the SessionStart hook is
# cache-FIRST -- it serves `check --cached` and only refreshes live on a
# TTL miss, and PostToolUse never refreshes at all -- so the 2.2 s is paid
# at most once per DEFAULT_TTL_SECONDS per home, not once per session.
# If that ordering ever inverts, this probe is the first thing to re-time.
CLI_VERSION_BUDGET_SECONDS = 5

# Opt-OUT, on the same terms as ARTIFACTS_ENV: on in every session, and the
# switch exists only for an operator who has measured a reason to spend
# nothing here.
CLI_VERSION_ENV = "SKT_CLI_VERSION"

# Marks a version probe that was REFUSED rather than answered, so the state
# machine below can offer the remedy that fits (make the environment and the
# home agree) instead of the one that does not (re-write the pin — the pin
# is fine, and re-writing it changes nothing about a cross-home refusal).
CLI_REFUSED_PREFIX = "the CLI refused a cross-home run: "

# The tap formula `skill-manager upgrade --self` upgrades, and therefore the
# only thing whose "newer one exists" answer matches that remedy.
BREW_FORMULA = "skill-manager"

# Recorded `errors[*].kind` values that describe the STORE CHECKOUT's own
# git state — the three whose remedy in skill-manager's own
# `ReportUseCase.hint` is an action IN the store directory rather than a
# retry of the command that failed. For these, "installed hash != remote
# tip" has a recorded explanation and a pull is not the reading:
#
#   MERGE_CONFLICT       unmerged paths in the store; sync set this state
#                        and clears it only when they are gone
#   NO_GIT_REMOTE        git-tracked, no origin — sync has nothing to fetch
#   NEEDS_GIT_MIGRATION  no .git in the store at all — sync cannot run
#
# Everything else stays out on purpose. GATEWAY_UNAVAILABLE,
# MCP_REGISTRATION_FAILED, REGISTRY_UNAVAILABLE, AGENT_SYNC_FAILED,
# HARNESS_CLI_UNAVAILABLE and AUTHENTICATION_NEEDED are records about
# registration, projection or credentials; the store is fine and the pull
# advice is correct, so suppressing it would trade a wrong message for a
# missing right one. A kind this file has never heard of is likewise NOT
# blocking: an unknown state must not silently swallow a true notification.
STORE_BLOCKING_ERRORS = ("MERGE_CONFLICT", "NO_GIT_REMOTE", "NEEDS_GIT_MIGRATION")


def _run_git(argv: list[str], timeout: float,
             env: dict | None = None) -> subprocess.CompletedProcess | None:
    """Bounded call; None on deadline. Kills the child's WHOLE group.

    `subprocess.run(timeout=)` kills only the direct child; git spawns
    helpers (ssh, credential fillers) that inherit the pipes and keep
    the caller open past its budget. Own session + killpg reaps the lot.

    Named for its first caller, but the mechanism is not git-specific and
    the CLI/brew probes below reuse it. `env` exists for those: invoking a
    home's own CLI pin requires stripping SKILL_MANAGER_CLI first, or an
    older unguarded pin execs itself forever (see publish._cli_env).
    """
    if timeout <= 0:
        return None  # budget already spent: do not spawn at all
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
    except OSError:
        # argv[0] is not on PATH, or is not executable. Identical in
        # consequence to a call that never answered, and a SessionStart
        # hook must never turn that into a traceback -- `brew` is simply
        # absent on a Linux box or a non-brew install.
        return None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:  # group already gone or unkillable: fall back to the child
            proc.kill()
        try:
            proc.communicate(timeout=5)  # reap — SIGKILL cannot be blocked, so this returns
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _remote_tip(origin: str, ref: str | None, *, deadline: float | None = None) -> str | None:
    timeout = float(REMOTE_TIMEOUT_SECONDS)
    if deadline is not None:
        # Clamp to the shared deadline: a worker that starts late must not
        # extend the command past it, and one that starts after it spawns
        # nothing at all (_run_git refuses timeout <= 0).
        timeout = min(timeout, deadline - time.monotonic())
    proc = _run_git(["git", "ls-remote", origin, ref or "HEAD"], timeout)
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split()[0]


def _remote_tip_safe(origin: str, ref: str | None, *, deadline: float | None = None) -> str | None:
    """Never raises: a hung or failing remote is 'unverifiable', not a crash.

    This is on the hook path (SKT-6 runs check on tool events) — an
    exception here would surface as a traceback inside an agent session.
    """
    try:
        return _remote_tip(origin, ref, deadline=deadline)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _store_dir(home: Path, unit: homes.Unit) -> Path | None:
    for kind_dir in ("skills", "plugins", "docs", "harnesses"):
        candidate = home / kind_dir / unit.name
        if candidate.is_dir():
            return candidate
    return None


def _is_ancestor(unit_dir: Path, ancestor: str, descendant: str, timeout: float) -> bool | None:
    """Is `ancestor` reachable from `descendant` IN THIS CHECKOUT?

    True/False when git could decide, None when it could not — and the
    interesting None is exit 128, "not a valid object name", which is
    exactly what a genuinely un-fetched commit gives. That makes the
    probe self-selecting and network-free: a store that really is stale
    does not have the remote tip locally and answers None, while a store
    that already contains it answers True.
    """
    proc = _run_git(
        ["git", "-C", str(unit_dir), "merge-base", "--is-ancestor", ancestor, descendant], timeout
    )
    if proc is None:
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # 128: the object is not in this checkout


def _local_state(unit_dir: Path, *, deadline: float | None = None,
                 remote_tip: str | None = None) -> str:
    """'clean' | 'dirty' | 'ahead' | 'upstream-stale' | 'unknown'.

    Both probes are bounded (per-call cap AND the shared deadline). A
    probe that timed out reports 'unknown', never 'clean': a publish
    prompt must not be fabricated from a check that did not finish, but
    a record that presents unpushed work as verified-clean for a whole
    TTL is the same lie in the other direction. The caller labels
    'unknown' as unverifiable, which also keeps an all-timed-out
    refresh out of the cache.

    `rev-list --count @{upstream}..HEAD` on its own cannot tell unpushed
    work from a stale remote-tracking ref: `@{upstream}` is a LOCAL ref
    that only a fetch moves, and the store checkout is advanced by a
    path that does not always move it. Measured in this repo's project
    home: six units reported 5-52 commits "ahead" while every one of
    them had HEAD exactly equal to its live remote tip. So where the
    caller knows the live tip, the count is adjudicated against it —
    locally, with no extra network — and a store whose HEAD the remote
    already contains is 'upstream-stale' (nothing to publish; the local
    ref is behind), never 'ahead'. Without a tip the verdict stays
    'ahead', because nothing available can separate the two.
    """
    if not (unit_dir / ".git").exists():
        return "clean"

    def _timeout() -> float:
        if deadline is None:
            return float(LOCAL_TIMEOUT_SECONDS)
        return min(float(LOCAL_TIMEOUT_SECONDS), deadline - time.monotonic())

    proc = _run_git(["git", "-C", str(unit_dir), "status", "--porcelain"], _timeout())
    if proc is None:
        return "unknown"
    if proc.stdout.strip():
        return "dirty"
    proc = _run_git(
        ["git", "-C", str(unit_dir), "rev-list", "--count", "@{upstream}..HEAD"], _timeout()
    )
    if proc is None:
        return "unknown"
    if proc.returncode == 0 and proc.stdout.strip() and int(proc.stdout.strip()) > 0:
        if remote_tip and _is_ancestor(unit_dir, "HEAD", remote_tip, _timeout()) is True:
            return STATE_UPSTREAM_STALE
        return "ahead"
    return "clean"


def state_file(home: Path) -> Path:
    return home / "cache" / "skt-check.json"


def _artifact_state(home: Path, deadline: float) -> dict:
    """The `artifacts` block of the record. LIVE PATH ONLY.

    One CLI call, and a typed `state` on every path so a reader can always
    tell WHY it is looking at no rows:

      ok            the counts and rows below are this pass's measurement
      unsupported   this home's skill-manager predates the artifact graph
      no-cli        this home holds no CLI pin to ask
      timeout       the probe did not finish inside its slice of the budget
      error         anything else, with the CLI's own reason attached
      off           SKT_ARTIFACTS=0

    None of those is an empty result presented as a clean one — "nothing is
    stale" and "I could not ask" are different answers, which is the whole
    reason `skt.artifacts` raises instead of returning `()`.

    `rows` carries ONLY the rebuildable set, because it is the only set a
    notification may be built from and the only one that is enumerated
    anywhere. The other stale artifacts are counted and not listed.
    """
    if os.environ.get(ARTIFACTS_ENV, "").strip() in ("0", "false", "no"):
        return {"state": "off", "reason": f"{ARTIFACTS_ENV} is set to off"}
    # Imported HERE, not at module scope: `--cached` must not load a module
    # whose whole job is to run a subprocess, and `publish` -> `check` would
    # make a module-level import circular besides.
    from . import artifacts as artifacts_mod

    budget = min(float(ARTIFACT_BUDGET_SECONDS), deadline - time.monotonic())
    try:
        survey = artifacts_mod.stale(home=home, timeout=budget)
    except artifacts_mod.ArtifactsUnsupported as exc:
        return {"state": "unsupported", "reason": exc.reason, "fix": exc.fix}
    except artifacts_mod.CliUnavailable as exc:
        return {"state": "no-cli", "reason": exc.reason, "fix": exc.fix}
    except artifacts_mod.ProbeTimeout as exc:
        return {"state": "timeout", "reason": exc.reason, "fix": exc.fix}
    except artifacts_mod.ArtifactError as exc:
        return {"state": "error", "reason": exc.reason, "fix": exc.fix}
    except Exception as exc:  # noqa: BLE001 — never a traceback in a session
        return {"state": "error", "reason": f"{type(exc).__name__}: {exc}", "fix": ""}
    rebuildable = survey.rebuildable
    return {
        "state": "ok",
        "total": survey.total,
        "stale": len(survey.stale),
        "unverifiable": len(survey.unverifiable),
        "current": survey.current,
        "not_built": len(survey.not_built),
        "rebuildable": len(rebuildable),
        "rows": [
            {
                "id": row.id,
                "name": row.short_name,
                "kind": row.kind,
                "owner": row.owner,
                "reason": row.reason,
                "because": list(row.because),
            }
            for row in rebuildable
        ],
        # The stale rows that are NOT rebuildable, which `rows` above
        # deliberately omits. A unit-store row is exactly that -- it has no
        # local producer -- and it is also the most common ROOT CAUSE of a
        # rebuildable row being stale. Omitting it from the state entirely is
        # why the remedy could not name it: the reader that had to choose the
        # remedy could not see the thing that needed fixing.
        "stale_stores": sorted({
            str(getattr(row, "id", "")).split(":", 1)[1]
            for row in survey.stale
            if str(getattr(row, "id", "")).startswith("unit-store:")
        }),
    }


def _parse_version(text: str) -> tuple[int, ...] | None:
    """`0.25.1` -> (0, 25, 1). None if it is not a dotted numeric version.

    Deliberately strict. A version this cannot read is reported as unknown
    rather than compared as a string: `0.9.0` sorts after `0.25.1`
    lexically, and a false "you are behind" costs more trust than a
    missing notification.
    """
    parts = text.strip().split(".")
    if not (2 <= len(parts) <= 4):
        return None
    out = []
    for part in parts:
        digits = part.split("-", 1)[0].split("+", 1)[0]
        if not digits.isdigit():
            return None
        out.append(int(digits))
    return tuple(out)


def _installed_cli_version(home: Path, timeout: float) -> tuple[str | None, str]:
    """The version of the CLI THIS HOME runs, not the one brew installed.

    The distinction is the whole point. `skill-manager` on PATH is usually
    a home's shim, and a home may pin a build that is not brew's — so
    asking brew what is installed answers a question about the machine
    when the question is about this home. Measured on 2026-08-27: the
    operator's root home shim, the project home shim and brew all agreed,
    but only because a migration had just repointed 23 dead pins.
    """
    from .publish import _cli, _cli_env

    cli = _cli(home)
    if not cli.is_file():
        return None, f"this home has no skill-manager CLI pin at {cli}"
    proc = _run_git([str(cli), "--version"], timeout, env=_cli_env())
    if proc is None:
        return None, "the CLI did not answer --version inside its budget"
    if proc.returncode != 0:
        # A REFUSAL IS NOT EVIDENCE ABOUT A VERSION (skill-manager#264).
        # A `bin/cli` shim binds the home it lives in and refuses when
        # `SKILL_MANAGER_HOME` names a different one, so this probe's
        # non-zero exit can mean "the environment disagrees with the pin"
        # — which the pin-rewriting remedy below does not touch. Reported
        # in the shim's own words, both homes included; `detail[0]` alone
        # kept the headline and dropped exactly the two paths that make
        # the sentence actionable.
        refusal = relay_mod._hoist((proc.stdout or "") + (proc.stderr or ""))
        if refusal or proc.returncode == relay_mod.HOME_MISMATCH_EXIT:
            return None, CLI_REFUSED_PREFIX + (
                "; ".join(line.strip() for line in refusal)
                or f"a bin/cli shim refused a cross-home run (exit {proc.returncode})"
            )
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, f"{cli} --version exited {proc.returncode}" + (
            f": {detail[0]}" if detail else ""
        )
    # `skill-manager 0.25.0`, then `build:` and `cli:` lines.
    first = (proc.stdout or "").strip().splitlines()
    if not first:
        return None, "the CLI printed no version line"
    token = first[0].split()
    if len(token) < 2 or _parse_version(token[-1]) is None:
        return None, f"could not read a version from {first[0]!r}"
    return token[-1], ""


def _brew_latest(timeout: float) -> tuple[str | None, str]:
    """The newest skill-manager brew can install right now.

    Two reads, both against brew's LOCAL formula cache — no network. That
    is the limitation worth stating rather than hiding: brew learns about
    a release when its tap is fetched, so a machine that has not run `brew
    update` recently will report the version it last saw. The answer is
    therefore a floor on how far behind the home is, never a ceiling, and
    the notification is worded as one.
    """
    outdated = _run_git(
        ["brew", "outdated", "--json=v2", BREW_FORMULA], min(timeout, 3.0)
    )
    if outdated is None:
        return None, "brew is not available, or did not answer inside its budget"
    # NOT gated on the exit status, and this is the whole subtlety: `brew
    # outdated` exits NON-ZERO precisely when something IS outdated, so
    # treating that as failure inverts the check and reports "nothing to
    # compare" in exactly the case worth reporting. Measured here on
    # 2026-08-27 against a real 0.25.0 -> 0.25.1 gap. The JSON parsing is
    # the real predicate: unreadable output is the failure, not rc.
    try:
        listed = (json.loads(outdated.stdout or "{}").get("formulae") or [])
    except json.JSONDecodeError:
        return None, "brew could not report on the skill-manager formula"
    for row in listed:
        current = (row.get("current_version") or "").strip()
        if current and _parse_version(current):
            return current, ""
    # Nothing outdated, so whatever brew HOLDS is the newest it knows of.
    have = _run_git(["brew", "list", "--versions", BREW_FORMULA], min(timeout, 2.0))
    if have is None or have.returncode != 0:
        return None, "brew knows of no installed skill-manager formula"
    words = (have.stdout or "").split()
    for word in reversed(words):
        if _parse_version(word):
            return word, ""
    return None, "could not read a version from brew list"


def _cli_state(home: Path, deadline: float) -> dict:
    """The `cli` block: which skill-manager this home runs, and whether a
    newer one is installable. LIVE PATH ONLY.

    A typed `state` on every path, for the same reason `_artifact_state`
    has one -- so a reader can tell "up to date" from "could not ask":

      ok              `installed` and `latest` both known and compared
      unknown-latest  the home's version is known; nothing could say what
                      the newest is (no brew, not a brew install)
      no-cli          this home holds no CLI pin to ask
      timeout         the probe did not finish inside its slice
      error           anything else, with the reason attached
      off             SKT_CLI_VERSION=0
    """
    if os.environ.get(CLI_VERSION_ENV, "").strip() in ("0", "false", "no"):
        return {"state": "off", "reason": f"{CLI_VERSION_ENV} is set to off"}

    budget = min(float(CLI_VERSION_BUDGET_SECONDS), deadline - time.monotonic())
    if budget <= 0:
        return {"state": "timeout", "reason": "the shared budget was spent before this probe"}

    installed, why = _installed_cli_version(home, budget)
    if installed is None:
        state = "no-cli" if "CLI pin" in why else (
            "timeout" if "budget" in why else "error"
        )
        fix = f"skill-manager home shims --root {home}   # re-writes the pin"
        if why.startswith(CLI_REFUSED_PREFIX):
            fix = (
                f"SKILL_MANAGER_HOME={home} skt check   # the pin is fine; the "
                "environment names a different home than the shim serves"
            )
        return {"state": state, "reason": why, "fix": fix}

    # A LOCAL BUILD IS NOT BEHIND A RELEASE, it is beside it. skill-manager
    # stamps a build suffix -- `0.25.0+g08a1c00d4503` -- on a CLI built from
    # a checkout rather than installed from the tap, and a home running one
    # is running what its operator is developing. Two things are then wrong
    # with the ordinary reading: the base version says nothing about which
    # commits the build carries (measured 2026-08-28, the operator's project
    # home built `0.25.0+g08a1c00` from a branch that ALREADY CONTAINED
    # every fix in the 0.25.1 release it was being told it was behind), and
    # `upgrade --self` upgrades the tap, which cannot move a CLI this home
    # builds itself. So: reported for orientation, never notified about.
    local_build = "+" in installed
    latest, why_latest = _brew_latest(max(0.5, deadline - time.monotonic()))
    if latest is None:
        return {"state": "unknown-latest", "installed": installed,
                "local_build": local_build, "reason": why_latest}

    behind = (not local_build) and _parse_version(installed) < _parse_version(latest)
    return {
        "state": "ok",
        "installed": installed,
        "latest": latest,
        "local_build": local_build,
        "outdated": behind,
    }


# The two shapes that both mean "this home runs another home's copy". Read
# from `home repair`'s own JSON rather than re-derived here: skill-manager owns
# the question, and a second implementation of it in skt is precisely the
# two-readers-one-truth defect this notification exists because of.
_MIGRATION_KINDS = ("PARENT_SHIM_SHADOWS_LOCAL_COPY", "FOREIGN_PATH_IN_SHIM")


def _migration_notifications(home: Path, deadline: float | None = None) -> list[dict]:
    """Does this home hold a copy of a unit that it does not run?

    The symptom is an afternoon: you edit a skill in the home you are working
    in, nothing changes, and every check says the home is fine — `home verify`
    passes because every path RESOLVES, it just resolves into the parent.

    Silent on every uncertainty. A missing pin, a timeout, a non-zero exit or
    output that will not parse all report NOTHING, because "I could not find
    out" is not "you need to migrate", and a greeting that cries migration on
    a healthy home is one people learn to scroll past.

    New homes do not need this: `home clone` writes local shims wherever the
    copy can run them. It is a one-time pass over homes that already exist.
    """
    cli = home / "bin" / "cli" / "skill-manager"
    if not cli.is_file():
        return []
    timeout = 25.0
    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
        if timeout < 1.0:
            return []
    try:
        proc = subprocess.run(
            [str(cli), "home", "repair", "--home", str(home), "--json"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "SKILL_MANAGER_HOME": str(home)},
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    # `home repair` exits non-zero WHEN IT FINDS SOMETHING, so the exit code
    # is not the error signal here -- the parse is.
    try:
        report = json.loads(proc.stdout)
        findings = report["findings"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    subjects = sorted({f.get("subject", "?") for f in findings
                       if f.get("kind") in _MIGRATION_KINDS})
    if not subjects:
        return []
    named = ", ".join(subjects[:3]) + (f" and {len(subjects) - 3} more"
                                       if len(subjects) > 3 else "")
    return [{
        "kind": "home-migration",
        "count": len(subjects),
        "subjects": subjects,
        "message": (
            f"this home runs another home's copy for {len(subjects)} entry "
            f"point(s) ({named}) — it HAS its own copy, so an edit here "
            f"changes nothing"
        ),
        "fix": f"skill-manager home repair --home {home} --fix",
    }]


def _cli_notifications(state: dict) -> list[dict]:
    """One notification, and only when this home is demonstrably behind.

    Never fires on `unknown-latest`: "I could not find out" is not
    "you are current", but it is also not grounds to tell an agent to
    upgrade. The silence is the honest answer there.
    """
    # `outdated` is already False for a local build, so this is belt and
    # braces -- but the rule is worth stating where the notification is
    # built, because the remedy below is the thing that would be wrong.
    if state.get("state") != "ok" or not state.get("outdated") or state.get("local_build"):
        return []
    installed = state["installed"]
    latest = state["latest"]
    return [
        {
            "kind": "cli-version",
            "installed": installed,
            "latest": latest,
            "message": (
                f"skill-manager {installed} is installed here, and {latest} is available "
                f"— this session's commands run the older one"
            ),
            "fix": "skill-manager upgrade --self",
        }
    ]


def _blocking_error(unit: homes.Unit, store: Path | None) -> dict | None:
    """A `unit-error` notification, or None if nothing recorded blocks the store.

    Reads the record, spawns nothing: this is the field the home has been
    writing all along and the reason the hash comparison disagrees.

    The remedy is deliberately NOT a skill-manager verb. Three facts from
    skill-manager's own source decide it for MERGE_CONFLICT:

      * `SyncCommand --merge`'s help says conflicts "leave the working
        tree in conflicted state and set MERGE_CONFLICT until resolved" —
        so `--merge` is what PRODUCES this state, not what clears it;
      * `LiveInterpreter` clears the error exactly when
        `GitOps.unmergedFiles(dir).isEmpty()` — the state is a fact about
        the store's working tree, and only resolving it there changes it;
      * `ReportUseCase.hint(MERGE_CONFLICT, ...)` already answers
        "resolve in <storeDir>, then `git add` + `git commit`".

    So `skt check` says what skill-manager already says, in the surface an
    agent actually reads, and adds the one thing the hint does not carry:
    the stash. The recorded message names `stash@{0}` when the conflict
    came from a stash pop, and that stash is somebody's uncommitted work —
    it is destroyed by anything that resets the store, which is the real
    cost of following a `skt sync` prompt here.
    """
    record = unit.error(*STORE_BLOCKING_ERRORS)
    if record is None:
        return None
    kind = record.get("kind")
    recorded = str(record.get("message") or "").strip()
    where = str(store) if store is not None else f"<this home>/skills/{unit.name}"
    if kind == "MERGE_CONFLICT":
        parked = " Local work is preserved at stash@{0}." if "stash@{" in recorded else ""
        message = (
            f"{unit.name} is not stale — its store is mid-merge (MERGE_CONFLICT): "
            f"unmerged paths remain.{parked} Syncing re-runs the merge that made them."
        )
        fix = f"git -C {where} status   # resolve, then: git add + git commit"
    elif kind == "NO_GIT_REMOTE":
        message = (
            f"{unit.name} cannot be synced (NO_GIT_REMOTE): its store is git-tracked "
            f"but has no origin, so there is nothing to pull from."
        )
        fix = f"git -C {where} remote add origin <url>"
    else:  # NEEDS_GIT_MIGRATION
        message = (
            f"{unit.name} cannot be synced (NEEDS_GIT_MIGRATION): its store has no .git, "
            f"so sync and upgrade have nothing to advance."
        )
        fix = f"skill-manager uninstall {unit.name} && skill-manager install github:<owner>/<repo>"
    note = {
        "kind": "unit-error",
        "unit": unit.name,
        "state": kind,
        "store": where,
        "message": message,
        "fix": fix,
    }
    if recorded:
        # The home's own sentence, verbatim. It carries the timestamp's
        # context — which remote, which branch, which stash — and nothing
        # this function composes should replace it.
        note["recorded"] = recorded
    return note


def _artifact_cause(row: dict, moved: dict) -> str:
    """Why this artifact is stale, in one clause, from TYPED evidence.

    Preference order, and the reason for it: an upstream `unit-store:<u>`
    that this same pass ALSO found a new version for gives both hashes
    typed, so the clause can say what moved and to what. Failing that, an
    upstream unit-store row is itself the cause and is named. Failing
    that, the artifact's own inputs moved and the verdict's own reason is
    quoted verbatim.

    What this deliberately does NOT do is parse the verdict's prose for
    the hashes inside it. `wt.py`'s docstring records what parsing another
    tool's prose costs; a shorter sentence is not worth it.
    """
    units = [b.split(":", 1)[1] for b in row.get("because") or [] if b.startswith("unit-store:")]
    for unit in units:
        note = moved.get(unit)
        if note:
            return f"{unit} moved {note['installed']} -> {note['remote']}"
    if units:
        extra = f" (+{len(units) - 1} more)" if len(units) > 1 else ""
        return f"{units[0]} moved{extra}"
    return row.get("reason") or "its inputs no longer match what was recorded"


def _artifact_notifications(state: dict, unit_notes: list[dict]) -> list[dict]:
    """`stale-artifact` rows, bounded, from the rebuildable set only.

    An artifact that was declared and never built is not surfaced: that is
    the normal state of a lazily-provisioned home, and a notification that
    fires in the healthy case is one an agent learns to ignore.
    """
    if state.get("state") != "ok":
        return []
    moved = {n["unit"]: n for n in unit_notes if n.get("kind") == "new-version"}

    def _already_said(row: dict) -> int:
        """0 if nothing else in this report explains this row, 1 if it does.

        Only `MAX_ARTIFACT_NOTIFICATIONS` rows are named, so which ones is
        a real decision. An artifact stale because a unit moved sits under
        that unit's own `new-version` line, three lines above; an artifact
        whose OWN re-derived fingerprint diverged is carried by nothing
        else in the report. Measured on the operator's project home with
        one planted edit: without this the three named rows were all
        downstream of two units already named above, and the planted
        artifact — the only news in the report — fell into `+5 more`.

        Stable within each group: this only decides which half of the list
        a row is in, never the order inside it.
        """
        return int(any(
            b.split(":", 1)[1] in moved
            for b in row.get("because") or [] if b.startswith("unit-store:")
        ))

    # THE REMEDY MUST CLEAR THE CONDITION IT NAMES.
    #
    # Every stale row used to be handed `skt build <name>`. For an artifact
    # that is stale because an upstream unit-store is stale, that command
    # runs, reports "built", and the row reads stale again immediately --
    # because `build` re-derives the artifact from an input that is still
    # wrong. Measured on the operator's project home, 2026-08-26:
    #
    #   skt check  -> artifact computeq is stale (deploy-helm moved)
    #                 rebuild with: skt build computeq
    #   skt build computeq helm-deploy monitoring
    #             -> "3 built" ... "4 of the selected artifact(s) are still stale"
    #   skt check  -> the same three lines, unchanged
    #
    # A loop, and the operator is the thing in it. `skill-manager artifacts
    # stale` knew the answer the whole time and prints it on the unit-store
    # row: "a unit's store bytes come from its source, not from a local
    # producer -- `skill-manager sync deploy-helm`". One sync cleared all
    # sixteen stale artifacts in that home.
    #
    # So: when this row's staleness is inherited from a unit-store that is
    # ITSELF in the stale set, name the sync that fixes the root cause. Only
    # then -- an artifact built from a FRESH store and stale on its own
    # inputs is exactly what `skt build` is for, and still gets it.
    # Both sources on purpose: `stale_stores` is the authoritative one (it can
    # see the non-rebuildable rows), and the rows themselves cover a state dict
    # built before that field existed.
    stale_stores = set(state.get("stale_stores") or ())
    stale_stores |= {
        str(r.get("id", "")).split(":", 1)[1]
        for r in (state.get("rows") or [])
        if str(r.get("id", "")).startswith("unit-store:")
    }

    def _remedy(row: dict, name: str) -> str:
        if str(row.get("id", "")).startswith("unit-store:"):
            # The root cause itself. `build` has no producer for a store row.
            return f"skill-manager sync {shlex.quote(name)}"
        upstream = next(
            (b.split(":", 1)[1] for b in row.get("because") or []
             if b.startswith("unit-store:") and b.split(":", 1)[1] in stale_stores),
            None,
        )
        if upstream:
            return f"skill-manager sync {shlex.quote(upstream)}"
        return f"skt build {shlex.quote(name)}"

    ordered = sorted(state.get("rows") or [], key=_already_said)
    out = []
    for row in ordered[:MAX_ARTIFACT_NOTIFICATIONS]:
        name = row["name"]
        out.append(
            {
                "kind": "stale-artifact",
                "artifact": row["id"],
                "name": name,
                "owner": row.get("owner"),
                "message": f"artifact {name} is stale ({_artifact_cause(row, moved)})",
                # The command, alone, so a consumer can run it without
                # extracting it from a sentence — and SHELL-QUOTED, because
                # the whole value of this line is that it can be retyped.
                # One of the seven rebuildable artifacts in the operator's
                # own project home is `jinja2-cli[yaml]`, and unquoted that
                # is `zsh: no matches found` in the operator's own shell.
                "fix": _remedy(row, name),
            }
        )
    return out


def collect(start: str | Path = ".", *, use_network: bool = True,
            probe_artifacts: bool = True, probe_cli: bool = True,
            probe_migration: bool = True) -> dict:
    """One live pass. `use_network` governs the REMOTE phase only.

    The artifact probe is local — it asks this home's own CLI about this
    home's own disk — so it is not covered by `use_network` and gets its
    own switch rather than quietly widening that flag's meaning. The
    CLI-version probe is local on the same terms and takes `probe_cli` for
    the same reason: it is a SECOND spawn into this home's pin, and a
    caller that asked for no local probes must be able to say so once per
    probe rather than discover a new one. All three are bounded by the one
    shared deadline.
    """
    home = homes.find_home(start)
    if home is None:
        return {"schema": SCHEMA_VERSION, "home": None, "error": "no skill-manager home found"}
    tier = ctx_mod.classify_tier(home, ctx_mod.checkout_root(start))
    notifications: list[dict] = []
    checked: list[str] = []
    unverifiable: list[str] = []
    upstream_stale: list[str] = []
    ahead_of_remote: list[str] = []
    managed = [u for u in homes.read_units(home) if u.change_managed]
    tips: dict[str, str | None] = {}
    # One deadline for the whole command: both git phases clamp every
    # subprocess to it, so the command's wall time is bounded by the
    # budget plus kill/reap slack — never by how many children hung.
    deadline = time.monotonic() + NETWORK_BUDGET_SECONDS
    if use_network and managed:
        # Parallel: a real root home has ~20 git-backed units, and serial
        # ls-remote at up to REMOTE_TIMEOUT_SECONDS each would block an
        # explicit refresh for minutes.
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=8)
        try:
            futures = {
                u.name: pool.submit(_remote_tip_safe, u.origin, u.git_ref, deadline=deadline)
                for u in managed
            }
            for name, future in futures.items():
                remaining = deadline - time.monotonic()
                try:
                    tips[name] = future.result(timeout=max(0.05, remaining))
                except Exception:  # budget exhausted -> unverifiable, not late
                    tips[name] = None
        finally:
            # Deliberately NOT `with`: __exit__ waits for queued/running
            # calls, which is the ceil(units/8) * REMOTE_TIMEOUT overrun
            # this budget exists to prevent. Cancel what never started and
            # abandon the runners — their subprocess timeouts are clamped
            # to this same deadline, so nothing they hold outlives it.
            pool.shutdown(wait=False, cancel_futures=True)
    for unit in managed:
        checked.append(unit.name)
        store = _store_dir(home, unit)
        # BEFORE either verdict, and network-free: the home records why a
        # store is where it is, and a state the installer wrote down is
        # not something the pull/push heuristics get to re-diagnose from
        # hashes. It also fires when the hashes AGREE — a store with
        # unmerged paths is not well just because its record is current.
        blocked = _blocking_error(unit, store)
        if blocked is not None:
            notifications.append(blocked)
        if use_network:
            tip = tips.get(unit.name)
            if tip is None:
                unverifiable.append(unit.name)
            elif blocked is not None:
                # The disagreement with the tip has a RECORDED cause, and
                # `blocked` already named it and its remedy. Neither a
                # `new-version` prompt nor an `ahead_of_remote` label is
                # true here: `hyper-experiments-finance` sits exactly ON
                # its remote tip mid-conflict, which is neither stale nor
                # ahead.
                pass
            elif tip != unit.git_hash:
                # `installed != tip` is not the same question as "is there
                # anything to pull". A store carrying a commit the remote
                # does not have yet differs from the tip in the OTHER
                # direction, and telling that agent to pull is wrong — it
                # is what made `debugging` a false notification in ARTI-00.
                # Ancestry decides it, locally: if this checkout already
                # contains the tip, there is nothing upstream to fetch.
                contains_tip = (
                    _is_ancestor(
                        store, tip, "HEAD",
                        min(float(LOCAL_TIMEOUT_SECONDS), deadline - time.monotonic()),
                    )
                    if store is not None
                    else None
                )
                if contains_tip is True:
                    ahead_of_remote.append(unit.name)
                else:
                    # Compared against the origin's default-branch tip: installed
                    # records carry no ref pin, so a deliberately pinned unit can
                    # appear here — the message states what was compared.
                    notifications.append(
                        {
                            "kind": "new-version",
                            "unit": unit.name,
                            "installed": (unit.git_hash or "")[:8],
                            "remote": tip[:8],
                            "message": f"new version available for {unit.name} — pull with: skt sync {unit.name}",
                        }
                    )
        if tier == "root" and blocked is None:
            # The same unread field, on the push side. A conflicted store
            # is `dirty` to `git status --porcelain`, so this branch used
            # to answer a half-merged working tree with `skt publish` —
            # which would carry the conflict markers upstream. `blocked`
            # has already said what the state is; there is no second
            # remedy to offer for the same fact.
            unit_dir = store
            if unit_dir:
                state = _local_state(
                    unit_dir, deadline=deadline, remote_tip=tips.get(unit.name)
                )
                if state == STATE_UPSTREAM_STALE:
                    # The remote already has this HEAD. Nothing to publish;
                    # only `@{upstream}` is behind. Recorded, not prompted.
                    upstream_stale.append(unit.name)
                elif state == "unknown":
                    # The probe never finished: an evidence gap, not a
                    # verdict. Labeling it keeps the cached record honest
                    # and the refusal predicate counting it.
                    if unit.name not in unverifiable:
                        unverifiable.append(unit.name)
                elif state != "clean":
                    notifications.append(
                        {
                            "kind": "sync-with-root",
                            "unit": unit.name,
                            "state": state,
                            "message": (
                                f"{unit.name} modified locally ({state}) — please sync with root "
                                f"to publish changes globally: skt publish {unit.name}"
                            ),
                        }
                    )
    # After the git phases, and BEFORE the artifact probe. The ordering
    # against the git phases is the established one -- an added probe must
    # not delay the unit notifications. The ordering against artifacts is
    # deliberate and new: a home running a release-behind binary is the
    # thing that explains why the other answers might be wrong, so when the
    # budget is thin it is the one worth spending on. It is also the
    # cheaper of the two.
    cli = (_cli_state(home, deadline) if probe_cli
           else {"state": "off", "reason": "probe_cli=False"})
    notifications += _cli_notifications(cli)
    # After the CLI notification and before the artifacts, because the CLI's
    # own version explains this one: a home migrated by an older build will
    # report the shape again, and the reader should see the upgrade first.
    # A THIRD local spawn, and therefore a third switch. The rule stated
    # beside `probe_artifacts` is that a local probe gets its own rather than
    # widening another flag behind a caller's back; a caller that turned the
    # known probes off and still got a subprocess would be exactly that.
    if probe_migration:
        notifications += _migration_notifications(home, deadline)
    # LAST, and from what the git phases have left of the shared deadline.
    # Ordered after them so an added probe cannot make the established
    # unit notifications later than they already were; the cost is that a
    # pass whose remotes ate the whole budget reports the artifacts
    # `timeout` rather than silently skipping them.
    artifacts = (_artifact_state(home, deadline) if probe_artifacts
                 else {"state": "off", "reason": "probe_artifacts=False"})
    artifact_notes = _artifact_notifications(artifacts, notifications)
    if artifact_notes:
        # The record's rows are reordered to match the notifications, so
        # that `skt status` — which reads this block back and names a few —
        # names the SAME few `skt check` did. Two surfaces disagreeing about
        # which artifacts matter is how a report stops being believed.
        rank = {note["artifact"]: i for i, note in enumerate(artifact_notes)}
        artifacts["rows"] = sorted(
            artifacts["rows"], key=lambda row: rank.get(row["id"], len(rank))
        )
    notifications += artifact_notes
    report = {
        "schema": SCHEMA_VERSION,
        "home": str(home),
        "tier": tier,
        "cli": cli,
        "artifacts": artifacts,
        "checked_units": checked,
        "unverifiable": unverifiable,
        # Agreement between store and remote that only a LOCAL ref
        # contradicts. Neither is a notification; both are reported so
        # the state is inspectable and `--json` consumers can see it.
        "upstream_stale": upstream_stale,
        "ahead_of_remote": ahead_of_remote,
        "network": use_network,
        "checked_at": time.time(),
        "notifications": notifications,
    }
    if len([n for n in notifications if n["kind"] == "new-version"]) > 1:
        report["hint"] = (
            "multiple units are stale — sync in skill-imports dependency order "
            "(a unit importing files another stale unit adds fails validation if synced first)"
        )
    return report


def _write_cache(report: dict) -> None:
    if not report.get("home"):
        return
    checked = report.get("checked_units") or []
    unverifiable = report.get("unverifiable") or []
    artifacts = report.get("artifacts") or {}
    # "The CLI answered" is not "anything was decided". A pass where every
    # one of 190 artifacts came back `unverifiable` resolved exactly as
    # much as a pass where no remote was reachable, and caching it would
    # let --cached serve `all current` at exit 0 for a whole TTL over a
    # home nothing in that pass could decide — the failure the predicate
    # below exists to prevent, arriving through the new dimension.
    artifacts_resolved = (
        artifacts.get("state") == "ok"
        and (artifacts.get("stale") or 0) + (artifacts.get("current") or 0) > 0
    )
    if (
        report.get("network")
        and checked
        and len(unverifiable) >= len(checked)
        and not artifacts_resolved
    ):
        # A refresh that resolved NOTHING is a failure, not a result:
        # caching it would let --cached serve "all current (fresh)" for a
        # TTL window in which no unit was actually verified.
        #
        # The record now carries a SECOND, independently resolved
        # dimension, so "nothing" has to mean both halves. A refresh that
        # decided 190 artifacts and reached no remote did not resolve
        # nothing, and dropping it would take the artifact notification
        # out of every offline session — while the unit half stays honest
        # either way, because every unresolved unit is still listed under
        # `unverifiable` and render_text prints them.
        return
    path = state_file(Path(report["home"]))
    # Write-temp + rename in the same directory: os.replace is atomic, so
    # a reader (every PostToolUse) sees the old record or the new one,
    # never a truncated half — and a crash mid-write publishes nothing.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(report))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_cache(home: Path) -> dict | None:
    try:
        return json.loads(state_file(home).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def cached_report(home: Path, ttl: int) -> dict:
    """The --cached result: state file in, report out, NO other I/O.

    Never calls collect()/_remote_tip/_local_state, never spawns a
    subprocess, never writes — a missing or expired cache is REPORTED,
    not repaired, or every cold home's PostToolUse would become the live
    check this function exists to avoid.
    """
    raw = _load_cache(home)
    if raw is None:
        return {
            "schema": SCHEMA_VERSION,
            "home": str(home),
            "cache_state": CACHE_MISSING,
            "from_cache": True,
            "checked_units": [],
            "unverifiable": [],
            "notifications": [],
        }
    if time.time() - raw.get("checked_at", 0) > ttl:
        # Stale content rides under `stale`, never at the top level: the
        # exit code stays 0 and hook injection cannot present it as
        # current — it is shown only where render_text labels it [stale].
        return {
            "schema": SCHEMA_VERSION,
            "home": str(home),
            "cache_state": CACHE_EXPIRED,
            "from_cache": True,
            "checked_units": [],
            "unverifiable": [],
            "notifications": [],
            "stale": raw,
        }
    raw["from_cache"] = True
    raw["cache_state"] = CACHE_FRESH
    return raw


def render_text(report: dict) -> str:
    if report.get("home") is None:
        return f"skt check: {report['error']}"
    state = report.get("cache_state")
    if state == CACHE_MISSING:
        return "skt check: no cached result (cache missing) — refresh with: skt check"
    if state == CACHE_EXPIRED:
        stale = report.get("stale") or {}
        age = int(time.time() - stale.get("checked_at", 0))
        lines = [f"skt check: cached result is {age}s old (expired) — refresh with: skt check"]
        lines += [f"  [stale] {n['message']}" for n in stale.get("notifications", [])]
        return "\n".join(lines)
    notes = report["notifications"]
    unverifiable = report.get("unverifiable") or []
    stale_ref = report.get("upstream_stale") or []
    ahead_of_remote = report.get("ahead_of_remote") or []
    if not notes:
        scope = f"{len(report['checked_units'])} change-managed unit(s)"
        line = f"skt check: all current ({scope}, tier {report['tier']})"
        if unverifiable:
            line += f"; unverifiable: {', '.join(unverifiable)}"
        return "\n".join(
            [line, *_artifact_lines(report), *_ref_lines(stale_ref, ahead_of_remote)]
        )
    lines = [f"skt check: {len(notes)} notification(s), tier {report['tier']}"]
    for note in notes:
        lines.append(f"  {note['message']}")
        if note.get("kind") == "stale-artifact":
            # The command on its own line: this is the fast path for a
            # critical fix and it has to be retypable without editing.
            lines.append(f"    rebuild with: {note['fix']}")
        elif note.get("kind") == "cli-version":
            # Own line, same reason as the two below: this is the fast path
            # for the fix, and it has to be retypable without editing. The
            # second line says what to do AFTER, because an upgrade that
            # leaves the homes unreconciled is the migration half-done.
            lines.append(f"    upgrade with: {note['fix']}")
            lines.append("    then re-check this home: skt check")
        elif note.get("kind") == "home-migration":
            # Own line, retypable, and the reassurance with it: the command
            # rewrites entry points, which sounds alarming, and the reason it
            # is safe is a property of when it fires rather than of the flag.
            lines.append(f"    migrate with: {note['fix']}")
            lines.append("    safe: it only rewrites entries this home can "
                         "already run, and leaves the rest alone")
        elif note.get("kind") == "unit-error":
            # Same shape, and for the same reason — plus the home's own
            # recorded sentence, which names the remote, the branch and
            # the stash this summary only alludes to.
            if note.get("recorded"):
                lines.append(f"    recorded: {note['recorded']}")
            lines.append(f"    resolve with: {note['fix']}")
    lines += _artifact_lines(report)
    if unverifiable:
        lines.append(f"  unverifiable (remote unreachable): {', '.join(unverifiable)}")
    lines += _ref_lines(stale_ref, ahead_of_remote)
    if report.get("hint"):
        lines.append(f"  hint: {report['hint']}")
    return "\n".join(lines)


def _artifact_lines(report: dict) -> list[str]:
    """The overflow line, and the reason the probe found nothing.

    Both are one line. A stale-artifact notification that could not be
    made because the CLI predates the verb is worth exactly one sentence
    in the report an agent reads at every session start — and worth more
    than nothing, which is what silence would say.
    """
    state = report.get("artifacts") or {}
    kind = state.get("state")
    if kind == "ok":
        decided = (state.get("stale") or 0) + (state.get("current") or 0)
        if not decided and (state.get("unverifiable") or 0):
            # Rare and pathological, and it must not read as an all-clear:
            # every artifact answered "I could not be decided".
            return [
                f"  none of this home's {state.get('total', 0)} artifacts could be "
                f"decided — see: skill-manager artifacts stale --unverifiable"
            ]
        shown = len([n for n in report.get("notifications") or []
                     if n.get("kind") == "stale-artifact"])
        extra = (state.get("rebuildable") or 0) - shown
        if extra > 0:
            return [f"  +{extra} more stale artifact(s) — rebuild them with: skt build --stale"]
        return []
    if kind in (None, "off", "no-cli"):
        return []
    return [f"  artifacts not checked ({kind}): {state.get('reason', '')}"]


def _ref_lines(stale_ref: list, ahead_of_remote: list) -> list[str]:
    """State worth seeing, phrased so it cannot read as work to do."""
    lines = []
    if stale_ref:
        lines.append(
            f"  published, local ref behind (nothing to publish): {', '.join(stale_ref)}"
            f" — refresh with: git -C <store> fetch"
        )
    if ahead_of_remote:
        lines.append(
            f"  ahead of the remote tip (nothing to pull): {', '.join(ahead_of_remote)}"
        )
    return lines


def run(*, as_json: bool, cached: bool, ttl: int = DEFAULT_TTL_SECONDS, start: str | Path = ".") -> int:
    """Hook-safe: this function must never raise (SKT-6 wires it into sessions)."""
    try:
        if cached:
            home = homes.find_home(start)
            if home is None:
                report = {"schema": SCHEMA_VERSION, "home": None, "error": "no skill-manager home found"}
            else:
                report = cached_report(home, ttl)
        else:
            report = collect(start)
            _write_cache(report)
    except Exception as exc:  # noqa: BLE001 — a hook traceback inside an agent session is worse
        print(f"skt check: internal error ({type(exc).__name__}: {exc})")
        return 1
    print(json.dumps(report, indent=2) if as_json else render_text(report))
    if not report.get("home"):
        return 1
    return NOTIFY_EXIT if report["notifications"] else 0
