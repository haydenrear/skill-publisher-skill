import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import status  # noqa: E402
from skt.homes import find_home, read_policy, read_units  # noqa: E402


def make_home(
    root: Path,
    units: dict[str, dict] | None = None,
    policy: str = "live",
    plugins: list[str] = (),
    drift: bool = False,
) -> Path:
    home = root / ".skill-manager"
    (home / "installed").mkdir(parents=True)
    (home / "home.runtime.json").write_text("{}")
    (home / "home.policy.toml").write_text(f'policy = "{policy}"\n')
    for name, extra in (units or {}).items():
        record = {
            "name": name,
            "version": "1.0.0",
            "unitKind": "SKILL",
            "origin": f"https://github.com/x/{name}",
            "gitHash": "a" * 40,
            "gitRef": "main",
            **extra,
        }
        (home / "installed" / f"{name}.json").write_text(json.dumps(record))
        if extra.get("_loaded", True):
            (home / "installed" / f"{name}.projections.json").write_text("{}")
    for plugin in plugins:
        (home / "plugins" / plugin).mkdir(parents=True)
    if drift:
        (home / "home.drift.json").write_text("{}")
    return home


def make_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    return path


@pytest.fixture(autouse=True)
def isolate_root_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-operator-root" / ".skill-manager"))


def test_project_home_report(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}, "beta": {"origin": None, "gitHash": None}})
    report = status.collect(repo)
    assert report["tier"] == "project"
    assert report["checkout"]["kind"] == "standalone"
    by_name = {u["name"]: u for u in report["units"]}
    assert by_name["alpha"]["change_managed"] is True
    assert by_name["beta"]["change_managed"] is False


def test_ticket_worktree_tier_and_context(tmp_path):
    repo = make_repo(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature/T-9",
         str(tmp_path / "repo-T-9")],
        check=True,
    )
    wt = tmp_path / "repo-T-9"
    make_home(wt, units={"alpha": {}})
    report = status.collect(wt)
    assert report["tier"] == "worktree"
    assert report["checkout"]["ticket"] == "T-9"


def test_root_home_tier(tmp_path, monkeypatch):
    fake_root = tmp_path / "fake-operator-root"
    repo = make_repo(fake_root / "somewhere")
    home = make_home(fake_root, units={"alpha": {}})
    assert home == fake_root / ".skill-manager"
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))
    report = status.collect(repo)
    assert report["tier"] == "root"


def test_frozen_policy_and_drift(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}}, policy="frozen", drift=True)
    report = status.collect(repo)
    assert report["policy"] == "frozen"
    assert report["drift_pending"] is True
    assert "DRIFT PENDING" in status.render_text(report)


def test_integration_and_constituent_kinds(tmp_path):
    outer = make_repo(tmp_path / "integ")
    (outer / "integration.toml").write_text("[integration]\n")
    make_home(outer, units={"alpha": {}})
    assert status.collect(outer)["checkout"]["kind"] == "integration"
    leaf = make_repo(outer / "constituents" / "leaf")
    make_home(leaf, units={"alpha": {}})
    assert status.collect(leaf)["checkout"]["kind"] == "constituent"


def test_epic_branch_detection(tmp_path):
    repo = make_repo(tmp_path / "repo", branch="epic/skt")
    make_home(repo, units={"alpha": {}})
    report = status.collect(repo)
    assert report["checkout"]["epic"] == "skt"


def test_spec_workflow_detection(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})
    plan_dir = repo / "specs" / "desired_program_model"
    plan_dir.mkdir(parents=True)
    (plan_dir / "ticket_plan.yaml").write_text(
        "name: my-workflow\ntickets:\n  - id: T-1\n    status: open\n  - id: T-2\n    status: closed\n"
    )
    report = status.collect(repo)
    assert report["spec_workflow"]["name"] == "my-workflow"
    assert report["spec_workflow"]["open_tickets"] == ["T-1"]


def test_no_home_is_reported_not_raised(tmp_path):
    repo = make_repo(tmp_path / "repo")
    report = status.collect(repo)
    assert report["home"] is None
    assert "no skill-manager home" in status.render_text(report)


def test_text_render_is_bounded(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={f"unit{i:02d}": {} for i in range(30)})
    text = status.render_text(status.collect(repo))
    assert len(text.splitlines()) <= 30
    assert "+15 more" in text


