import json
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import publish as publish_mod  # noqa: E402
from skt import ticket as ticket_mod  # noqa: E402

from test_check import make_unit_upstream, unit_record  # noqa: E402
from test_status import make_home, make_repo  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_root_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-root" / ".skill-manager"))


# --- fake git_issue_workflow wrapper ---------------------------------------


class FakeWtError(RuntimeError):
    def __init__(self, reason, fix="", log="", exit_code=1):
        super().__init__(reason)
        self.reason, self.fix, self.log, self.exit_code = reason, fix, log, exit_code


class FakeBootstrapFailed(FakeWtError):
    pass


class FakeCloseRefused(FakeWtError):
    pass


def fake_giw(monkeypatch, **behaviors):
    mod = types.ModuleType("git_issue_workflow")
    mod.WtError = FakeWtError
    mod.BootstrapFailed = FakeBootstrapFailed
    mod.CloseRefused = FakeCloseRefused
    contract = types.SimpleNamespace(
        worktree="/tmp/repo-T-1", branch="feature/T-1", close="wt close T-1",
        launch="/tmp/repo-T-1/.skill-manager/bin/launch/claude",
        if_exit_8=None, propagate=None,
    )
    mod.wt_new = behaviors.get("wt_new", lambda t, b=None, **k: contract)
    mod.wt_info = behaviors.get("wt_info", lambda t, **k: contract)
    mod.wt_close = behaviors.get(
        "wt_close",
        lambda t, **k: types.SimpleNamespace(
            worktree="/tmp/repo-T-1", branch="feature/T-1", delete=None,
            home_work="/repo/.skill-manager (one tier only)", dry_run_clean=False,
        ),
    )
    monkeypatch.setitem(sys.modules, "git_issue_workflow", mod)
    return mod


def test_ticket_new_prints_contract_and_home_warning(tmp_path, monkeypatch, capsys):
    fake_giw(monkeypatch)
    assert ticket_mod.run("new", "T-1") == 0
    out = capsys.readouterr().out
    assert "/tmp/repo-T-1" in out
    assert "skt publish" in out  # the home warning


def test_ticket_close_refused_renders_remedy(tmp_path, monkeypatch, capsys):
    def refuse(t, **k):
        raise FakeCloseRefused(
            "the home still holds work (unit: eval-unit)",
            fix="skill-manager home sync --from /w/.skill-manager --to /r/.skill-manager --merge",
        )

    fake_giw(monkeypatch, wt_close=refuse)
    assert ticket_mod.run("close", "T-1") == 4
    out = capsys.readouterr().out
    assert "close refused" in out
    assert "fix:" in out


def test_ticket_close_success_reports_home_work(tmp_path, monkeypatch, capsys):
    fake_giw(monkeypatch)
    assert ticket_mod.run("close", "T-1") == 0
    out = capsys.readouterr().out
    assert "home-work:" in out


def test_ticket_requires_verb_and_id(capsys):
    assert ticket_mod.run(None, None) == 1


# --- publish ----------------------------------------------------------------


def edited_home(tmp_path, monkeypatch=None):
    """A project home whose store unit checkout carries an uncommitted edit."""
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# improved\n")
    return repo, home, bare


def recording_cli(home: Path, fail_on: str | None = None) -> Path:
    calls = home / "cli-calls.log"
    cli = home / "bin" / "cli" / "skill-manager"
    cli.parent.mkdir(parents=True, exist_ok=True)
    body = f'echo "$@" >> "{calls}"\n'
    if fail_on:
        body += f'case "$1 $2" in "{fail_on}") exit 5;; esac\n'
    cli.write_text("#!/usr/bin/env bash\n" + body)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    return calls


def test_publish_check_lists_and_exits_10(tmp_path, capsys):
    repo, home, _ = edited_home(tmp_path)
    assert publish_mod.run(None, check_only=True, start=repo) == 10
    assert "alpha (dirty)" in capsys.readouterr().out


def test_publish_check_clean_home(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={})
    assert publish_mod.run(None, check_only=True, start=repo) == 0


def test_publish_project_tier_syncs_to_root_then_publishes(tmp_path, monkeypatch, capsys):
    repo, home, _ = edited_home(tmp_path)
    fake_root = tmp_path / "fake-root" / ".skill-manager"
    (fake_root / "installed").mkdir(parents=True)
    calls = recording_cli(home)
    assert publish_mod.run("alpha", ticket="T-7", start=repo) == 0
    logged = calls.read_text().splitlines()
    assert logged[0].startswith("home sync --from")
    assert str(fake_root) in logged[0]
    assert logged[1] == "unit publish alpha --ticket T-7"


def test_publish_root_tier_skips_home_sync(tmp_path, monkeypatch, capsys):
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# improved\n")
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))
    calls = recording_cli(home)
    assert publish_mod.run("alpha", ticket="T-7", start=repo) == 0
    logged = calls.read_text().splitlines()
    assert len(logged) == 1 and logged[0].startswith("unit publish alpha")


