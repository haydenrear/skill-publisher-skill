import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import sync as sync_mod  # noqa: E402

from test_check import advance_upstream, make_unit_upstream, unit_record  # noqa: E402
from test_status import make_home, make_repo  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_root_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-root" / ".skill-manager"))


def fake_cli(home: Path, body: str) -> None:
    """A stand-in home CLI whose sync behavior the test controls."""
    cli = home / "bin" / "cli" / "skill-manager"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("#!/usr/bin/env bash\n" + body)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)


def test_sync_success_verifies_against_remote_tip(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    new_tip = advance_upstream(bare, tmp_path)
    record = home / "installed" / "alpha.json"
    # the fake CLI "moves the store" by updating the installed record:
    fake_cli(
        home,
        f"python3 -c \"import json;p='{record}';d=json.load(open(p));"
        f"d['gitHash']='{new_tip}';json.dump(d,open(p,'w'))\"\n",
    )
    assert sync_mod.run("alpha", start=repo) == 0
    assert new_tip[:8] in capsys.readouterr().out


def test_sync_detects_silent_noop(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    advance_upstream(bare, tmp_path)
    fake_cli(home, "exit 0\n")  # exits green, moves nothing — the documented trap
    assert sync_mod.run("alpha", start=repo) == 11
    out = capsys.readouterr().out
    assert "silent-no-op trap" in out


def test_sync_unknown_unit(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={})
    assert sync_mod.run("ghost", start=repo) == 1


def test_sync_propagates_cli_failure(tmp_path):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    fake_cli(home, "echo boom >&2; exit 7\n")
    assert sync_mod.run("alpha", start=repo) == 7


# --- skill-publisher-skill#15: the command that fixes staleness made it ------
#
# Measured live during ARTI-00: `skt sync debugging` reported
#   skt sync: debugging now at 91909afc (matches remote tip)
# and the very next `skt check` reported
#   debugging modified locally (ahead) — please sync with root ...
# with rev-list = 2. A bare `git fetch` took it to 0. So it is the sequence,
# not the state, that has to be tested: sync must LEAVE the remote-tracking
# ref correct, not merely arrive at the right commit.


def stale_advancing_cli(store: Path, bare: Path, record: Path, new_tip: str) -> str:
    """A CLI that advances the checkout the way the real one does.

    `git fetch <URL> <ref>` writes FETCH_HEAD and, because the URL is not
    a configured remote, updates no remote-tracking ref at all.
    """
    return (
        "set -e\n"
        f"git -C '{store}' fetch --no-tags --quiet '{bare}' main\n"
        f"git -C '{store}' reset --hard --quiet FETCH_HEAD\n"
        f"python3 -c \"import json;p='{record}';d=json.load(open(p));"
        f"d['gitHash']='{new_tip}';json.dump(d,open(p,'w'))\"\n"
    )


def rev_list_ahead(store: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(store), "rev-list", "--count", "@{upstream}..HEAD"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def test_sync_then_check_does_not_claim_the_unit_is_ahead(tmp_path, monkeypatch, capsys):
    from skt import check as check_mod

    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    store = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(store)], check=True)
    new_tip = advance_upstream(bare, tmp_path)
    fake_cli(home, stale_advancing_cli(store, bare, home / "installed" / "alpha.json", new_tip))
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))

    assert sync_mod.run("alpha", start=repo) == 0
    assert f"now at {new_tip[:8]} (matches remote tip)" in capsys.readouterr().out

    # the sync half: the ref it was supposed to leave correct
    assert rev_list_ahead(store) == 0

    # and the check that immediately follows says nothing
    report = check_mod.collect(repo)
    assert all(n["kind"] != "sync-with-root" for n in report["notifications"]), report
    assert report["notifications"] == [], report

