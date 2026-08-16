# /// script
# requires-python = ">=3.10"
# dependencies = ["testgraphsdk"]
#
# [tool.uv.sources]
# testgraphsdk = { path = "../sdk/python", editable = true }
# ///
"""skt.hook-contract — what the two shipped hooks leave behind.

`hooks/hooks.json` wires two shell hooks into every session that loads
the skt plugin, and both carry contracts a unit test cannot reach because
the contract IS the shell script, the environment it inherits, and the
file it appends to:

  SessionStart  appends EXACTLY ONE line to `<home>/logs/skt/hook.log`
                per invocation — the mode-independent proof that the
                startup report was present — and puts the report on
                stdout, which becomes session context. It NEVER exits
                non-zero: a broken orientation hook must not break the
                session it orients, so an unresolvable `skt` is a logged
                line and exit 0, not a failure.

  PostToolUse   surfaces the last check RESULT and never performs one.
                Its dedup key is (session, checked_at), so a fresh cache
                holding notifications injects ONCE and then stays silent
                for the rest of the TTL — not once per tool call. That
                regression was real (`fix: hook dedup is unconditional —
                the no-home-env session injected every tool call`), which
                is why it is driven here over five consecutive calls
                rather than one.

Both hooks are executed as the harness executes them: as scripts, with
an environment, against a real home on disk.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from testgraphsdk import NodeResult, NodeSpec, node

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from skt_fixture import (  # noqa: E402
    POST_TOOL_HOOK,
    SESSION_START_HOOK,
    build_home,
    child_env,
    init_repo,
    unit_record,
)

UPSTREAM = "skt.wrapper-installed"

SPEC = (
    NodeSpec("skt.hook-contract")
    .kind("assertion")
    .depends_on(UPSTREAM)
    .tags("skt", "hooks", "matrix")
    .timeout("600s")
    .side_effects("fs:tmp")
    .output("hookInvocations", "string")
)

SESSION_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z session-start session=(\S+) (\S+)$"
)
POST_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z post-tool session=(\S+) check-notified$"
)

NOTIFICATION = {
    "kind": "new-version",
    "unit": "alpha",
    "installed": "aaaaaaaa",
    "remote": "bbbbbbbb",
    "message": "new version available for alpha — pull with: skt sync alpha",
}


def _cache(home: Path, *, checked_at: float, notifications: list[dict]) -> None:
    path = home / "cache" / "skt-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "home": str(home),
                "tier": "project",
                "checked_units": ["alpha"],
                "unverifiable": [],
                "upstream_stale": [],
                "ahead_of_remote": [],
                "network": True,
                "checked_at": checked_at,
                "notifications": notifications,
            }
        )
    )


def _hook_output(emitted: str) -> dict:
    """The hook's `hookSpecificOutput` block, or {} if it emitted no such thing.

    Total, so the assertion above it can be unconditional: garbage on stdout
    and no stdout at all both become an empty block, which fails the assertion
    rather than raising out of the node.
    """
    try:
        payload = json.loads(emitted)
    except (json.JSONDecodeError, TypeError):
        return {}
    block = payload.get("hookSpecificOutput") if isinstance(payload, dict) else None
    return block if isinstance(block, dict) else {}


def _log_lines(home: Path) -> list[str]:
    log = home / "logs" / "skt" / "hook.log"
    if not log.is_file():
        return []
    return [line for line in log.read_text().splitlines() if line.strip()]


def _pin_wrapper(home: Path, wrapper: str) -> None:
    """Put the wrapper where `resolve_skt` looks for it FIRST.

    Both hooks prefer `$SKILL_MANAGER_HOME/bin/cli/skt` over anything on
    PATH, which is the branch a real home takes — so that is the branch
    exercised here.
    """
    target = home / "bin" / "cli"
    target.mkdir(parents=True, exist_ok=True)
    link = target / "skt"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(wrapper)


def _run_hook(script: Path, *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], cwd=str(cwd), capture_output=True, text=True, timeout=120, env=env
    )


@node(SPEC)
def main(ctx):
    result = NodeResult.pass_(ctx.node_id)
    wrappers = json.loads(ctx.get(UPSTREAM, "wrappers") or "{}")
    if not wrappers:
        return NodeResult.fail(ctx.node_id, f"{UPSTREAM} published no wrappers")

    work = ctx.report_dir / "fixtures" / "skt-hooks"
    work.mkdir(parents=True, exist_ok=True)
    repo = init_repo(work / "session-checkout")
    invocations = 0

    for version, wrapper in sorted(wrappers.items()):
        # ---------------------------------------------------------- SessionStart
        home = build_home(
            work / f"{version}-session-home",
            # Nothing here is change-managed, so the hook's one permitted
            # live refresh has no remote to reach: this node measures the
            # hook's contract, not a network.
            units=[unit_record("alpha"), unit_record("beta", kind="PLUGIN")],
            policy="live",
        )
        _pin_wrapper(home, wrapper)
        env = child_env(SKILL_MANAGER_HOME=str(home), CLAUDE_SESSION_ID="sess-alpha")

        first = _run_hook(SESSION_START_HOOK, cwd=repo, env=env)
        invocations += 1
        result.assertion(f"{version}: SessionStart exits zero", first.returncode == 0)
        result.assertion(
            f"{version}: SessionStart puts the status report on stdout",
            "skt status" in first.stdout and str(home) in first.stdout,
        )
        lines = _log_lines(home)
        result.assertion(f"{version}: one invocation appends one line", len(lines) == 1)
        match = SESSION_LINE.match(lines[0]) if lines else None
        result.assertion(
            f"{version}: the line carries the session id and the outcome",
            bool(match) and match.group(1) == "sess-alpha" and match.group(2) == "status-injected",
        )
        if not match and lines:
            result.log(f"{version}: unexpected hook.log line: {lines[0]!r}")

        second = _run_hook(SESSION_START_HOOK, cwd=repo, env=env)
        invocations += 1
        result.assertion(
            f"{version}: a second invocation appends a second line, not a replacement",
            second.returncode == 0 and len(_log_lines(home)) == 2,
        )

        # An UNRESOLVABLE skt is a logged line and exit 0. This is the
        # "never break the session" clause, and it is the only way to see
        # it: nothing about it is reachable from an import.
        blind = build_home(work / f"{version}-blind-home", units=[unit_record("alpha")])
        # `os.defpath` — the system directories and nothing else. It has
        # the coreutils the hook's own logging needs (`date`, `mkdir`) and
        # no `skt`, which is exactly the shape that makes `resolve_skt`
        # fall all the way through. A truly empty PATH would break `bash`
        # itself and prove nothing about the hook.
        result.assertion(
            f"{version}: the system PATH really has no skt",
            shutil.which("skt", path=os.defpath) is None,
        )
        blind_env = child_env(
            SKILL_MANAGER_HOME=str(blind),
            CLAUDE_SESSION_ID="sess-blind",
            PATH=os.defpath,
        )
        blind_run = _run_hook(SESSION_START_HOOK, cwd=repo, env=blind_env)
        invocations += 1
        blind_lines = _log_lines(blind)
        result.assertion(
            f"{version}: an unresolvable skt still exits zero and says why",
            blind_run.returncode == 0
            and len(blind_lines) == 1
            and blind_lines[0].endswith("skt-unresolvable")
            and blind_run.stdout.strip() == "",
        )

        # ---------------------------------------------------------- PostToolUse
        tool_home = build_home(
            work / f"{version}-tool-home", units=[unit_record("alpha")], policy="live"
        )
        _pin_wrapper(tool_home, wrapper)
        checked_at = time.time()
        _cache(tool_home, checked_at=checked_at, notifications=[NOTIFICATION])
        tool_env = child_env(SKILL_MANAGER_HOME=str(tool_home), CLAUDE_SESSION_ID="sess-tool")

        emissions = []
        for call in range(1, 6):
            run = _run_hook(POST_TOOL_HOOK, cwd=repo, env=tool_env)
            invocations += 1
            # NUMBERED. Five assertions sharing one name make a failure among
            # them unlocatable from the envelope, and "the third tool call
            # exited non-zero" is a different defect from "the first did".
            result.assertion(
                f"{version}: PostToolUse exits zero (tool call {call}/5)",
                run.returncode == 0,
            )
            if run.stdout.strip():
                emissions.append(run.stdout.strip())
        result.assertion(
            f"{version}: five tool calls over one check result inject exactly once",
            len(emissions) == 1,
        )
        # UNCONDITIONAL, so this node emits the same assertion SET whether it
        # passes or fails. Guarding it on `emissions` would make the count an
        # outcome, and a reader diffing two runs could not then tell a claim
        # that was skipped from one that was deleted.
        hook_out = _hook_output(emissions[0]) if emissions else {}
        result.assertion(
            f"{version}: the injection is a PostToolUse additionalContext block",
            hook_out.get("hookEventName") == "PostToolUse"
            and NOTIFICATION["message"] in hook_out.get("additionalContext", ""),
        )
        result.assertion(
            f"{version}: and appends exactly one post-tool line",
            len([line for line in _log_lines(tool_home) if POST_LINE.match(line)]) == 1,
        )

        # A NEW check result re-arms it. The dedup key is the result, not
        # the session — a session that never restarts must still see the
        # next check.
        _cache(tool_home, checked_at=checked_at + 1, notifications=[NOTIFICATION])
        rearm = _run_hook(POST_TOOL_HOOK, cwd=repo, env=tool_env)
        invocations += 1
        result.assertion(
            f"{version}: a new check result injects again",
            rearm.returncode == 0 and bool(rearm.stdout.strip()),
        )

        # A different session has its own marker.
        other = _run_hook(
            POST_TOOL_HOOK,
            cwd=repo,
            env=child_env(SKILL_MANAGER_HOME=str(tool_home), CLAUDE_SESSION_ID="sess-other"),
        )
        invocations += 1
        result.assertion(
            f"{version}: a second session gets its own injection",
            other.returncode == 0 and bool(other.stdout.strip()),
        )

        # EXPIRED cache: `check --cached` exits 0, so the hook must stay
        # silent. Stale notifications re-firing on every tool call for a
        # whole TTL is the failure this rule prevents.
        _cache(tool_home, checked_at=time.time() - 10_000, notifications=[NOTIFICATION])
        stale_env = child_env(
            SKILL_MANAGER_HOME=str(tool_home), CLAUDE_SESSION_ID="sess-stale"
        )
        stale = _run_hook(POST_TOOL_HOOK, cwd=repo, env=stale_env)
        invocations += 1
        result.assertion(
            f"{version}: an expired cache injects nothing",
            stale.returncode == 0 and stale.stdout.strip() == "",
        )

        # COLD home: no cache record at all. The hook must not perform the
        # check it is forbidden to perform.
        cold = build_home(work / f"{version}-cold-home", units=[unit_record("alpha")])
        _pin_wrapper(cold, wrapper)
        cold_run = _run_hook(
            POST_TOOL_HOOK,
            cwd=repo,
            env=child_env(SKILL_MANAGER_HOME=str(cold), CLAUDE_SESSION_ID="sess-cold"),
        )
        invocations += 1
        result.assertion(
            f"{version}: a cold home injects nothing and writes no cache",
            cold_run.returncode == 0
            and cold_run.stdout.strip() == ""
            and not (cold / "cache" / "skt-check.json").exists(),
        )

    result.assertion("hooks.json is valid JSON with both hook events", _hooks_json_ok())
    result.metric("hookInvocations", invocations)
    return result.publish("hookInvocations", str(invocations))


def _hooks_json_ok() -> bool:
    from skt_fixture import REPO_ROOT

    try:
        data = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks", {})
    if set(hooks) != {"SessionStart", "PostToolUse"}:
        return False
    post = hooks["PostToolUse"][0]["hooks"][0]
    # The 2s budget is the whole reason skt.check-cached-costs-one-read
    # exists. If it is ever raised, that node's claim stops being the
    # thing that makes this hook safe — so the number is asserted here.
    return post.get("timeout") == 2 and os.access(POST_TOOL_HOOK, os.X_OK)


if __name__ == "__main__":
    main()
