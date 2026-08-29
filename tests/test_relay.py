"""HBR-3 / skill-manager#264: skt relays a failure, it does not summarise it.

GOAL-the-real-error-survives asks one question — is the underlying CLI
error present in skt's rendered output for a seeded cross-home refusal? —
and its baseline is "absent, truncated". Every case here is that question
asked of one rendering site.

The seeded refusal is `LauncherShims`' own text, verbatim, exiting
`HOME_MISMATCH_EXIT_CODE` (79): a `bin/cli` shim binds the home it lives
in, so when `SKILL_MANAGER_HOME` names a different one it refuses rather
than editing the other silently.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import relay as relay_mod  # noqa: E402

#: `LauncherShims`' refusal, as a shim prints it. Both homes named.
REFUSAL = """\
skill-manager: refusing to run against a home you did not name.
  you named:  /repo/.skill-manager
  this shim would have edited: /Users/x/.skill-manager
  This entrypoint binds the home it lives in, so it cannot honour
  SKILL_MANAGER_HOME. Refusing rather than silently editing the
  other one.
  Say which one you mean:
    --home /Users/x/.skill-manager   (this shim's home)
    --home /repo/.skill-manager   (the home your environment names)
"""


def proc(stdout: str = "", stderr: str = "", code: int = 1):
    return subprocess.CompletedProcess(["cli"], code, stdout, stderr)


def rendered(relayed) -> str:
    return "\n".join(relay_mod.render(relayed))


def test_a_seeded_cross_home_refusal_reaches_the_reader():
    out = rendered(
        relay_mod.relay(
            "skill-manager",
            proc(stderr=REFUSAL, code=relay_mod.HOME_MISMATCH_EXIT),
            reason="home sync failed (exit 79)",
        )
    )
    assert "refusing to run against a home you did not name" in out
    # Both homes, which are the whole content of the refusal.
    assert "/repo/.skill-manager" in out
    assert "/Users/x/.skill-manager" in out
    # And it is not reported as a version problem, which is what #264 saw.
    assert "not out of date" in out


def test_the_refusal_survives_a_log_long_enough_to_be_elided():
    """The budget may drop the middle. It may not drop the cause.

    A refusal is emitted by a shim under a probe, deep inside a long
    bootstrap log — so "keep the head and the tail" alone would still lose
    it. The hoist is what makes the answer independent of where in the
    output it appeared.
    """
    filler = "\n".join(f"  step {i} ok" for i in range(400))
    relayed = relay_mod.relay(
        "bootstrap-home.sh",
        proc(stdout=f"{filler}\n{REFUSAL}\n{filler}", code=1),
        reason="home bootstrap failed (exit 1)",
    )
    assert relayed.elided > 0, "this fixture must exceed the budget to be a test"
    out = rendered(relayed)
    assert "refusing to run against a home you did not name" in out
    assert "this shim would have edited: /Users/x/.skill-manager" in out
    assert "omitted from the middle" in out, "an elision must announce itself"


def test_a_die_message_keeps_its_first_line_not_only_its_last():
    """The measured #264 rendering: the cause is the first line, and it went.

    `bootstrap-home.sh`'s `die` prints the diagnosis first and the
    consequences after, so `tail[-1]` kept "against the operator's global
    home." — a dangling fragment — and dropped the sentence that says what
    happened, plus the log path the script had already written.
    """
    die = (
        "log:       /tmp/bootstrap-home-A3Uj8x.log\n"
        "error: no skill-manager CLI with a `home` subcommand was found.\n"
        "    on PATH: /Users/x/.skill-manager/bin/cli/skill-manager "
        "(too old — `home clone` is missing)\n"
        "  Set SKILL_MANAGER_CLI to a build that has it, or install a newer "
        "skill-manager.\n"
        "  Without it a worktree cannot get its own home, and an agent would run\n"
        "  against the operator's global home.\n"
    )
    relayed = relay_mod.relay(
        "bootstrap-home.sh", proc(stderr=die, code=1), reason="home bootstrap failed"
    )
    out = rendered(relayed)
    assert "no skill-manager CLI with a `home` subcommand was found" in out
    assert relayed.log == "/tmp/bootstrap-home-A3Uj8x.log"
    assert "log:   /tmp/bootstrap-home-A3Uj8x.log" in out


def test_exit_79_is_named_even_when_the_words_were_swallowed():
    """`cli_has_home` pipes the shim into grep, so the TEXT can be gone.

    The exit code survives that pipeline when skt runs the shim itself, so
    a refusal with no output is still reported as a refusal rather than as
    an unexplained non-zero exit.
    """
    out = rendered(
        relay_mod.relay(
            "skill-manager",
            proc(code=relay_mod.HOME_MISMATCH_EXIT),
            reason="home sync failed",
        )
    )
    assert "cause:" in out and "cross-home" in out.lower()


def test_the_child_is_quoted_once_not_twice():
    relayed = relay_mod.relay(
        "skill-manager", proc(stderr=REFUSAL, code=79), reason="failed"
    )
    assert relayed.detail == (), "the hoist already carried every line"
    assert rendered(relayed).count("this shim would have edited") == 1


def test_an_ordinary_failure_keeps_the_whole_output_and_the_callers_fix():
    relayed = relay_mod.relay(
        "skill-manager",
        proc(stdout="line one\n", stderr="line two\n", code=3),
        reason="unit publish failed (exit 3)",
        fix="run it yourself",
        refusal_fix="never reached",
    )
    out = rendered(relayed)
    assert not relayed.refused
    assert "line one" in out and "line two" in out
    assert "fix:   run it yourself" in out


def test_the_refusal_fix_is_chosen_from_the_homes_the_refusal_names():
    """The remedy differs by WHICH home is wrong, so it is not guessed.

    A pin whose body names another home is regenerated; an environment
    that names a home the pin does not serve is what moves instead.
    """
    from skt.publish import _refusal_fix

    foreign = relay_mod.relay(
        "skill-manager",
        proc(stderr=REFUSAL, code=79),
        reason="failed",
        refusal_fix=_refusal_fix(Path("/repo/.skill-manager"), "skt sync debugging"),
    )
    assert "home shims --root /repo/.skill-manager" in foreign.fix
    assert "/Users/x/.skill-manager" in foreign.fix

    same_home = relay_mod.relay(
        "skill-manager",
        proc(stderr=REFUSAL, code=79),
        reason="failed",
        refusal_fix=_refusal_fix(Path("/Users/x/.skill-manager"), "skt sync debugging"),
    )
    assert "SKILL_MANAGER_HOME=/Users/x/.skill-manager skt sync debugging" in same_home.fix


def test_one_enormous_line_is_clipped_and_says_so():
    """"Lines" is not a bound on bytes: a JSON blob is one line."""
    relayed = relay_mod.relay(
        "skill-manager", proc(stdout="x" * 9000 + "\n", code=1), reason="failed"
    )
    body = "\n".join(relayed.detail)
    assert "more characters]" in body
    assert len(body) < 9000


def test_a_short_output_is_never_elided():
    relayed = relay_mod.relay(
        "skill-manager", proc(stderr="boom\n", code=2), reason="failed"
    )
    assert relayed.elided == 0
    assert relayed.detail == ("boom",)


def test_the_version_probe_reports_a_refusal_as_a_refusal(tmp_path, monkeypatch):
    """#264's first defect, in skt's own probe: exit != 0 is not a version.

    `skt check` asks this home's pin for `--version`; a shim that refuses a
    cross-home run answers non-zero, and reading that as "unreadable
    version" hands the operator a pin-rewriting remedy for a pin that is
    fine.
    """
    from skt import check as check_mod

    home = tmp_path / ".skill-manager"
    (home / "bin" / "cli").mkdir(parents=True)
    (home / "bin" / "cli" / "skill-manager").write_text("#!/bin/sh\nexit 79\n")

    monkeypatch.setattr(
        check_mod, "_run_git", lambda *a, **k: proc(stderr=REFUSAL, code=79)
    )
    version, why = check_mod._installed_cli_version(home, 5.0)
    assert version is None
    assert why.startswith(check_mod.CLI_REFUSED_PREFIX)
    assert "this shim would have edited: /Users/x/.skill-manager" in why

    state = check_mod._cli_state(home, _far_deadline())
    assert "home shims" not in state["fix"], "the pin is not what is wrong"
    assert "SKILL_MANAGER_HOME" in state["fix"]


def _far_deadline() -> float:
    import time

    return time.monotonic() + 60