def test_json_schema_versioned(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})
    report = status.collect(repo)
    # The number tracks the constant rather than a literal, so a bump is
    # one edit in the module the constant describes — but the report must
    # CARRY it, which is what this asserts.
    assert report["schema"] == status.SCHEMA_VERSION
    assert isinstance(report["schema"], int)


def test_cli_status_runs_as_script(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}}, plugins=["skt"])
    cli = Path(__file__).resolve().parents[1] / "src" / "skt" / "cli.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "status", "--json"],
        capture_output=True,
        text=True,
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "SKT_ROOT_HOME": str(tmp_path / "nowhere")},
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["plugins"] == ["skt"]


def test_cli_tools_reported_from_cli_lock(tmp_path):
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo, units={"alpha": {}})
    (home / "cli-lock.toml").write_text(
        '["brew"."gh"]\nspec = "brew:gh"\n\n["skill-script"."tla-spec-dev"]\nspec = "skill-script:tla-spec-dev"\n'
    )
    report = status.collect(repo)
    assert report["cli_tools"] == ["gh", "tla-spec-dev"]
    assert "tla-spec-dev" in status.render_text(report)


def test_spec_ticket_plan_match_flag(tmp_path):
    import subprocess as sp

    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})
    plan_dir = repo / "specs" / "desired_program_model"
    plan_dir.mkdir(parents=True)
    (plan_dir / "ticket_plan.yaml").write_text(
        "name: wf\ntickets:\n  - id: T-1\n    status: closed\n  - id: T-2\n    status: open\n"
    )
    sp.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature/T-1"], check=True)
    report = status.collect(repo)
    assert report["spec_workflow"]["ticket_in_plan"] is True
    assert "IS in the plan" in status.render_text(report)
    sp.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature/GHOST"], check=True)
    report = status.collect(repo)
    assert report["spec_workflow"]["ticket_in_plan"] is False


def _commit(repo, msg="c"):
    import subprocess as sp

    (repo / "f.txt").write_text(msg)
    sp.run(["git", "-C", str(repo), "add", "-A"], check=True)
    sp.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", msg],
        check=True,
    )


def test_worktree_base_in_sync_fresh(tmp_path):
    import subprocess as sp

    repo = make_repo(tmp_path / "repo")
    sp.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature/T-1",
            str(tmp_path / "repo-T-1")], check=True)
    wt = tmp_path / "repo-T-1"
    make_home(wt, units={})
    report = status.collect(wt)
    sync = report["worktree_sync"]
    assert sync["in_sync"] is True and sync["ahead"] == 0 and sync["behind"] == 0
    assert "in sync with parent" in status.render_text(report)


def test_worktree_ahead_still_in_sync(tmp_path):
    import subprocess as sp

    repo = make_repo(tmp_path / "repo")
    sp.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature/T-2",
            str(tmp_path / "repo-T-2")], check=True)
    wt = tmp_path / "repo-T-2"
    make_home(wt, units={})
    _commit(wt, "work")
    sync = status.collect(wt)["worktree_sync"]
    assert sync["in_sync"] is True and sync["ahead"] == 1


def test_worktree_base_stale_when_parent_moves(tmp_path):
    import subprocess as sp

    repo = make_repo(tmp_path / "repo")
    sp.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature/T-3",
            str(tmp_path / "repo-T-3")], check=True)
    wt = tmp_path / "repo-T-3"
    make_home(wt, units={})
    _commit(repo, "parent moved")
    report = status.collect(wt)
    sync = report["worktree_sync"]
    assert sync["in_sync"] is False and sync["behind"] == 1
    assert "BASE STALE" in status.render_text(report)


def test_main_checkout_has_no_sync_block(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={})
    assert status.collect(repo)["worktree_sync"] is None


# --- the promotion block -----------------------------------------------------
#
# Added after skill-manager's disclosure-cost eval (2026-08-25) measured what a
# fresh agent, inheriting nothing, actually pays to orient itself. Asked "which
# tier am I, what does this home inherit, how does my edit reach the tier above
# and the unit's own repo, what must I never write", agents answered the FIRST
# from `skt status` and hunted for the other three: 10,879 corpus tokens at the
# worktree tier and 21,186 at root, against a 2,000-token budget, reading a
# 13,000-token reference page and -- at root -- skt's own Python source.
#
# `skt publish` already knew the answer. These assertions are that `skt status`
# now says it, and that it says it by ASKING publish rather than by growing a
# second resolver, which is the mistake the whole family of bugs is made of.


