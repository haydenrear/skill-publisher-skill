"""In-process probe: `skt check --cached` must cost ONE state-file read.

Run as `<pinned-python> cached_no_spawn_probe.py <repo-root> <workdir> <out.json>`.
Stdlib only, no testgraphsdk — it must run on a bare pinned interpreter.

WHY THIS IS A PROBE AND NOT AN ASSERTION ABOUT A SUBPROCESS
-----------------------------------------------------------
`hooks/hooks.json` gives the PostToolUse hook a 2-second timeout, and
that hook runs on EVERY tool call. "Fast enough when I measured it" is
not the contract; "spawns nothing" is. A wall-clock assertion on a
subprocess would pass on a quiet laptop and fail on a loaded runner
while measuring neither.

So the check runs INSIDE this interpreter with two independent
instruments, and both must agree:

  1. `sys.addaudithook` — CPython raises `subprocess.Popen`, `os.system`,
     `os.exec`, `os.fork`, `os.posix_spawn` and `os.spawn` from the C
     layer, so a spawn is caught no matter which module reaches it, and
     `open` is raised for every file the code opens. This is the
     instrument that cannot be routed around.
  2. Poisoned callables — `subprocess.*`, `os.fork`/`spawn*`/`system`,
     `ThreadPoolExecutor` and `threading.Thread` are replaced after
     import, so a regression produces a NAMED failure rather than a
     count. `check.run` swallows every exception by design (a traceback
     inside an agent session is worse than a bad answer), which is
     precisely why the audit hook is the primary instrument: a poisoned
     call that `run` catches still leaves its audit record.

Five cache states are driven, plus the two that share an answer:
no-home, missing, corrupt, expired, fresh-quiet, fresh-notify. All of
them read the state file at most once and none of them writes it.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(sys.argv[1]).resolve()
WORKDIR = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3])

sys.path.insert(0, str(REPO_ROOT / "src"))

from skt import check as check_mod  # noqa: E402
from skt import homes as homes_mod  # noqa: E402

TTL = 900

# Audit events that mean "a new process was, or was about to be, created".
# `os.exec`/`os.spawn`/`os.posix_spawn` cover the paths that never touch
# the `subprocess` module at all.
SPAWN_EVENTS = (
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.fork",
    "os.forkpty",
    "os.posix_spawn",
    "os.spawn",
    "os.startfile",
    "pty.spawn",
)

_armed = False
_spawns: list[str] = []
_opens: list[str] = []


def _audit(event: str, args) -> None:
    if not _armed:
        return
    if event == "open":
        try:
            _opens.append(str(args[0]))
        except Exception:  # noqa: BLE001 — the hook must never raise
            _opens.append("<unprintable>")
    elif event in SPAWN_EVENTS:
        _spawns.append(event)


sys.addaudithook(_audit)


class SpawnAttempt(RuntimeError):
    """A poisoned callable was reached on the --cached path."""


def _poison(name: str):
    def refuse(*_args, **_kwargs):
        _spawns.append(f"poisoned:{name}")
        raise SpawnAttempt(f"--cached reached {name}")

    return refuse


_ORIGINALS: dict[str, object] = {}


def poison_everything() -> None:
    import concurrent.futures
    import subprocess
    import threading

    _ORIGINALS["subprocess.Popen"] = subprocess.Popen
    for attr in (
        "Popen", "run", "call", "check_call", "check_output",
        "getoutput", "getstatusoutput",
    ):
        if hasattr(subprocess, attr):
            setattr(subprocess, attr, _poison(f"subprocess.{attr}"))
    for attr in (
        "system", "fork", "forkpty", "popen", "posix_spawn", "posix_spawnp",
        "execv", "execve", "execvp", "execvpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle",
        "spawnlp", "spawnlpe",
    ):
        if hasattr(os, attr):
            setattr(os, attr, _poison(f"os.{attr}"))
    concurrent.futures.ThreadPoolExecutor = _poison("ThreadPoolExecutor")
    threading.Thread = _poison("threading.Thread")


def cache_record(*, checked_at: float, notifications: list[dict]) -> dict:
    return {
        "schema": check_mod.SCHEMA_VERSION,
        "home": "",  # filled per scenario
        "tier": "project",
        "checked_units": ["alpha", "beta"],
        "unverifiable": [],
        "upstream_stale": [],
        "ahead_of_remote": [],
        "network": True,
        "checked_at": checked_at,
        "notifications": notifications,
    }


NOTIFICATION = {
    "kind": "new-version",
    "unit": "alpha",
    "installed": "aaaaaaaa",
    "remote": "bbbbbbbb",
    "message": "new version available for alpha — pull with: skt sync alpha",
}


def make_home(name: str) -> Path:
    home = WORKDIR / name
    (home / "installed").mkdir(parents=True, exist_ok=True)
    (home / "installed" / "alpha.json").write_text(
        json.dumps({"name": "alpha", "version": "1.0.0", "unitKind": "SKILL"})
    )
    return home


def write_cache(home: Path, payload) -> Path:
    path = check_mod.state_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload)
    else:
        payload = dict(payload)
        payload["home"] = str(home)
        path.write_text(json.dumps(payload))
    return path


def measure(fn):
    """Run `fn` with the instruments armed; return (value, spawns, opens)."""
    global _armed
    _spawns.clear()
    _opens.clear()
    _armed = True
    try:
        value = fn()
    finally:
        _armed = False
    return value, list(_spawns), list(_opens)


def scenario(name: str, home: Path | None, start: Path, *, as_json: bool = True) -> dict:
    if home is None:
        os.environ.pop("SKILL_MANAGER_HOME", None)
        os.environ["SKT_ROOT_HOME"] = str(WORKDIR / "no-such-root-home")
    else:
        os.environ["SKILL_MANAGER_HOME"] = str(home)
        os.environ["SKT_ROOT_HOME"] = str(WORKDIR / "no-such-root-home")

    state = check_mod.state_file(home) if home is not None else None
    before = state.read_bytes() if state is not None and state.is_file() else None

    buffer = io.StringIO()

    def call():
        with redirect_stdout(buffer):
            return check_mod.run(as_json=as_json, cached=True, ttl=TTL, start=str(start))

    rc, spawns, opens = measure(call)
    printed = buffer.getvalue()
    after = state.read_bytes() if state is not None and state.is_file() else None

    report = {}
    if as_json:
        try:
            report = json.loads(printed)
        except json.JSONDecodeError:
            report = {}

    state_reads = [p for p in opens if state is not None and Path(p) == state]
    return {
        "scenario": name,
        "exit_code": rc,
        "spawns": spawns,
        "opens": opens,
        "state_file_reads": len(state_reads),
        "other_opens": [p for p in opens if state is None or Path(p) != state],
        "cache_state": report.get("cache_state"),
        "notifications": report.get("notifications"),
        "has_stale": "stale" in report,
        "stale_notifications": (report.get("stale") or {}).get("notifications"),
        "error": report.get("error"),
        "printed": printed if not as_json else "",
        "cache_unchanged": before == after,
    }


def main() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    now = time.time()

    # WARM-UP, unarmed. The first call through this code path imports
    # nothing new, but the interpreter may still lazily open a codec or a
    # locale file, and an `open` charged to the interpreter's first run
    # would be counted against skt. One unmeasured pass removes that.
    warm = make_home("warm-home")
    write_cache(warm, cache_record(checked_at=now, notifications=[]))
    os.environ["SKILL_MANAGER_HOME"] = str(warm)
    with redirect_stdout(io.StringIO()):
        check_mod.run(as_json=True, cached=True, ttl=TTL, start=str(WORKDIR))
        check_mod.run(as_json=False, cached=True, ttl=TTL, start=str(WORKDIR))

    poison_everything()

    results = []

    # 1. no home at all — nothing to read, and no attempt to find one on
    #    disk beyond stat()ing ancestors. A system temp dir is used
    #    because every directory inside this checkout has a real
    #    `.skill-manager` above it.
    orphan = Path(tempfile.mkdtemp(prefix="skt-no-home-"))
    results.append(scenario("no-home", None, orphan))

    # 2. a home with no cache record at all — the COLD home, which is the
    #    case a per-tool-call hook meets first.
    missing = make_home("missing-home")
    results.append(scenario("missing", missing, WORKDIR))

    # 3. a cache record that is not JSON. `_load_cache` folds it into the
    #    same answer as `missing`: reported, never repaired.
    corrupt = make_home("corrupt-home")
    write_cache(corrupt, "{ this is not json")
    results.append(scenario("corrupt", corrupt, WORKDIR))

    # 4. a record past the TTL. Its content must ride under `stale` and
    #    never at the top level.
    expired = make_home("expired-home")
    write_cache(expired, cache_record(checked_at=now - 10_000, notifications=[NOTIFICATION]))
    results.append(scenario("expired", expired, WORKDIR))
    results.append(scenario("expired-text", expired, WORKDIR, as_json=False))

    # 5. fresh, nothing to report.
    quiet = make_home("fresh-quiet-home")
    write_cache(quiet, cache_record(checked_at=now, notifications=[]))
    results.append(scenario("fresh-quiet", quiet, WORKDIR))

    # 6. fresh, with notifications — the only state that exits 10.
    notify = make_home("fresh-notify-home")
    write_cache(notify, cache_record(checked_at=now, notifications=[NOTIFICATION]))
    results.append(scenario("fresh-notify", notify, WORKDIR))

    # ------------------------------------------------------------------
    # CONTROLS. An instrument that reports "no spawn" is worth nothing
    # until it is shown to report a spawn when there is one, so both
    # instruments are made to fire against this same code, in this same
    # process, on this same run.
    #
    # control-audit: the audit hook alone, against the REAL Popen that was
    #   saved before poisoning. If this comes back empty the hook is not
    #   installed and every "spawns: []" above is vacuous.
    def _real_spawn():
        popen = _ORIGINALS["subprocess.Popen"]
        proc = popen([sys.executable, "-c", "pass"])  # type: ignore[operator]
        proc.wait()
        return 0

    _, control_spawns, _ = measure(_real_spawn)
    results.append(
        {
            "scenario": "control-audit-sees-a-real-spawn",
            "exit_code": 0,
            "spawns": control_spawns,
            "opens": [],
            "state_file_reads": 0,
            "other_opens": [],
            "cache_state": None,
            "notifications": None,
            "has_stale": False,
            "stale_notifications": None,
            "error": None,
            "printed": "",
            "cache_unchanged": True,
        }
    )

    # control-live: the LIVE path of the very function under test. `skt
    #   check` without --cached classifies the tier, which shells out to
    #   git. If this comes back empty then --cached and the live path are
    #   indistinguishable to this probe and the six results above prove
    #   nothing about `--cached` in particular.
    def _live():
        with redirect_stdout(io.StringIO()):
            return check_mod.run(as_json=True, cached=False, ttl=TTL, start=str(WORKDIR))

    os.environ["SKILL_MANAGER_HOME"] = str(quiet)
    live_rc, live_spawns, _ = measure(_live)
    results.append(
        {
            "scenario": "control-live-path-does-spawn",
            "exit_code": live_rc,
            "spawns": live_spawns,
            "opens": [],
            "state_file_reads": 0,
            "other_opens": [],
            "cache_state": None,
            "notifications": None,
            "has_stale": False,
            "stale_notifications": None,
            "error": None,
            "printed": "",
            "cache_unchanged": True,
        }
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "executable": sys.executable,
                "results": results,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
