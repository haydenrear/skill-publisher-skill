"""`skt ticket list|sweep` — the fleet verbs.

Real git worktrees, faked skill-manager. That split follows the suite's
existing idiom (test_check/test_ticket_publish build real repos and clone
real stores, and fake the CLI with a recording bash pin), and it is also
the split that matters here: the porcelain parsing, the stash
attribution, the upstream/containment arithmetic and `git worktree
remove`'s own refusals are the behaviour under test and a mock of git
would only assert that the mock was written the way the code was. The
home gate is somebody else's program, so it is stubbed.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import sweep as sweep_mod  # noqa: E402
from skt import ticket as ticket_mod  # noqa: E402

from test_check import GIT  # noqa: E402
from test_status import make_home, make_repo  # noqa: E402

#: Captured BEFORE the `isolate` fixture below replaces `shutil.which`
#: with a stub. The real-binary tests at the bottom need the real one;
#: every other test needs it gone, because `resolve_gate_cli` falls back
#: to PATH by design and a developer's own skill-manager must never be
#: the CLI a stubbed gate runs.
_REAL_WHICH = shutil.which


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.delenv("SKILL_MANAGER_CLI", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-root" / ".skill-manager"))
    # A real skill-manager on the developer's PATH must never be the CLI a
    # test's gate runs: `resolve_gate_cli` falls back to PATH by design.
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)


# --------------------------------------------------------------- the fixture


# The SHIPPED CLI's payload shape, measured against
# `skill-manager home close-out --json`: the verdict key is `safe`, beside
# `exitCode`, `units` and `blockers`. `clean` is what git-issue-workflow's
# complete.md documents and is what the `not_a_home` refusal emits, so
# `_verdict_says_unsafe` honours both and requires neither.
CLEAN_VERDICT = json.dumps(
    {"home": "/w/.skill-manager", "into": "/r/.skill-manager", "safe": True,
     "exitCode": 0, "units": [], "blockers": []}
)

BLOCKED_VERDICT = json.dumps(
    {
        "home": "/w/.skill-manager",
        "into": "/r/.skill-manager",
        "safe": False,
        "exitCode": 1,
        "units": [{"unit": "skill:alpha", "status": "dirty", "detail": "SKILL.md"}],
        "blockers": [
            {
                "unit": "skill:alpha",
                "status": "conflicted",
                "detail": "1 file(s) changed on both sides; nothing was written",
                "conflicts": ["skill-manager.toml"],
                "remedy": "skill-manager unit publish alpha --ticket T-1",
            }
        ],
    }
)

# The refusal the shipped CLI actually emits for a path that is not a home.
NOT_A_HOME_VERDICT = json.dumps(
    {
        "error": "not_a_home",
        "path": "/w",
        "message": "home close-out --home: /w is not a Skill Manager home. If you meant "
                   "the home inside a worktree, name it: /w/.skill-manager. Nothing was "
                   "read and nothing was written.",
        "safe": False,
        "clean": False,
        "blockers": [],
        "units": [],
        "exitCode": 2,
    }
)


def gate_cli(home: Path, *, verdict: str = CLEAN_VERDICT, exit_code: int = 0) -> Path:
    """A `skill-manager` pin whose `home close-out` the test controls.

    It answers the `--help` probe `resolve_gate_cli` uses (on `--into`,
    the way close-change.sh probes it), logs every invocation, and then
    emits the verdict the test asked for.
    """
    log = home / "cli-calls.log"
    cli = home / "bin" / "cli" / "skill-manager"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'case "$*" in *--help*) echo "      --into=<into>   The project home"; exit 0;; esac\n'
        f'echo "$@" >> "{log}"\n'
        f"cat <<'VERDICT'\n{verdict}\nVERDICT\n"
        f"exit {exit_code}\n"
    )
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    return log


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*GIT, *(["-C", str(cwd)] if cwd else []), *args],
        capture_output=True, text=True, check=True,
    )


def epic_repo(
    tmp_path, *, verdict: str = CLEAN_VERDICT, exit_code: int = 0, epic: bool = True
) -> dict:
    """A primary checkout with an origin, an `epic/demo` branch, and a home.

    `.skill-manager` is gitignored, the way every real repo has it: the
    home is not part of the repository's working-tree state and the
    close-out gate — not `status --porcelain` — is what assesses it.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    root = make_repo(tmp_path / "repo")
    (root / ".gitignore").write_text(".skill-manager/\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "ignore the home", cwd=root)
    git("remote", "add", "origin", str(origin), cwd=root)
    git("push", "-q", "-u", "origin", "main", cwd=root)
    if epic:
        git("branch", "epic/demo", "main", cwd=root)
        git("push", "-q", "origin", "epic/demo", cwd=root)
    home = make_home(root, units={})
    log = gate_cli(home, verdict=verdict, exit_code=exit_code)
    return {"root": root, "origin": origin, "home": home, "log": log}


def add_worktree(
    repo: dict,
    ticket: str,
    *,
    base: str = "epic/demo",
    commit: bool = False,
    push: bool = False,
    dirty: bool = False,
    stash: bool = False,
    home: bool = True,
) -> Path:
    root = repo["root"]
    path = root.parent / f"wt-{ticket}"
    git("worktree", "add", "-q", str(path), "-b", f"feature/{ticket}", base, cwd=root)
    if home:
        (path / ".skill-manager" / "installed").mkdir(parents=True)
        (path / ".skill-manager" / "home.runtime.json").write_text("{}")
    if commit:
        (path / f"{ticket}.txt").write_text("work\n")
        git("add", "-A", cwd=path)
        git("commit", "-q", "-m", f"{ticket} work", cwd=path)
    if push:
        git("push", "-q", "-u", "origin", f"feature/{ticket}", cwd=path)
    if stash:
        (path / "README.md").write_text("stashed\n")
        git("stash", "-q", cwd=path)
    if dirty:
        (path / "README.md").write_text("uncommitted\n")
    return path


# ------------------------------------------------------------------- reading


def test_list_is_read_only_and_names_ticket_branch_and_flags(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    clean = add_worktree(repo, "T-1")
    dirty = add_worktree(repo, "T-2", dirty=True)

    assert ticket_mod.run("list", None, start=repo["root"]) == 0
    out = capsys.readouterr().out
    assert "T-1" in out and "T-2" in out
    assert "feature/T-1" in out
    assert "epic       demo (discovered; the candidate set is NOT narrowed)" in out
    assert "target     epic/demo" in out
    assert "clean" in out and "BLOCKED" in out
    assert "1 uncommitted path(s)" in out
    assert clean.is_dir() and dirty.is_dir(), "list removes nothing"


def test_list_json_carries_the_flags(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-1", commit=True)

    assert ticket_mod.run("list", None, start=repo["root"], as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {r["ticket"]: r for r in payload["worktrees"]}
    assert rows[None]["primary"] is True
    row = rows["T-1"]
    assert row["branch"] == "feature/T-1"
    assert row["status"]["unpushed"] == 1
    assert row["status"]["not_in_target"] == 1
    assert row["status"]["clean"] is False
    assert payload["target"] == "epic/demo"


def test_list_reports_a_worktree_whose_directory_is_gone(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-1")
    shutil.rmtree(path)

    assert ticket_mod.run("list", None, start=repo["root"]) == 0
    out = capsys.readouterr().out
    assert "MISSING" in out or "prunable" in out


def test_list_refuses_a_ticket_argument_and_suggests_epic(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    assert ticket_mod.run("list", "T-1", start=repo["root"]) == 1
    assert "--epic T-1" in capsys.readouterr().err


# ------------------------------------------------------------ dry run default


def test_sweep_refuses_by_default_and_removes_nothing(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    clean = add_worktree(repo, "T-1")
    dirty = add_worktree(repo, "T-2", dirty=True)

    assert ticket_mod.run("sweep", None, start=repo["root"]) == 0
    out = capsys.readouterr().out
    assert "(dry run)" in out
    assert "would remove" in out
    assert "SKIPPED" in out
    assert "NOTHING was removed" in out
    assert "--yes" in out
    assert "gate is NOT run by a dry run" in out, "the dry run is honest about what it did not ask"
    assert clean.is_dir() and dirty.is_dir()
    assert not repo["log"].exists(), "the dry run does not even run the gate"


def test_sweep_dry_run_json_marks_dry_run(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-1")

    assert ticket_mod.run("sweep", None, start=repo["root"], as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["summary"] == {
        "planned": 1, "removed": 0, "skipped": 0, "failed": 0, "excluded": 1,
    }


# -------------------------------------------------------------- safety skips


def test_dirty_worktree_is_skipped_not_removed(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    dirty = add_worktree(repo, "T-2", dirty=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    out = capsys.readouterr().out
    assert "SKIPPED" in out
    assert "uncommitted path(s)" in out
    assert "0 removed, 1 skipped for safety" in out
    assert dirty.is_dir(), "a dirty worktree survives the sweep"
    assert not repo["log"].exists(), "and the gate is never even asked about it"


def test_unpushed_commits_are_skipped(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-3", commit=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    out = capsys.readouterr().out
    assert "not pushed to" in out
    assert path.is_dir()


def test_commits_not_contained_in_the_epic_branch_are_skipped(tmp_path, capsys):
    """Pushed, so nothing is at risk — but it never landed on the epic."""
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-4", commit=True, push=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    out = capsys.readouterr().out
    assert "not pushed to" not in out
    assert "not contained in epic/demo" in out
    assert path.is_dir()


def test_a_stash_is_attributed_to_the_worktree_that_made_it(tmp_path, capsys):
    """`refs/stash` is SHARED, so attribution is the whole difficulty.

    A stash pushed in one worktree is listed by `git stash list` in every
    sibling and in the primary. Counting it per worktree would make one
    unrelated stash refuse the entire sweep, so entries are attributed by
    the branch in their own reflog subject.
    """
    repo = epic_repo(tmp_path)
    stashed = add_worktree(repo, "T-5", stash=True)
    innocent = add_worktree(repo, "T-6")
    # The premise: git shows the same stash from the innocent worktree.
    listed = subprocess.run(
        ["git", "-C", str(innocent), "stash", "list"], capture_output=True, text=True, check=True
    ).stdout
    assert "feature/T-5" in listed

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    out = capsys.readouterr().out
    assert "stash entr" in out
    assert stashed.is_dir(), "the worktree that made the stash is skipped"
    assert not innocent.is_dir(), "its sibling is NOT punished for the shared ref"
    assert "1 removed, 1 skipped for safety" in out


def test_the_safety_gate_is_re_run_immediately_before_each_removal(tmp_path, capsys):
    """The plan is minutes old by removal time; the second answer decides.

    A sweep over a dozen homes spends minutes in the close-out gate, and
    an agent committing in one of them mid-pass is the normal case. So a
    worktree that was clean when the plan was printed and is dirty when
    its turn comes must be skipped.
    """
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-7")

    real_inspect = sweep_mod.inspect
    calls: list[str] = []

    def inspect_then_dirty(wt, *, root, target):
        calls.append(str(wt.path))
        status = real_inspect(wt, root=root, target=target)
        if len(calls) > 1:  # the pre-removal re-check
            status = sweep_mod.Status(dirty=("M README.md",), target=target)
        return status

    sweep_mod.inspect = inspect_then_dirty
    try:
        assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    finally:
        sweep_mod.inspect = real_inspect

    out = capsys.readouterr().out
    assert len(calls) == 2, f"measured for the plan AND again before removal: {calls}"
    assert "0 removed, 1 skipped for safety" in out
    assert path.is_dir()


# ------------------------------------------------- the primary and the self


def test_the_primary_checkout_is_never_a_candidate(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    out = capsys.readouterr().out
    assert "the primary checkout — never removed" in out
    assert repo["root"].is_dir()


def test_sweep_refuses_the_worktree_it_is_running_in(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    here = add_worktree(repo, "T-8")

    assert ticket_mod.run("sweep", None, start=here, yes=True) == 0
    out = capsys.readouterr().out
    assert "running IN" in out
    assert str(repo["root"]) in out, "and it names the primary checkout to run from"
    assert here.is_dir()
    assert not repo["log"].exists(), "no gate, no removal"


def test_the_destination_home_comes_from_the_primary_not_from_pwd(tmp_path, capsys):
    """The trap this command exists not to fall into.

    Run from inside a worktree, `homes.find_home(".")` resolves that
    worktree's OWN home — and a close-out gate whose --home and --into
    are the same directory reports clean for everything.
    """
    repo = epic_repo(tmp_path)
    here = add_worktree(repo, "T-9")
    victim = add_worktree(repo, "T-10")

    assert ticket_mod.run("sweep", None, start=here, yes=True) == 0
    logged = repo["log"].read_text()
    assert f"--home {victim / '.skill-manager'}" in logged
    assert f"--into {repo['home']}" in logged
    assert str(here / ".skill-manager") not in logged.split("--into")[1]
    assert not victim.is_dir()


def test_into_overrides_the_destination_home(tmp_path, capsys):
    """--into names the home the verdict is ABOUT, not the CLI that runs.

    The destination here has no CLI pin of its own; the primary
    checkout's is used, and the gate is still asked about `--into`.
    """
    repo = epic_repo(tmp_path)
    wt = add_worktree(repo, "T-11")
    other = make_home(tmp_path / "elsewhere", units={})

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True, into=str(other)) == 0
    logged = repo["log"].read_text()
    assert f"--into {other}" in logged
    assert f"--into {repo['home']}" not in logged
    assert not wt.is_dir()


# --------------------------------------------------------------- the gate


def test_a_gate_refusal_skips_and_prints_the_clis_own_remedy(tmp_path, capsys):
    repo = epic_repo(tmp_path, verdict=BLOCKED_VERDICT, exit_code=1)
    path = add_worktree(repo, "T-12")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    out = capsys.readouterr().out
    assert "the home gate refused" in out
    assert "skill-manager unit publish alpha --ticket T-1" in out, "verbatim, not re-derived"
    assert "conflict  skill-manager.toml" in out
    assert "nothing was written" in out, "the CLI's own detail, not a re-derivation"
    assert path.is_dir()


def test_gate_exit_2_is_a_failure_and_names_the_not_a_home_trap(tmp_path, capsys):
    """Exit 2 means NOTHING was assessed — it must not read as "not clean"."""
    repo = epic_repo(tmp_path, verdict=NOT_A_HOME_VERDICT, exit_code=2)
    path = add_worktree(repo, "T-13")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 1
    out = capsys.readouterr().out
    assert "not a Skill Manager home (exit 2)" in out
    assert "NOTHING about this worktree was assessed" in out
    assert "If you meant the home inside a worktree" in out, \
        "the CLI already wrote the best sentence about this; do not re-derive it"
    assert "1 failed" in out
    assert path.is_dir()


def test_either_spelling_of_the_verdict_can_refuse(tmp_path):
    """`safe` and `clean` are both honoured; neither is required.

    Reading only one of them would make a refusal written in the other
    spelling read as consent — the one direction this must never fail in.
    """
    assert sweep_mod._verdict_says_unsafe({"safe": False}) is True
    assert sweep_mod._verdict_says_unsafe({"clean": False}) is True
    assert sweep_mod._verdict_says_unsafe({"safe": True, "clean": True}) is False
    assert sweep_mod._verdict_says_unsafe({}) is False, "silence is not a refusal"


def test_a_verdict_that_says_unsafe_at_exit_0_still_skips(tmp_path, capsys):
    """The document outranks a 0 that disagrees with it."""
    repo = epic_repo(tmp_path, verdict='{"safe": false, "blockers": [], "units": []}', exit_code=0)
    path = add_worktree(repo, "T-13b")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    assert "the home gate refused" in capsys.readouterr().out
    assert path.is_dir()


def test_gate_exit_9_abandons_the_pass_and_exits_9(tmp_path, capsys):
    """A frozen destination is one fact about the destination, not N facts."""
    repo = epic_repo(tmp_path, verdict="", exit_code=9)
    first = add_worktree(repo, "T-14")
    second = add_worktree(repo, "T-15")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 9
    out = capsys.readouterr().out
    assert "policy `frozen`" in out
    assert "ABANDONED" in out
    assert "1 further worktree(s) were never assessed" in out, \
        "an abandoned summary must not read as a complete one"
    assert "home policy live" in out
    assert first.is_dir() and second.is_dir()


def test_a_gate_that_cannot_run_refuses_the_whole_sweep(tmp_path, capsys):
    """Nothing established must never be spent as "safe"."""
    repo = epic_repo(tmp_path)
    (repo["home"] / "bin" / "cli" / "skill-manager").unlink()
    path = add_worktree(repo, "T-16")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 1
    out = capsys.readouterr().out
    assert "nothing was removed" in out
    assert path.is_dir()


def test_a_worktree_with_no_home_is_removed_and_says_so(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-17", home=False)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    out = capsys.readouterr().out
    assert "no Skill Manager home at" in out
    assert not path.is_dir()
    assert not repo["log"].exists(), "there was nothing for the gate to assess"


# ------------------------------------------------------- summary and removal


def test_a_clean_worktree_is_removed_with_a_summary_and_a_space_delta(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-18")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    out = capsys.readouterr().out
    assert "removed" in out
    assert "1 removed, 0 skipped for safety, 0 failed" in out
    assert "free space" in out
    assert "copy-on-write" in out, "the du caveat is stated where the number is"
    assert not path.is_dir()
    assert "branch feature/T-18 kept" in out


def test_removal_never_passes_force(tmp_path, capsys):
    """`--force` is the flag whose purpose is to make a blocker go away."""
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-19")
    calls: list[list[str]] = []
    real_run = sweep_mod._run

    def recording(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)

    sweep_mod._run = recording
    try:
        assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    finally:
        sweep_mod._run = real_run

    removals = [c for c in calls if c[:3] == ["git", "worktree", "remove"]]
    assert removals, "a removal happened"
    assert all("--force" not in c and "-f" not in c for c in removals)
    assert any(c[:3] == ["git", "worktree", "prune"] for c in calls), "prune runs after the pass"
    assert not any("rm" == Path(c[0]).name for c in calls), "never rm"


def test_sweep_json_summary_and_space_keys(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-20")
    add_worktree(repo, "T-21", dirty=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True, as_json=True) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["removed"] == 1
    assert payload["summary"]["skipped"] == 1
    assert payload["summary"]["failed"] == 0
    assert payload["pruned"] is True
    assert payload["free_bytes_delta"] is not None
    assert "du" in payload["size_note"]
    assert payload["exit_code"] == 4


def test_free_space_is_measured_not_computed_from_du(tmp_path):
    """`os.statvfs` before and after — the only honest size on a CoW clone."""
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-22")
    result = sweep_mod.sweep(start=repo["root"], yes=True)
    assert result.free_before is not None and result.free_after is not None
    assert result.free_delta == result.free_after - result.free_before
    text = sweep_mod.render_sweep(result)
    assert "du" in text and "~30x" in text
    assert " per-worktree" not in text.lower().split("free space")[0]


# ------------------------------------------------------------------- scoping


def test_epic_scoping_limits_the_pass(tmp_path, capsys):
    """No ticket plan here, so `--epic` falls back to fork-point containment.

    Which means the epic branches have to have actually diverged — the
    documented weakness of that fallback, and the reason a DISCOVERED
    slug never narrows anything.
    """
    repo = epic_repo(tmp_path)
    root = repo["root"]
    for slug in ("demo", "other"):
        git("checkout", "-q", f"epic/{slug}", cwd=root) if slug == "demo" else git(
            "checkout", "-q", "-b", "epic/other", "main", cwd=root
        )
        (root / f"{slug}.txt").write_text(slug)
        git("add", "-A", cwd=root)
        git("commit", "-q", "-m", f"{slug} epic work", cwd=root)
    git("checkout", "-q", "main", cwd=root)

    in_epic = add_worktree(repo, "T-23", base="epic/demo")
    other = root.parent / "wt-other"
    git("worktree", "add", "-q", str(other), "-b", "feature/X-9", "epic/other", cwd=root)

    assert ticket_mod.run("sweep", None, start=root, epic="demo") == 0
    out = capsys.readouterr().out
    assert "not part of epic demo" in out
    assert "would remove" in out and "T-23" in out
    assert in_epic.is_dir() and other.is_dir()


def test_epic_scoping_uses_the_ticket_plan_on_the_epic_branch(tmp_path, capsys):
    """The plan lives on `epic/demo`; the sweep runs from `main`.

    Read with `git show <ref>:<path>` for exactly that reason.
    """
    root = epic_repo(tmp_path)["root"]
    git("checkout", "-q", "epic/demo", cwd=root)
    plan = root / "specs" / "desired_program_model"
    plan.mkdir(parents=True)
    (plan / "ticket_plan.yaml").write_text(
        "name: demo\ntickets:\n  - id: T-24\n    status: open\n"
    )
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "plan", cwd=root)
    git("checkout", "-q", "main", cwd=root)

    assert sweep_mod.epic_ticket_ids(root, "epic/demo") == {"T-24"}


def test_an_unknown_epic_is_refused_rather_than_silently_widened(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-25")

    assert ticket_mod.run("sweep", None, start=repo["root"], epic="nope", yes=True) == 1
    out = capsys.readouterr().out
    assert "no branch epic/nope" in out
    assert "git fetch origin" in out


def test_an_uncheckable_containment_refuses_rather_than_warning(tmp_path, capsys):
    """The hard stop git-epic-workflow §5 promises, actually enforced.

    This inverts `test_no_target_branch_warns_that_containment_was_not_
    checked`, which pinned the old behaviour deliberately. It was wrong:
    §5 (`worktree-lifecycle.md:301-305`) makes epic-unmerged work a hard
    stop BEFORE any removal, and a stop that only happens when somebody
    remembered `--epic` is not one. In a repository where no single
    `epic/*` is discoverable, the shipped warning let a bare
    `skt ticket sweep --yes` remove worktrees whose commits were pushed
    and merged nowhere — while every OTHER evidence gap in this module
    fails closed. "Containment was not checked" is an evidence gap.
    """
    repo = epic_repo(tmp_path, epic=False)
    path = add_worktree(repo, "X-1", base="main", commit=True, push=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    out = capsys.readouterr().out
    assert "containment is UNCHECKABLE" in out
    assert "--epic <slug> or --target <ref>" in out, "the refusal names its own remedy"
    assert "SKIPPED" in out
    assert path.is_dir(), "pushed-but-merged-nowhere is exactly the case that must survive"
    assert not repo["log"].exists(), "and the gate is never even asked about it"


def test_naming_a_target_lifts_the_uncheckable_containment_refusal(tmp_path, capsys):
    """The refusal above is not a dead end: it is answerable with a flag."""
    repo = epic_repo(tmp_path, epic=False)
    path = add_worktree(repo, "X-2", base="main")

    assert ticket_mod.run("sweep", None, start=repo["root"], target="main", yes=True) == 0
    assert not path.is_dir()


def test_list_shows_the_uncheckable_containment_as_a_blocker(tmp_path, capsys):
    repo = epic_repo(tmp_path, epic=False)
    add_worktree(repo, "X-3", base="main")

    assert ticket_mod.run("list", None, start=repo["root"]) == 0
    out = capsys.readouterr().out
    assert "every worktree is REFUSED" in out
    assert "BLOCKED containment is UNCHECKABLE" in out
    assert "0 with no blocker" in out


def test_a_repo_with_no_remote_reports_rather_than_enforces_pushed(tmp_path, capsys):
    """"Unpushed" is a question with no meaning where there is no remote.

    And on a BRANCH the ref outlives the worktree either way, so
    enforcing it there would refuse every sweep in a local-only repo for
    nothing. `--target` is passed because containment is a separate gate
    and an uncheckable one now refuses on its own.
    """
    root = make_repo(tmp_path / "local-only")
    (root / ".gitignore").write_text(".skill-manager/\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "ignore the home", cwd=root)
    home = make_home(root, units={})
    gate_cli(home)
    path = root.parent / "wt-L-1"
    git("worktree", "add", "-q", str(path), "-b", "feature/L-1", "main", cwd=root)
    (path / ".skill-manager" / "installed").mkdir(parents=True)

    assert ticket_mod.run("sweep", None, start=root, target="main", yes=True) == 0
    out = capsys.readouterr().out
    assert "has no remote" in out
    assert "its branch ref outlives the worktree" in out
    assert not path.is_dir()


def test_target_can_be_named_explicitly(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-26", commit=True, push=True)

    assert ticket_mod.run(
        "sweep", None, start=repo["root"], target=f"feature/T-26", yes=True
    ) == 0
    assert not path.is_dir(), "contained in the ref it was told to check"


# ------------------------------------------------------------------- outside


def test_outside_a_repository_refuses(tmp_path, capsys):
    assert ticket_mod.run("sweep", None, start=tmp_path, yes=True) == 1
    assert "not inside a git repository" in capsys.readouterr().out


def test_human_bytes_signs_a_delta():
    assert sweep_mod.human_bytes(None) == "unmeasured"
    assert sweep_mod.human_bytes(0) == "0 B"
    assert sweep_mod.human_bytes(-1536) == "-1.5 KiB"
    assert sweep_mod.human_bytes(33_700_000).endswith("MiB")


def test_probe_failure_blocks_rather_than_reading_as_clean(tmp_path):
    """An evidence gap is not a clean bill of health."""
    status = sweep_mod.Status(unmeasured=("the stash",))
    assert not status.clean
    assert "could not determine the stash" in status.blockers[0]


def test_the_cli_exposes_both_verbs(tmp_path):
    cli = Path(__file__).resolve().parents[1] / "src" / "skt" / "cli.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "ticket", "--help"], capture_output=True, text=True,
        env={**os.environ, "SKT_ROOT_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0
    assert "list" in proc.stdout and "sweep" in proc.stdout
    assert "--epic" in proc.stdout and "--yes" in proc.stdout and "--into" in proc.stdout


# ------------------------------------------------- evidence gaps fail closed


def test_a_failed_remote_probe_blocks_instead_of_disabling_the_unpushed_gate(
    tmp_path, capsys, monkeypatch
):
    """`""` and None are different answers; `bool()` collapsed them.

    `remotes = bool(_out("remote", ...))` read a FAILED probe as "this
    repository has no remote", which is the one value that switches the
    unpushed-commits gate off. It was the only place in this module where
    an evidence gap did not fail closed — the ARTI-28 defect class (an
    "absent and unreadable are the same false" probe), found the same day
    in `ArtifactPrune`, `ChildHomeRegistry` and `ServersDown`.
    """
    repo = epic_repo(tmp_path)
    # Contained in the ref it is checked against, so `unpushed` is the
    # only thing left that can block it.
    path = add_worktree(repo, "R-1", commit=True)
    real_out = sweep_mod._out

    def out_but_remote_probe_fails(*args, **kwargs):
        if args == ("remote",):
            return None  # the probe did not run — NOT "there are none"
        return real_out(*args, **kwargs)

    monkeypatch.setattr(sweep_mod, "_out", out_but_remote_probe_fails)

    assert ticket_mod.run(
        "sweep", None, start=repo["root"], target="feature/R-1", yes=True
    ) == 4
    out = capsys.readouterr().out
    assert "could not determine whether this repository has any remote" in out
    assert path.is_dir(), "an unreadable probe must never read as `nothing to worry about`"


def test_a_detached_head_with_local_commits_is_refused_even_with_no_remote(
    tmp_path, capsys
):
    """The `remotes=False` note promised a branch ref a detached HEAD has not.

    On a branch, `unpushed` is a workflow gate: the ref outlives the
    worktree. Detached, nothing references the tip once the directory is
    gone but the reflog, so the same fact is data loss — and it must hold
    where `remotes` is False, which is precisely where the old warning
    said the opposite out loud.
    """
    root = make_repo(tmp_path / "local-only-detached")
    (root / ".gitignore").write_text(".skill-manager/\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "ignore the home", cwd=root)
    home = make_home(root, units={})
    gate_cli(home)
    path = root.parent / "wt-detached"
    git("worktree", "add", "-q", "--detach", str(path), "main", cwd=root)
    (path / ".skill-manager" / "installed").mkdir(parents=True)
    (path / "work.txt").write_text("only here\n")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "detached work", cwd=path)

    assert ticket_mod.run("sweep", None, start=root, target="main", yes=True) == 4
    out = capsys.readouterr().out
    assert "DETACHED HEAD" in out
    assert "no ref will outlive this worktree" in out
    assert "its branch ref outlives the worktree" not in out, \
        "the no-remote note must not promise a ref that does not exist"
    assert path.is_dir()


def test_the_no_remote_note_tells_a_detached_worktree_the_truth(tmp_path):
    """The warning itself, without the blocker in the way."""
    branchy = sweep_mod.Status(remotes=False, target="main", detached=False)
    detached = sweep_mod.Status(remotes=False, target="main", detached=True)
    assert "its branch ref outlives the worktree" in branchy.warnings[0]
    assert "no ref outlives it" in detached.warnings[0]


# -------------------------------------------------------- the removal window


def test_a_commit_made_while_the_gate_ran_is_caught_before_removal(tmp_path, capsys):
    """`inspect` → gate → `inspect` → remove, and the LAST one decides.

    The gate is allowed `GATE_TIMEOUT_SECONDS` (180) and is the slow step
    by an order of magnitude, so a git answer taken before it can be
    three minutes old at removal time. `git worktree remove` refuses a
    dirty tree on its own, so what actually slipped through the old
    `inspect → gate → remove` order was a new COMMIT — which leaves the
    tree clean and the commit unpushed. This gate CLI makes one while it
    "runs".
    """
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-27")
    cli = repo["home"] / "bin" / "cli" / "skill-manager"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *--help*) echo "      --into=<into>   The project home"; exit 0;; esac\n'
        f'printf "mid-gate\\n" > "{path}/mid.txt"\n'
        f'git -C "{path}" add -A\n'
        f'git -C "{path}" -c user.email=t@t -c user.name=t commit -q -m "landed mid-gate"\n'
        f"cat <<'VERDICT'\n{CLEAN_VERDICT}\nVERDICT\n"
    )
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

    real_inspect = sweep_mod.inspect
    calls: list[str] = []

    def counting(wt, *, root, target):
        calls.append(str(wt.path))
        return real_inspect(wt, root=root, target=target)

    sweep_mod.inspect = counting
    try:
        assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    finally:
        sweep_mod.inspect = real_inspect

    out = capsys.readouterr().out
    assert len(calls) == 3, f"plan, pre-filter, and AFTER the gate: {calls}"
    assert "SKIPPED" in out
    assert "not pushed to" in out or "not contained in" in out
    assert path.is_dir(), "the commit that landed during the gate keeps its worktree"


def test_a_blocked_worktree_still_does_not_pay_for_the_gate(tmp_path, capsys):
    """The pre-filter earns its place: a two-home compare per worktree."""
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-28", dirty=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    assert not repo["log"].exists()


# ------------------------------------------------------- what git cannot see


def test_gitignored_paths_other_than_the_home_are_reported_before_deletion(
    tmp_path, capsys
):
    """`.skill-manager` is not the only invisible thing a removal deletes.

    `git status --porcelain` does not report ignored paths and
    `git worktree remove` deletes them without `--force`, so `.env`,
    `.venv` and local scratch go the same way as the home did — with no
    gate and, until now, no mention. A WARNING, not a blocker: most
    ignored content is disposable and a gate that always fires gets
    turned off.
    """
    repo = epic_repo(tmp_path)
    (repo["root"] / ".gitignore").write_text(".skill-manager/\n.env\nscratch/\n")
    git("add", "-A", cwd=repo["root"])
    git("commit", "-q", "-m", "ignore more", cwd=repo["root"])
    # onto the epic branch too, so the worktree below both inherits the
    # rules and stays contained in its target
    git("branch", "-f", "epic/demo", "main", cwd=repo["root"])
    git("push", "-q", "-f", "origin", "epic/demo", cwd=repo["root"])
    path = add_worktree(repo, "T-29")
    (path / ".env").write_text("TOKEN=hunter2\n")
    (path / "scratch").mkdir()
    (path / "scratch" / "notes.md").write_text("local\n")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    out = capsys.readouterr().out
    assert "gitignored path(s) will be DELETED" in out
    assert ".env" in out
    assert "scratch/" in out
    assert ".skill-manager" not in out.split("gitignored path(s)")[1].split("\n")[0], \
        "the home has its own gate and is not double-counted here"
    assert not path.is_dir(), "reported, not refused"


def test_the_ignored_listing_is_a_warning_even_when_it_cannot_be_read(tmp_path):
    status = sweep_mod.Status(target="main", ignored_measured=False)
    assert status.clean, "an unreadable ignore listing is not a refusal"
    assert "could not list gitignored paths" in status.warnings[0]


# --------------------------------------------------------- the --into guard


def test_into_may_not_name_a_worktrees_own_home(tmp_path, capsys):
    """The self-comparison trap, re-entered through the front door.

    The default destination is derived from the PRIMARY checkout for
    exactly one reason: a close-out gate whose `--home` and `--into` are
    the same directory compares a home against itself and reports clean
    for everything. `--into` could name that directory by hand.
    """
    repo = epic_repo(tmp_path)
    victim = add_worktree(repo, "T-30")

    assert ticket_mod.run(
        "sweep", None, start=repo["root"], into=str(victim / ".skill-manager"), yes=True
    ) == 1
    out = capsys.readouterr().out
    assert "belongs to the worktree" in out
    assert "compares it against itself" in out
    assert str(repo["home"]) in out, "and it names the destination that is correct"
    assert victim.is_dir()
    assert not repo["log"].exists(), "refused before the gate, so before any removal"


@pytest.mark.parametrize("suffix", [(), ("nested", "home"), (".skill-manager", "x")])
def test_into_may_not_name_anything_inside_a_sweepable_worktree(tmp_path, capsys, suffix):
    """Including the worktree directory itself — which is also the path
    `git worktree remove` takes, i.e. the `not_a_home` trap's twin."""
    repo = epic_repo(tmp_path)
    victim = add_worktree(repo, "T-31")

    assert ticket_mod.run(
        "sweep", None, start=repo["root"], into=str(victim.joinpath(*suffix)), yes=True
    ) == 1
    assert "belongs to the worktree" in capsys.readouterr().out
    assert victim.is_dir()


# ------------------------------------------------------------- the exit code


def test_a_refused_pass_exits_4_and_a_complete_one_exits_0(tmp_path, capsys):
    """`0 removed, N skipped` at exit 0 reads exactly like "nothing to do".

    `skt ticket close` already returns 4 when this same gate refuses this
    same teardown; the fleet verb returning 0 for the identical event is
    the divergence. A skip is not a FAILURE (1) — it is an outstanding
    action with a printed remedy, which is what this repository's `check`
    and `publish --check` spend a distinct non-zero code on.
    """
    repo = epic_repo(tmp_path)
    blocked = add_worktree(repo, "T-32", dirty=True)
    fine = add_worktree(repo, "T-33")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    out = capsys.readouterr().out
    assert "1 removed, 1 skipped for safety" in out
    assert f"exits {sweep_mod.EXIT_SKIPPED_FOR_SAFETY}" in out
    assert "skt ticket close" in out, "the divergence it is being aligned with"
    assert blocked.is_dir() and not fine.is_dir()

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 4
    capsys.readouterr()
    shutil.rmtree(blocked)
    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0, \
        "nothing left to refuse"


def test_a_dry_run_stays_0_even_with_blocked_worktrees(tmp_path, capsys):
    """A plan makes no claim to have acted, and says so twice in its output."""
    repo = epic_repo(tmp_path)
    add_worktree(repo, "T-34", dirty=True)

    assert ticket_mod.run("sweep", None, start=repo["root"]) == 0
    out = capsys.readouterr().out
    assert "(dry run)" in out and "NOTHING was removed" in out


def test_a_failure_outranks_a_skip_in_the_exit_code(tmp_path, capsys):
    """1 means something BROKE, and that has to survive a skip beside it."""
    repo = epic_repo(tmp_path, verdict=NOT_A_HOME_VERDICT, exit_code=2)
    add_worktree(repo, "T-35")
    add_worktree(repo, "T-36", dirty=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 1
    out = capsys.readouterr().out
    assert "1 failed" in out and "1 skipped for safety" in out


# ------------------------------------------- the gate, against a real binary


def _real_skill_manager():
    """A REAL `skill-manager`, or a loud skip. (path, version).

    Everything above stubs the gate with a bash script echoing a canned
    verdict, which proves the WIRING and not that a real
    `home close-out` produces that payload for a home holding an
    unpublished edit. That is the central safety claim of this command,
    so it needs at least one test that asks the actual program.

    The awkward part, stated rather than hidden: the binary this was
    written against is 0.23.0, which predates the `home close-out` fix in
    0.24.0. So the homes below are built to be UNAMBIGUOUS on both
    builds — a unit whose content differs between the worktree home and
    the destination is work that exists in only one place, which
    `HomeCloseOut` blocks on by construction ("a unit the project home
    does not have, or has an older copy of", and "a unit both sides
    changed that a three-way merge *can* fold together — can, not has").
    Measured on 0.23.0: `safe:false`, exit 1, one `skill:alpha` blocker.
    Measured on the same binary for byte-identical homes: `safe:true`,
    exit 0, `status:"unchanged"`. Neither answer depends on the fix.

    A silently-skipped safety test is worse than none, so the skip
    reason is PRINTED as well as recorded — CI has no skill-manager and
    will skip both of these every run.
    """
    found = _REAL_WHICH("skill-manager")
    if found is None:
        return _skip_real_gate(
            "no `skill-manager` on PATH, so the home gate was exercised only "
            "against the bash stub"
        )
    cli = Path(found)
    version = subprocess.run(
        [str(cli), "--version"], capture_output=True, text=True
    ).stdout.splitlines()
    version = version[0].strip() if version else "unknown"
    if not sweep_mod.cli_has_close_out(cli):
        return _skip_real_gate(
            f"{cli} ({version}) has no `home close-out --into`, so it predates the "
            "gate entirely"
        )
    return cli, version


def _skip_real_gate(reason: str):
    message = f"(skipped: {reason})"
    print(message)
    pytest.skip(message)


def _real_home(home: Path, skill_body: str) -> Path:
    """A home a real skill-manager will read: policy, runtime, one unit."""
    (home / "installed").mkdir(parents=True, exist_ok=True)
    (home / "home.runtime.json").write_text("{}")
    (home / "home.policy.toml").write_text('policy = "live"\n')
    (home / "skills" / "alpha").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "alpha" / "SKILL.md").write_text(skill_body)
    (home / "installed" / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "unitKind": "SKILL",
                "origin": "https://github.com/x/alpha",
                "gitHash": "a" * 40,
                "gitRef": "main",
            }
        )
    )
    return home


PUBLISHED = "---\nname: alpha\n---\nthe published body\n"
EDITED = "---\nname: alpha\n---\nthe published body\nAN EDIT THAT REACHED NO REPOSITORY\n"


def _real_gate_repo(tmp_path, cli: Path, worktree_body: str) -> tuple[dict, Path]:
    repo = epic_repo(tmp_path)
    pin = repo["home"] / "bin" / "cli" / "skill-manager"
    pin.unlink()
    pin.symlink_to(cli)
    _real_home(repo["home"], PUBLISHED)
    path = add_worktree(repo, "REAL-1")
    _real_home(path / ".skill-manager", worktree_body)
    return repo, path


def test_a_real_home_close_out_refuses_a_worktree_holding_an_unpublished_edit(
    tmp_path, capsys
):
    """The claim the whole command rests on, asked of the shipped program.

    Not a stub echoing `BLOCKED_VERDICT`: a real
    `skill-manager home close-out --home <worktree>/.skill-manager --into
    <primary>/.skill-manager --json`, over a real home whose copy of a
    skill differs from the destination's. See `_real_skill_manager` for
    why that construction is unambiguous on 0.23.0 and 0.24.0 alike.
    """
    cli, version = _real_skill_manager()
    repo, path = _real_gate_repo(tmp_path, cli, EDITED)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True, as_json=True) == 4
    payload = json.loads(capsys.readouterr().out)
    step = next(s for s in payload["steps"] if s["action"] != "excluded")
    assert step["action"] == "skipped", f"{version} verdict: {step}"
    assert "the home gate refused" in step["reasons"][0]
    assert any("alpha" in reason for reason in step["reasons"]), \
        f"the real CLI names the unit it is refusing over; got {step['reasons']}"
    assert path.is_dir(), "an unpublished skill edit keeps its worktree, for real"


def test_a_real_home_close_out_lets_a_worktree_with_nothing_to_lose_go(
    tmp_path, capsys
):
    """The other half, or the test above proves only that it refuses always.

    A gate that refuses everything — which is exactly what the 0.23.0
    `home close-out` bug did to real cloned homes — passes the refusal
    test for the wrong reason. So the same real binary is asked about a
    worktree home byte-identical to the destination, and has to say yes.
    """
    cli, version = _real_skill_manager()
    repo, path = _real_gate_repo(tmp_path, cli, PUBLISHED)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0, version
    assert "1 removed" in capsys.readouterr().out
    assert not path.is_dir()