def test_worktree_status_names_its_parent_and_the_route_up(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature/T-9",
         str(tmp_path / "repo-T-9")],
        check=True,
    )
    wt = tmp_path / "repo-T-9"
    make_home(wt, units={"alpha": {}})

    report = status.collect(wt)
    assert report["tier"] == "worktree"
    # The parent is the MAIN working tree's home, resolved by publish's own
    # _parent_home -- so status and publish cannot drift apart.
    assert report["promotion"]["parent"] == str(repo / ".skill-manager")
    assert report["promotion"]["error"] is None

    text = status.render_text(report)
    assert str(repo / ".skill-manager") in text, "the parent home is named"
    assert "skt publish" in text, "the route up is named, not just the tier"
    assert "Never write" in text, "the home NOT to write is named"


def test_root_status_says_there_is_nothing_above(tmp_path, monkeypatch):
    fake_root = tmp_path / "fake-operator-root"
    repo = make_repo(fake_root / "somewhere")
    home = make_home(fake_root, units={"alpha": {}})
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))

    report = status.collect(repo)
    assert report["tier"] == "root"
    # (None, None) is the root tier -- nothing above BY DESIGN, which is a
    # different statement from "could not work it out", and the text has to
    # make that difference legible or an agent will go looking for a parent.
    assert report["promotion"]["parent"] is None
    assert report["promotion"]["error"] is None
    text = status.render_text(report)
    assert "none — this IS the root home" in text
    assert "straight to the unit's own git repo" in text


def test_an_unresolvable_parent_is_reported_not_swallowed(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})
    monkeypatch.setattr(
        "skt.publish._parent_home",
        lambda home, start: (None, "operator root home not found at /nope"),
    )
    report = status.collect(repo)
    assert report["promotion"]["parent"] is None
    text = status.render_text(report)
    assert "UNRESOLVED" in text
    assert "will REFUSE" in text, (
        "an agent told the parent is unknown must also be told publish will "
        "refuse -- otherwise the helpful next move is to hand-copy the unit, "
        "which is the damage shape this line exists to prevent"
    )


def test_a_raising_resolver_cannot_break_the_status_line(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})

    def boom(home, start):
        raise RuntimeError("git exploded")

    monkeypatch.setattr("skt.publish._parent_home", boom)
    # `skt status` runs in the SessionStart hook. A promotion line that can
    # raise would take the whole orientation report down with it.
    report = status.collect(repo)
    assert "could not resolve the tier above" in report["promotion"]["error"]
    assert "units" in status.render_text(report)


def test_the_descent_record_answers_when_the_tier_convention_cannot(tmp_path, monkeypatch):
    """A home that has been copied still knows where it came from.

    `_parent_home` derives the tier above from PATH SHAPE. Copy a home, or
    inspect it from a sandbox with a redirected HOME, and that derivation
    misses -- while `home.provenance.json`, which `home clone` WROTE, names the
    source on disk. Measured in skill-manager's disclosure-cost sandbox: status
    printed "parent UNRESOLVED: operator root home not found" directly above a
    record that named the parent.
    """
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo, units={"alpha": {}})
    (home / "home.provenance.json").write_text(
        json.dumps({"clonedFrom": "/somewhere/else/.skill-manager",
                    "parentStores": ["/the/root/.skill-manager"]})
    )
    monkeypatch.setattr(
        "skt.publish._parent_home",
        lambda h, s: (None, "operator root home not found at /nope"),
    )
    report = status.collect(repo)
    assert report["promotion"]["parent"] == "/somewhere/else/.skill-manager"
    assert report["promotion"]["error"] is None
    assert report["promotion"]["from"] == "descent record"
    text = status.render_text(report)
    assert "descent record" in text, "where the answer came from is stated, not implied"
    assert "UNRESOLVED" not in text


def test_no_descent_record_still_reports_the_tier_failure(tmp_path, monkeypatch):
    """The fallback must not swallow a genuine failure into silence."""
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})
    monkeypatch.setattr(
        "skt.publish._parent_home",
        lambda h, s: (None, "operator root home not found at /nope"),
    )
    report = status.collect(repo)
    assert report["promotion"]["parent"] is None
    assert "operator root home not found" in report["promotion"]["error"]
    assert "UNRESOLVED" in status.render_text(report)
