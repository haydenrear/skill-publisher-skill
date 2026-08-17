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

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out
    assert "uncommitted path(s)" in out
    assert "0 removed, 1 skipped for safety" in out
    assert dirty.is_dir(), "a dirty worktree survives the sweep"
    assert not repo["log"].exists(), "and the gate is never even asked about it"


def test_unpushed_commits_are_skipped(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-3", commit=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    out = capsys.readouterr().out
    assert "not pushed to" in out
    assert path.is_dir()


def test_commits_not_contained_in_the_epic_branch_are_skipped(tmp_path, capsys):
    """Pushed, so nothing is at risk — but it never landed on the epic."""
    repo = epic_repo(tmp_path)
    path = add_worktree(repo, "T-4", commit=True, push=True)

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
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

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
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
        assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
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

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
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

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
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

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["removed"] == 1
    assert payload["summary"]["skipped"] == 1
    assert payload["summary"]["failed"] == 0
    assert payload["pruned"] is True
    assert payload["free_bytes_delta"] is not None
    assert "du" in payload["size_note"]
    assert payload["exit_code"] == 0


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


def test_no_target_branch_warns_that_containment_was_not_checked(tmp_path, capsys):
    """A repo with no epic branch still sweeps — and says what it did not check."""
    repo = epic_repo(tmp_path, epic=False)
    path = add_worktree(repo, "X-1", base="main")

    assert ticket_mod.run("sweep", None, start=repo["root"], yes=True) == 0
    out = capsys.readouterr().out
    assert "containment NOT checked" in out
    assert "no epic/target branch known" in out
    assert not path.is_dir(), "the gap is reported, not treated as a blocker"


def test_a_repo_with_no_remote_reports_rather_than_enforces_pushed(tmp_path, capsys):
    """"Unpushed" is a question with no meaning where there is no remote.

    And the branch ref outlives the worktree either way, so enforcing it
    there would refuse every sweep in a local-only repo for nothing.
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

    assert ticket_mod.run("sweep", None, start=root, yes=True) == 0
    out = capsys.readouterr().out
    assert "has no remote" in out
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
