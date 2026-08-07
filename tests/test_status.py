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
    assert status.collect(repo)["schema"] == 1


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
