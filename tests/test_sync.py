import json
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
