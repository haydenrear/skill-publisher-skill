"""`skt.artifacts` — the typed surface over the artifact graph.

Every test here drives a REAL subprocess through a fake `skill-manager` pin
written into the fixture home. That is deliberate: the things this module
has to get right — running the home's own pin and not a PATH one, stripping
`SKILL_MANAGER_CLI` before it execs, killing the process GROUP on a
deadline, parsing a document that arrives with a non-zero exit — are all
properties of the subprocess boundary, and a mocked `subprocess.run` would
assert none of them.
"""

import ast
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import artifacts as art  # noqa: E402

from test_status import make_home  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_root_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-root" / ".skill-manager"))


def fake_cli(
    home: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    sleep: float = 0.0,
    argv_log: Path | None = None,
    env_log: Path | None = None,
    orphan_pidfile: Path | None = None,
) -> Path:
    """A stand-in for `<home>/bin/cli/skill-manager`.

    `orphan_pidfile` makes the script spawn a grandchild that outlives it,
    which is how the killpg contract is actually observable: a plain
    `Popen.kill()` leaves that grandchild running.
    """
    cli = home / "bin" / "cli" / "skill-manager"
    cli.parent.mkdir(parents=True, exist_ok=True)
    out_file = cli.parent / f".stdout-{abs(hash(stdout)) % 10**8}"
    err_file = cli.parent / f".stderr-{abs(hash(stderr)) % 10**8}"
    out_file.write_text(stdout)
    err_file.write_text(stderr)
    lines = ["#!/bin/sh"]
    if argv_log is not None:
        lines.append(f'printf "%s\\n" "$*" >> "{argv_log}"')
    if env_log is not None:
        lines.append(f'printf "[%s]" "${{SKILL_MANAGER_CLI-<unset>}}" > "{env_log}"')
    if orphan_pidfile is not None:
        lines.append(f'sh -c "sleep 30" & printf "%s" "$!" > "{orphan_pidfile}"')
    if sleep:
        lines.append(f"sleep {sleep}")
    if stdout:
        lines.append(f'cat "{out_file}"')
    if stderr:
        lines.append(f'cat "{err_file}" >&2')
    lines.append(f"exit {exit_code}")
    cli.write_text("\n".join(lines) + "\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return cli


LIST_DOC = {
    "schema": 1,
    "home": "/h/.skill-manager",
    "ledger": {"path": "artifacts.lock.toml", "present": True, "schema": 1,
               "recorded_at": "2026-08-14T00:00:00Z", "artifacts": 2},
    "artifacts": [
        {
            "id": "cli-shim:skill-script/computeq",
            "kind": "cli-shim",
            "owner": "deploy-helm",
            "materialization": "materialized",
            "agreement": "agrees",
            "origin": "recorded",
            "inputs": ["unit:deploy-helm"],
            "observed_inputs": ["store:skills/deploy-helm/skill-scripts"],
            "outputs": [{"path": "bin/cli/computeq", "scope": "home", "presence": "present"}],
            "source": "cli-lock.toml",
            "recorded": {"install_fingerprint": "abc123", "backend": "skill-script"},
            "actual": {"shim_name": "computeq"},
        },
        {
            "id": "unit-store:deploy-helm",
            "kind": "unit-store",
            "owner": "deploy-helm",
            "materialization": "materialized",
            "agreement": "disagrees",
            "origin": "recorded",
            "inputs": [],
            "observed_inputs": [],
            "outputs": [],
            "source": "installed/deploy-helm.json",
            "recorded": {},
            "actual": {},
        },
    ],
    "summary": {"artifacts": 2, "by_kind": {}, "by_materialization": {},
                "by_agreement": {}, "by_origin": {}},
}

STALE_DOC = {
    "schema": 2,
    "home": "/h/.skill-manager",
    "summary": {"artifacts": 5, "stale": 3, "unverifiable": 1, "current": 1,
                "stale_by_kind": {"cli-shim": 2, "unit-store": 1}},
    "stale": [
        {
            "id": "cli-shim:skill-script/computeq",
            "kind": "cli-shim", "owner": "deploy-helm", "freshness": "stale",
            "materialization": "materialized",
            "reason": "its declared inputs moved: recorded a3f21c8f, now 9b17e401 over "
                      "the skill-scripts tree",
            "because": ["unit-store:deploy-helm"],
        },
        {
            "id": "cli-shim:brew/docker",
            "kind": "cli-shim", "owner": "deploy-helm", "freshness": "stale",
            "materialization": "declared-only",
            "reason": "its output bin/cli/docker is not there",
            "because": [],
        },
        {
            "id": "unit-store:deploy-helm",
            "kind": "unit-store", "owner": "deploy-helm", "freshness": "stale",
            "materialization": "materialized",
            "reason": "what is recorded about it does not describe the bytes on disk",
            "because": [],
        },
    ],
    "unverifiable": [
        {
            "id": "cli-shim:npm/gemini-cli",
            "kind": "cli-shim", "owner": "acp", "freshness": "unverifiable",
            "materialization": "unknown",
            "reason": "its lock row records no install fingerprint",
            "because": [],
        },
    ],
}

BUILD_DOC = {
    "schema": 1,
    "home": "/h/.skill-manager",
    "dry_run": False,
    "summary": {"selected": 2, "rebuilt": 1, "no_op": 0, "failed": 1,
                "already_current": 0, "not_buildable": 0, "still_stale": 1},
    "steps": [
        {
            "id": "cli-shim:skill-script/computeq", "kind": "cli-shim",
            "owner": "deploy-helm", "action": "rebuild",
            "producer": "skill-script:computeq (declared by deploy-helm)",
            "reason": "its declared inputs moved",
            "freshness_before": "stale", "materialization_before": "materialized",
            "freshness_after": "current", "materialization_after": "materialized",
            "outcome": "built", "verifiable": True,
        },
        {
            "id": "cli-shim:pip/pytest", "kind": "cli-shim", "owner": "sdc",
            "action": "rebuild", "producer": "pip:pytest (declared by sdc)",
            "reason": "its output is not there",
            "freshness_before": "stale", "materialization_before": "declared-only",
            "freshness_after": "stale", "materialization_after": "declared-only",
            "outcome": "failed", "verifiable": False,
        },
    ],
}


def home_with(tmp_path, **kwargs) -> Path:
    home = make_home(tmp_path / "repo")
    fake_cli(home, **kwargs)
    return home


# ------------------------------------------------------------------- list


def test_list_artifacts_returns_typed_rows(tmp_path):
    home = home_with(tmp_path, stdout=json.dumps(LIST_DOC))
    rows = art.list_artifacts(home=home)
    assert [r.id for r in rows] == ["cli-shim:skill-script/computeq", "unit-store:deploy-helm"]
    shim = rows[0]
    assert isinstance(shim, art.Artifact)
    assert shim.short_name == "computeq"
    assert shim.buildable and shim.present
    assert shim.inputs == ("unit:deploy-helm",)
    assert shim.outputs[0] == art.ArtifactOutput("bin/cli/computeq", "home", "present")
    assert shim.recorded["install_fingerprint"] == "abc123"
    # unit-store is real and reported, and is NOT something `skt build` offers
    # to rebuild — only cli-shim has a per-artifact producer.
    assert rows[1].buildable is False


def test_list_filters_reach_the_cli(tmp_path):
    log = tmp_path / "argv.log"
    home = home_with(tmp_path, stdout=json.dumps(LIST_DOC), argv_log=log)
    art.list_artifacts(home=home, kind="cli-shim", owner="deploy-helm")
    assert log.read_text().strip() == (
        "artifacts list --json --kind cli-shim --owner deploy-helm"
    )


# ------------------------------------------------------------------ stale


def test_stale_survey_separates_the_three_states(tmp_path):
    home = home_with(tmp_path, stdout=json.dumps(STALE_DOC))
    survey = art.stale(home=home)
    assert isinstance(survey, art.StaleSurvey)
    assert (survey.total, survey.current) == (5, 1)
    assert len(survey) == 3
    assert [r.id for r in survey] == [r.id for r in survey.stale]
    # Unverifiable is beside stale and is never folded into current.
    assert [r.id for r in survey.unverifiable] == ["cli-shim:npm/gemini-cli"]
    assert survey.stale_by_kind == {"cli-shim": 2, "unit-store": 1}


def test_rebuildable_is_on_disk_and_actually_buildable(tmp_path):
    home = home_with(tmp_path, stdout=json.dumps(STALE_DOC))
    survey = art.stale(home=home)
    # brew/docker is stale but was never built (a lazy home's normal state);
    # unit-store is stale but `build` has no producer for it. Neither may be
    # offered as "rebuild with: skt build ...".
    assert [r.id for r in survey.rebuildable] == ["cli-shim:skill-script/computeq"]
    assert [r.id for r in survey.not_built] == ["cli-shim:brew/docker"]


def test_stale_row_carries_its_upstream_cause(tmp_path):
    home = home_with(tmp_path, stdout=json.dumps(STALE_DOC))
    row = art.stale(home=home).rebuildable[0]
    assert row.because == ("unit-store:deploy-helm",)
    assert row.short_name == "computeq"


def test_schema_1_stale_document_still_reads(tmp_path):
    """The older document carries no `materialization`; that is `unknown`.

    Not `materialized`: guessing the field that decides whether an artifact
    is on disk would manufacture the presence proxy this epic removes.
    """
    old = {
        "schema": 1,
        "home": "/h",
        "summary": {"artifacts": 1, "stale": 1, "unverifiable": 0, "current": 0},
        "stale": [{"id": "cli-shim:brew/x", "kind": "cli-shim", "owner": "u",
                   "freshness": "stale", "reason": "moved", "because": []}],
        "unverifiable": [],
    }
    home = home_with(tmp_path, stdout=json.dumps(old))
    survey = art.stale(home=home)
    assert survey.stale[0].materialization == "unknown"
    assert survey.stale[0].present is False
    assert survey.rebuildable == ()


def test_future_schema_is_refused_not_half_parsed(tmp_path):
    doc = dict(STALE_DOC, schema=art.STALE_SCHEMA + 1)
    home = home_with(tmp_path, stdout=json.dumps(doc))
    with pytest.raises(art.ArtifactsUnsupported) as err:
        art.stale(home=home)
    assert str(art.STALE_SCHEMA + 1) in err.value.reason
    assert err.value.fix


# ------------------------------------------------------------------ build


def test_build_parses_its_report_even_on_a_failing_exit(tmp_path):
    """`build --json` exits 1 when a rebuild failed AND prints the report.

    Consulting the exit code first would throw away the only description of
    what happened.
    """
    home = home_with(tmp_path, stdout=json.dumps(BUILD_DOC), exit_code=1)
    result = art.build(("cli-shim:skill-script/computeq",), home=home)
    assert isinstance(result, art.BuildResult)
    assert result.exit_code == 1
    assert (result.rebuilt, result.failed, result.still_stale) == (1, 1, 1)
    assert result.ok is False
    assert [s.outcome for s in result] == ["built", "failed"]
    assert result.steps[0].repaired is True
    assert result.steps[1].repaired is False


def test_build_flags_and_ids_reach_the_cli(tmp_path):
    log = tmp_path / "argv.log"
    home = home_with(tmp_path, stdout=json.dumps(BUILD_DOC), argv_log=log)
    art.build(("cli-shim:pip/pytest",), home=home, stale_only=True,
              dry_run=True, force=True, yes=True)
    assert log.read_text().strip() == (
        "build --stale --force --dry-run --yes --json cli-shim:pip/pytest"
    )


def test_a_rebuilt_but_unverifiable_artifact_is_not_a_failure(tmp_path):
    """#120: a backend that records no fingerprint cannot confirm its build.

    `unverifiable` after a successful rebuild is the true answer, and must
    not be reported as a repair that failed.
    """
    doc = {
        "schema": 1, "home": "/h", "dry_run": False,
        "summary": {"selected": 1, "rebuilt": 1, "no_op": 0, "failed": 0,
                    "already_current": 0, "not_buildable": 0, "still_stale": 0},
        "steps": [{"id": "cli-shim:pip/pytest", "kind": "cli-shim", "owner": "u",
                   "action": "rebuild", "producer": "pip:pytest", "reason": "moved",
                   "freshness_before": "stale", "materialization_before": "materialized",
                   "freshness_after": "unverifiable",
                   "materialization_after": "materialized",
                   "outcome": "built", "verifiable": False}],
    }
    home = home_with(tmp_path, stdout=json.dumps(doc))
    result = art.build(home=home)
    assert result.ok is True
    assert result.steps[0].repaired is True
    assert result.steps[0].verifiable is False


# ------------------------------------------------------- typed refusals


def test_a_cli_without_the_artifacts_verb_degrades_honestly(tmp_path):
    """The pin most operator homes carry predates the whole artifact graph."""
    home = home_with(
        tmp_path,
        stderr="Unmatched argument at index 0: 'artifacts'\n"
               "Usage: skill-manager [-hV] [COMMAND]\n",
        exit_code=2,
    )
    with pytest.raises(art.ArtifactsUnsupported) as err:
        art.stale(home=home)
    assert "predates the artifact graph" in err.value.reason
    assert "skt sync skill-manager" in err.value.fix
    # Never an empty result: "nothing is stale" and "I could not ask" are
    # different answers and only one of them is true here.
    assert isinstance(err.value, art.ArtifactError)


def test_not_a_home_is_its_own_refusal(tmp_path):
    home = home_with(
        tmp_path, stderr="error: /tmp/x is not a Skill Manager home\n", exit_code=2
    )
    with pytest.raises(art.HomeNotFound):
        art.list_artifacts(home=home)


def test_missing_cli_pin_is_its_own_refusal(tmp_path):
    home = make_home(tmp_path / "repo")  # no bin/cli/skill-manager
    with pytest.raises(art.CliUnavailable) as err:
        art.stale(home=home)
    assert "no skill-manager CLI pin" in err.value.reason


def test_no_home_at_all(tmp_path, monkeypatch):
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "nowhere"))
    with pytest.raises(art.HomeNotFound):
        art.stale(tmp_path / "empty")