# --- skill-publisher-skill#14: the root tier has no pinned CLI ---------------
#
# `<home>/bin/cli/skill-manager` is written by `skill-manager home shims`. An
# operator root installed from brew that never ran it has no pin, so the hard
# refusal made `skt sync` unavailable at the one tier that publishes globally.
# The pin still wins where it exists, and the fallback is ROOT-only: below
# root, falling through to another CLI is the failure the pin exists to remove.


def prepend_path(monkeypatch, bindir: Path) -> None:
    """Put `bindir` FIRST on PATH, keeping the real one (git/bash/python3)."""
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def path_cli(bindir: Path, body: str) -> Path:
    """A `skill-manager` on PATH — what a brew install leaves behind."""
    bindir.mkdir(parents=True, exist_ok=True)
    cli = bindir / "skill-manager"
    cli.write_text("#!/usr/bin/env bash\n" + body)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    return cli


def moves_store(record: Path, to_hash: str) -> str:
    """A CLI body that does what a real sync does: advance the installed record."""
    return (
        f"python3 -c \"import json;p='{record}';d=json.load(open(p));"
        f"d['gitHash']='{to_hash}';json.dump(d,open(p,'w'))\"\n"
    )


def test_root_tier_falls_back_to_path_cli_and_says_so(tmp_path, monkeypatch, capsys):
    """Before #14 this printed `home CLI not found` and exited 1."""
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    new_tip = advance_upstream(bare, tmp_path)
    cli = path_cli(tmp_path / "brew-bin", moves_store(home / "installed" / "alpha.json", new_tip))
    prepend_path(monkeypatch, cli.parent)
    assert not (home / "bin" / "cli" / "skill-manager").exists()

    assert sync_mod.run("alpha", start=repo) == 0
    out = capsys.readouterr().out
    assert f"skt sync: using PATH skill-manager at {cli}" in out
    assert "root tier; this home has no pinned CLI" in out
    assert new_tip[:8] in out


def test_home_pin_wins_over_path_and_is_named(tmp_path, monkeypatch, capsys):
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    new_tip = advance_upstream(bare, tmp_path)
    fake_cli(home, moves_store(home / "installed" / "alpha.json", new_tip))
    # a PATH CLI that fails loudly if it is ever the one chosen
    other = path_cli(tmp_path / "brew-bin", "echo WRONG-CLI >&2; exit 9\n")
    prepend_path(monkeypatch, other.parent)

    assert sync_mod.run("alpha", start=repo) == 0
    pin = home / "bin" / "cli" / "skill-manager"
    assert f"skt sync: using this home's pinned CLI at {pin}" in capsys.readouterr().out


def test_non_root_tier_still_refuses_without_a_pin(tmp_path, monkeypatch, capsys):
    """The fallback is ROOT-only: a project home must not use a stray CLI."""
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    cli = path_cli(tmp_path / "brew-bin", "exit 0\n")
    prepend_path(monkeypatch, cli.parent)

    assert sync_mod.run("alpha", start=repo) == 1
    out = capsys.readouterr().out
    assert str(home / "bin" / "cli" / "skill-manager") in out
    assert "project-tier home has no pinned CLI" in out


def test_root_tier_refuses_another_homes_pin_on_path(tmp_path, monkeypatch, capsys):
    """A foreign pin derives ITS home from its own location and would write it."""
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    other_home = tmp_path / "some-project" / ".skill-manager"
    foreign = path_cli(other_home / "bin" / "cli", "exit 0\n")
    prepend_path(monkeypatch, foreign.parent)

    assert sync_mod.run("alpha", start=repo) == 1
    out = capsys.readouterr().out
    assert "is another home's pin" in out
    assert str(other_home) in out


def test_root_tier_with_no_cli_anywhere_names_both_remedies(tmp_path, monkeypatch, capsys):
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    # a PATH with git/bash/python3 but deliberately no skill-manager
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert sync_mod.run("alpha", start=repo) == 1
    out = capsys.readouterr().out
    assert "no `skill-manager` on PATH" in out
    assert "home shims" in out
