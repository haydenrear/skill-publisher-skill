"""SKT-19: declared-path epic worktrees through skt (issue #87)."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import ticket as ticket_mod  # noqa: E402

from test_status import make_home, make_repo  # noqa: E402

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


@pytest.fixture(autouse=True)
def isolate_root_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-root" / ".skill-manager"))


def fake_bootstrap(home: Path, exit_code: int = 0, says: str = "") -> Path:
    """A home whose git-issue-workflow ships a controllable bootstrap-home.sh.

    `says` seeds the child's exact output, which is the thing HBR-3 is
    about: what the reader is shown when this fails.
    """
    script = home / "skills" / "git-issue-workflow" / "scripts" / "bootstrap-home.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    if says:
        script.write_text(
            "#!/usr/bin/env bash\ncat >&2 <<'SAID'\n" + says + "\nSAID\n"
            f"exit {exit_code}\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script
    if exit_code == 0:
        script.write_text(
            "#!/usr/bin/env bash\n"
            'root=""\nwhile [ $# -gt 0 ]; do case "$1" in --root) root="$2"; shift 2;; *) shift;; esac; done\n'
            'mkdir -p "$root/.skill-manager/bin/launch"\n'
            'touch "$root/.skill-manager/bin/launch/claude"\n'
        )
    else:
        script.write_text(f"#!/usr/bin/env bash\necho boom >&2\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _ignore_home(repo: Path) -> None:
    (repo / ".gitignore").write_text(".skill-manager/\n.claude/\n.codex/\n.gemini/\n")
    subprocess.run([*GIT, "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "-m", "ignore homes"], check=True)


def epic_repo(tmp_path):
    repo = make_repo(tmp_path / "repo")
    _ignore_home(repo)
    subprocess.run([*GIT, "-C", str(repo), "branch", "epic/slug"], check=True)
    home = make_home(repo, units={})
    fake_bootstrap(home)
    return repo


def run_epic_new(repo, ticket, path, base=None, monkeypatch=None):
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        return ticket_mod.epic_new(ticket, base, str(path))
    finally:
        os.chdir(cwd)


def test_declared_path_worktree_with_pinned_base(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    declared = tmp_path / "wt-7-slug"
    assert run_epic_new(repo, "7-slug", declared, base="epic/slug") == 0
    out = capsys.readouterr().out
    assert f"created epic worktree {declared}" in out
    assert declared.is_dir()
    branch = subprocess.run(
        ["git", "-C", str(declared), "branch", "--show-current"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "feature/7-slug"
    refs = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "refs/index-bases"],
        capture_output=True, text=True,
    ).stdout
    assert "index-bases" in refs and refs.strip(), "retention ref created"
    assert (declared / ".skill-manager" / "bin" / "launch" / "claude").exists()


def test_dirty_tree_refused(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    (repo / "dirty.txt").write_text("x")
    assert run_epic_new(repo, "8-slug", tmp_path / "wt-8-slug") == 1
    assert "not clean" in capsys.readouterr().out


#: What `bootstrap-home.sh`'s `die` actually printed in skill-manager#264,
#: reproduced against a real refusing shim on 2026-08-29. The diagnosis is
#: the SECOND line and the last line is a sentence fragment, which is why
#: `tail[-1]` reported "against the operator's global home." and nothing
#: else.
BOOTSTRAP_DIE = """\
log:       /tmp/bootstrap-home-A3Uj8x.log
error: no skill-manager CLI with a `home` subcommand was found.
    on PATH: /Users/x/.skill-manager/bin/cli/skill-manager (too old — `home clone` is missing)
  Set SKILL_MANAGER_CLI to a build that has it, or install a newer skill-manager.
  Without it a worktree cannot get its own home, and an agent would run
  against the operator's global home."""

#: `LauncherShims`' cross-home refusal, as it reaches skt once the probe
#: stops swallowing it (HBR-2) or when skt runs the shim itself.
SHIM_REFUSAL = """\
+ probing /Users/x/.skill-manager/bin/cli/skill-manager
skill-manager: refusing to run against a home you did not name.
  you named:  /repo/wt-13-slug/.skill-manager
  this shim would have edited: /Users/x/.skill-manager
  Say which one you mean:
    --home /Users/x/.skill-manager   (this shim's home)
    --home /repo/wt-13-slug/.skill-manager   (the home your environment names)
error: home clone failed"""


def test_bootstrap_failure_relays_the_cause_not_the_last_line(tmp_path, capsys):
    """HBR-3 / #264 defect 2: the diagnosis reached the reader.

    Baseline was `tail[-1]` — the child's final line — so a five-line
    failure rendered as its own trailing fragment and the operator had to
    re-run the bootstrap by hand to learn anything.
    """
    repo = make_repo(tmp_path / "repo")
    _ignore_home(repo)
    home = make_home(repo, units={})
    fake_bootstrap(home, exit_code=1, says=BOOTSTRAP_DIE)
    assert run_epic_new(repo, "12-slug", tmp_path / "wt-12-slug") == 3
    out = capsys.readouterr().out
    assert "rolled back" in out, "the rollback report is unchanged"
    assert "no skill-manager CLI with a `home` subcommand was found" in out
    # #264 asked for this by name: the script writes a log and says so.
    assert "log:   /tmp/bootstrap-home-A3Uj8x.log" in out


def test_a_seeded_cross_home_refusal_survives_provisioning(tmp_path, capsys):
    """GOAL-the-real-error-survives, at the site that measured its baseline."""
    repo = make_repo(tmp_path / "repo")
    _ignore_home(repo)
    home = make_home(repo, units={})
    fake_bootstrap(home, exit_code=1, says=SHIM_REFUSAL)
    assert run_epic_new(repo, "13-slug", tmp_path / "wt-13-slug") == 3
    out = capsys.readouterr().out
    assert "refusing to run against a home you did not name" in out
    assert "you named:  /repo/wt-13-slug/.skill-manager" in out
    assert "this shim would have edited: /Users/x/.skill-manager" in out
    # And the remedy no longer sends the reader after an upgrade.
    assert "upgrading changes nothing" in out


def test_bootstrap_failure_rolls_back(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    _ignore_home(repo)
    home = make_home(repo, units={})
    fake_bootstrap(home, exit_code=5)
    declared = tmp_path / "wt-9-slug"
    assert run_epic_new(repo, "9-slug", declared) == 3
    out = capsys.readouterr().out
    assert "rolled back" in out
    assert not declared.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "feature/9-slug"],
        capture_output=True, text=True,
    ).stdout
    assert not branches.strip(), "branch deleted on rollback"


def test_retention_ref_conflict_refused(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "epic/slug^{tree}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    bogus = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-m", "other"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref",
         f"refs/index-bases/{repo.name}/{tree}", bogus],
        check=True,
    )
    assert run_epic_new(repo, "10-slug", tmp_path / "wt-10-slug", base="epic/slug") == 1
    assert "retention ref" in capsys.readouterr().out


def test_unknown_base_refused(tmp_path, capsys):
    repo = epic_repo(tmp_path)
    assert run_epic_new(repo, "11-slug", tmp_path / "wt-11", base="epic/ghost") == 1
    assert "cannot resolve base" in capsys.readouterr().out