def test_unknown_id_from_the_cli_carries_candidates(tmp_path):
    home = home_with(
        tmp_path,
        stderr="error: no artifact with id cli-shim:brew/xx in /h\n"
               "  did you mean:\n    cli-shim:brew/x\n    cli-shim:brew/xyz\n",
        exit_code=2,
    )
    with pytest.raises(art.UnknownArtifact) as err:
        art.build(("cli-shim:brew/xx",), home=home)
    assert err.value.candidates == ("cli-shim:brew/x", "cli-shim:brew/xyz")
    assert err.value.exit_code == 2


def test_frozen_home_refuses_the_build(tmp_path):
    home = home_with(
        tmp_path, stderr="error: home policy is frozen; build refused\n", exit_code=9
    )
    with pytest.raises(art.BuildRefused) as err:
        art.build(home=home)
    assert err.value.exit_code == 9
    assert "frozen" in err.value.reason


def test_policy_gate_refuses_the_build(tmp_path):
    home = home_with(tmp_path, stderr="install refused by policy.install\n", exit_code=6)
    with pytest.raises(art.BuildRefused) as err:
        art.build(home=home)
    assert err.value.exit_code == 6


def test_an_unclassifiable_failure_is_still_typed(tmp_path):
    home = home_with(tmp_path, stderr="boom\n", exit_code=70)
    with pytest.raises(art.ArtifactError) as err:
        art.stale(home=home)
    assert type(err.value) is art.ArtifactError
    assert err.value.exit_code == 70
    assert "boom" in err.value.detail