def test_publish_sync_failure_stops_with_remedy(tmp_path, capsys):
    repo, home, _ = edited_home(tmp_path)
    fake_root = tmp_path / "fake-root" / ".skill-manager"
    (fake_root / "installed").mkdir(parents=True)
    recording_cli(home, fail_on="home sync")
    assert publish_mod.run("alpha", ticket="T-7", start=repo) == 5
    out = capsys.readouterr().out
    assert "error: home sync" in out and "fix:" in out


def test_publish_ambiguous_edits_require_a_name(tmp_path, capsys):
    repo, home, _ = edited_home(tmp_path)
    bare2, tip2 = make_unit_upstream(tmp_path, "beta")
    record = {
        "name": "beta", "version": "1.0.0", "unitKind": "SKILL",
        "origin": str(bare2), "gitHash": tip2, "gitRef": "main",
    }
    (home / "installed" / "beta.json").write_text(json.dumps(record))
    unit_dir = home / "skills" / "beta"
    subprocess.run(["git", "clone", "-q", str(bare2), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# also improved\n")
    assert publish_mod.run(None, start=repo) == 1
    assert "one at a time" in capsys.readouterr().out


def test_publish_infers_ticket_from_branch(tmp_path, capsys):
    repo, home, _ = edited_home(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature/T-42"], check=True)
    fake_root = tmp_path / "fake-root" / ".skill-manager"
    (fake_root / "installed").mkdir(parents=True)
    calls = recording_cli(home)
    assert publish_mod.run("alpha", start=repo) == 0
    assert "--ticket T-42" in calls.read_text()


# --- skill-publisher-skill#15, publish side ---------------------------------
# `edited_units` shares `_local_state`, so the same false `ahead` made
# `skt publish` (no unit named) refuse with "several units are edited" and
# made `skt ticket close` warn about units with nothing to publish.


def test_publish_check_ignores_a_merely_stale_upstream_ref(tmp_path, monkeypatch, capsys):
    import subprocess as sp

    from skt import publish as publish_mod
    from test_check import make_unit_upstream, stale_upstream_store, unit_record
    from test_status import make_home, make_repo

    fake_root = tmp_path / "fake-root"
    make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    unit_dir, new_tip = stale_upstream_store(home, bare, "alpha", tmp_path)
    count = sp.run(["git", "-C", str(unit_dir), "rev-list", "--count", "@{upstream}..HEAD"],
                   capture_output=True, text=True, check=True).stdout.strip()
    assert int(count) > 0

    assert publish_mod.edited_units(home) == []


def test_publish_check_still_sees_real_unpushed_work(tmp_path):
    import subprocess as sp

    from skt import publish as publish_mod
    from test_check import GIT, make_unit_upstream, unit_record
    from test_status import make_home, make_repo

    fake_root = tmp_path / "fake-root"
    make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    sp.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# unpublished\n")
    sp.run([*GIT, "-C", str(unit_dir), "add", "-A"], check=True)
    sp.run([*GIT, "-C", str(unit_dir), "commit", "-q", "-m", "local only"], check=True)

    edited = publish_mod.edited_units(home)
    assert [e["unit"] for e in edited] == ["alpha"]
    assert edited[0]["state"] == "ahead"


def test_edited_units_bounds_its_probes_and_does_not_repeat_them(tmp_path, monkeypatch):
    """F1: one shared deadline, and each local probe run once.

    Without the deadline this walked every still-ahead unit serially at
    `_remote_tip`'s 10s fallback — about a minute on the six falsely-ahead
    units measured in the project home, at the `skt ticket close` teardown
    gate. And re-running `_local_state` to re-decide with a tip repeated
    the `status` and `rev-list` whose answers were already in hand.
    """
    import subprocess as sp

    from skt import check as check_mod
    from skt import publish as publish_mod
    from test_check import make_unit_upstream, stale_upstream_store, unit_record
    from test_status import make_home, make_repo

    fake_root = tmp_path / "fake-root"
    make_repo(fake_root / "anywhere")
    units = {}
    for name in ("alpha", "beta"):
        bare, tip = make_unit_upstream(tmp_path, name)
        units[name] = unit_record(bare, tip)
    home = make_home(fake_root, units=units)
    for name in ("alpha", "beta"):
        stale_upstream_store(home, tmp_path / f"{name}-upstream.git", name, tmp_path)

    deadlines: list = []
    real_tip = check_mod._remote_tip_safe

    def recording_tip(origin, ref, *, deadline=None):
        deadlines.append(deadline)
        return real_tip(origin, ref, deadline=deadline)

    local_calls: list = []
    real_local = check_mod._local_state

    def recording_local(unit_dir, *, deadline=None, remote_tip=None):
        local_calls.append(str(unit_dir))
        return real_local(unit_dir, deadline=deadline, remote_tip=remote_tip)

    monkeypatch.setattr(publish_mod, "_remote_tip_safe", recording_tip)
    monkeypatch.setattr(publish_mod, "_local_state", recording_local)

    assert publish_mod.edited_units(home) == []

    assert len(deadlines) == 2, "one tip resolved per still-ahead unit"
    assert all(d is not None for d in deadlines), "every probe is bounded"
    assert len(set(deadlines)) == 1, "and they SHARE one deadline, not one each"
    assert len(local_calls) == len(set(local_calls)) == 2, \
        f"_local_state runs once per unit, not twice: {local_calls}"
