"""`skt check` — new-version, sync-with-root and stale-artifact notifications.

Pull-side (every tier): compare each change-managed unit's installed
gitHash against its remote tip (`git ls-remote`). Push-side (ROOT tier
only): a unit's store checkout that is dirty or ahead of its remote is
work nobody else can see — prompt the publish. Project homes are
updated from `wt`-created imports, so they get pull-side messages only.

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
the top of this file, so the cache-only path does not so much as load
the module that could spawn one.

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

from . import context as ctx_mod
from . import homes

# 2: the record gained `upstream_stale` and `ahead_of_remote`. Purely
# additive, and every reader uses `.get(...) or []`, so a v1 record still
# loads and nothing gates on the number — but the constant IS the
# record's description, and leaving it at 1 would have it describe a
# record that no longer exists.
# 3: the record gained `artifacts` (ARTI-10) and notifications of kind
# `stale-artifact`. Additive on the same terms: an older record has no
# `artifacts` key, `skt status` reads it with `.get`, and a reader that
# does not know the kind still gets a `message`.
SCHEMA_VERSION = 3
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


def _run_git(argv: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Bounded git call; None on deadline. Kills the child's WHOLE group.

    `subprocess.run(timeout=)` kills only the direct child; git spawns
    helpers (ssh, credential fillers) that inherit the pipes and keep
    the caller open past its budget. Own session + killpg reaps the lot.
    """
    if timeout <= 0:
        return None  # budget already spent: do not spawn at all
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
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
    }


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
                # extracting it from a sentence.
                "fix": f"skt build {name}",
            }
        )
    return out


def collect(start: str | Path = ".", *, use_network: bool = True) -> dict:
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
        if use_network:
            tip = tips.get(unit.name)
            if tip is None:
                unverifiable.append(unit.name)
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
        if tier == "root":
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
    # LAST, and from what the git phases have left of the shared deadline.
    # Ordered after them so an added probe cannot make the established
    # unit notifications later than they already were; the cost is that a
    # pass whose remotes ate the whole budget reports the artifacts
    # `timeout` rather than silently skipping them.
    artifacts = _artifact_state(home, deadline)
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
    artifacts_resolved = (report.get("artifacts") or {}).get("state") == "ok"
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