# ------------------------------------------------------- process discipline


def test_a_hung_cli_times_out_and_the_whole_group_dies(tmp_path):
    """A plain `Popen.kill()` leaves the CLI's own children running.

    The CLI spawns brew/npm/pip/uv, which inherit the pipes and outlive the
    direct child — the same failure `check._run_git` documents for git's
    helpers. The orphan below is the observable form of that.
    """
    pidfile = tmp_path / "orphan.pid"
    home = home_with(tmp_path, stdout=json.dumps(STALE_DOC), sleep=30,
                     orphan_pidfile=pidfile)
    started = time.monotonic()
    with pytest.raises(art.ProbeTimeout) as err:
        art.stale(home=home, timeout=1.0)
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"the deadline did not bound the call ({elapsed:.1f}s)"
    assert "did not finish" in err.value.reason
    orphan = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(orphan, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the CLI's grandchild {orphan} survived the deadline")


def test_a_spent_budget_spawns_nothing(tmp_path):
    log = tmp_path / "argv.log"
    home = home_with(tmp_path, stdout=json.dumps(STALE_DOC), argv_log=log)
    with pytest.raises(art.ProbeTimeout):
        art.stale(home=home, timeout=0)
    assert not log.exists(), "a spent budget must not spawn the CLI at all"


def test_the_pin_is_run_without_skill_manager_cli_set(tmp_path, monkeypatch):
    """`_cli_env` strips it, or an unguarded pin execs itself forever."""
    seen = tmp_path / "env.log"
    monkeypatch.setenv("SKILL_MANAGER_CLI", "/some/other/pin")
    home = home_with(tmp_path, stdout=json.dumps(STALE_DOC), env_log=seen)
    art.stale(home=home)
    assert seen.read_text() == "[<unset>]"


def test_it_runs_the_home_pin_and_never_a_path_skill_manager(tmp_path, monkeypatch):
    """A PATH `skill-manager` in a ticket worktree belongs to another home."""
    decoy_dir = tmp_path / "decoy-bin"
    decoy_dir.mkdir()
    decoy = decoy_dir / "skill-manager"
    decoy.write_text("#!/bin/sh\nexit 99\n")
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{decoy_dir}:{os.environ['PATH']}")
    home = home_with(tmp_path, stdout=json.dumps(STALE_DOC))
    assert art.stale(home=home).total == 5  # the decoy would have raised


# ------------------------------------------------------------- resolution


def test_a_full_id_resolves_without_asking_the_cli(tmp_path):
    log = tmp_path / "argv.log"
    home = home_with(tmp_path, stdout=json.dumps(LIST_DOC), argv_log=log)
    assert art.resolve_ids(["cli-shim:brew/kubectl"], home=home) == (
        "cli-shim:brew/kubectl",
    )
    assert not log.exists()


def test_a_short_name_resolves_to_its_id(tmp_path):
    home = home_with(tmp_path, stdout=json.dumps(LIST_DOC))
    assert art.resolve_ids(["computeq"], home=home) == (
        "cli-shim:skill-script/computeq",
    )


def test_an_ambiguous_short_name_is_refused_not_guessed(tmp_path):
    doc = json.loads(json.dumps(LIST_DOC))
    twin = dict(doc["artifacts"][0], id="cli-shim:brew/computeq")
    doc["artifacts"].append(twin)
    home = home_with(tmp_path, stdout=json.dumps(doc))
    with pytest.raises(art.UnknownArtifact) as err:
        art.resolve_ids(["computeq"], home=home)
    assert "names 2 artifacts" in err.value.reason
    assert set(err.value.candidates) == {
        "cli-shim:skill-script/computeq", "cli-shim:brew/computeq"
    }


def test_an_unknown_short_name_names_what_the_home_holds(tmp_path):
    home = home_with(tmp_path, stdout=json.dumps(LIST_DOC))
    with pytest.raises(art.UnknownArtifact) as err:
        art.resolve_ids(["nosuch"], home=home)
    assert "no artifact named 'nosuch'" in err.value.reason


# ------------------------------------------------------------- the contract


def test_the_module_imports_nothing_outside_the_standard_library():
    """`src/skt/cli.py`'s constraint, asserted rather than remembered.

    The skill-script installer runs skt with the system python3 and no
    venv, so a third-party import here does not fail in CI — it fails in an
    operator's session, on the hook path.
    """
    source = Path(art.__file__).read_text()
    tree = ast.parse(source)
    external: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            external += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            external.append(node.module.split(".")[0])
    offenders = [m for m in external if m not in sys.stdlib_module_names]
    assert offenders == [], f"non-stdlib imports in skt.artifacts: {offenders}"


def test_the_package_exports_the_surface():
    import skt

    assert "stale" in skt.__all__ and "build" in skt.__all__
    for name in skt.__all__:
        assert hasattr(skt, name), f"__all__ names {name}, which is not exported"
    assert skt.stale is art.stale
    # One root every caller can catch, the way `WtError` roots wt.py's.
    for subclass in (art.HomeNotFound, art.CliUnavailable, art.ArtifactsUnsupported,
                     art.ProbeTimeout, art.UnknownArtifact, art.BuildRefused):
        assert issubclass(subclass, art.ArtifactError)


def test_every_refusal_carries_a_reason_and_a_fix(tmp_path):
    """wt-style: one error line, one fix line. A refusal with no remedy is a
    dead end an agent cannot act on."""
    cases = [
        (dict(stderr="Unmatched argument at index 0: 'artifacts'\n", exit_code=2),
         art.ArtifactsUnsupported),
        (dict(stderr="error: x is not a Skill Manager home\n", exit_code=2),
         art.HomeNotFound),
        (dict(stderr="error: frozen\n", exit_code=9), art.BuildRefused),
    ]
    for kwargs, expected in cases:
        home = home_with(tmp_path / str(id(kwargs)), **kwargs)
        with pytest.raises(expected) as err:
            art.stale(home=home)
        assert err.value.reason and err.value.fix
