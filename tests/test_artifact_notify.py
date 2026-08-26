"""ARTI-10: `skt build`, the stale-artifact notification, and the cache line.

The contract under most pressure here is the one that predates this ticket:
`skt check --cached` is contract-cache-only in EVERY cache state, because
PostToolUse gets 2 seconds on every tool call in every session. An artifact
probe that shells out on that path is exactly the regression that timeout
exists to catch — so the poisoning below is deliberately broader than
`subprocess`: `os.spawn*`, `os.fork`, `os.posix_spawn` and
`ThreadPoolExecutor` are all replaced after import, and all five cache
states are driven through them.
"""

import ast
import concurrent.futures
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import build_cmd as build_mod  # noqa: E402
from skt import check as check_mod  # noqa: E402
from skt import status as status_mod  # noqa: E402

from test_artifacts import STALE_DOC, fake_cli  # noqa: E402
from test_status import make_home, make_repo  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_root_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.delenv("SKT_ARTIFACTS", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-root" / ".skill-manager"))


BUILD_DOC = {
    "schema": 1,
    "home": "/h/.skill-manager",
    "dry_run": False,
    "summary": {"selected": 2, "rebuilt": 1, "no_op": 0, "failed": 0,
                "already_current": 0, "not_buildable": 1, "still_stale": 0},
    "steps": [
        {"id": "cli-shim:skill-script/computeq", "kind": "cli-shim",
         "owner": "deploy-helm", "action": "rebuild",
         "producer": "skill-script:computeq (declared by deploy-helm)",
         "reason": "its declared inputs moved",
         "freshness_before": "stale", "materialization_before": "materialized",
         "freshness_after": "current", "materialization_after": "materialized",
         "outcome": "built", "verifiable": True},
        {"id": "projection:default:claude:x#claude/skills/x", "kind": "projection",
         "owner": "x", "action": "not-buildable", "producer": None,
         "reason": "no per-artifact producer — `skill-manager bind x`",
         "freshness_before": "stale", "materialization_before": "materialized",
         "freshness_after": None, "materialization_after": None,
         "outcome": "skipped", "verifiable": False},
    ],
}


def home_with_artifacts(tmp_path, doc=None, **kwargs) -> tuple[Path, Path]:
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    fake_cli(home, stdout=json.dumps(doc if doc is not None else STALE_DOC), **kwargs)
    return repo, home


# ------------------------------------------------ the notification itself


def test_check_names_the_artifact_and_the_rebuild_command(tmp_path):
    repo, home = home_with_artifacts(tmp_path)
    report = check_mod.collect(repo)
    notes = [n for n in report["notifications"] if n["kind"] == "stale-artifact"]
    assert len(notes) == 1
    note = notes[0]
    assert note["name"] == "computeq"
    assert note["artifact"] == "cli-shim:skill-script/computeq"
    # CHANGED 2026-08-26, and the old expectation is worth naming: it asserted
    # `skt build computeq`, and STALE_DOC's own rows say why that was the bug
    # rather than the behaviour. The fixture carries `unit-store:deploy-helm`
    # as stale AND `computeq` with `because: [unit-store:deploy-helm]` -- so
    # `skt build computeq` re-derives from an input that is still wrong,
    # reports "built", and the row reads stale again. Measured on the
    # operator's project home: check -> build -> check printed the identical
    # three lines, and one `skill-manager sync deploy-helm` cleared all
    # sixteen stale artifacts. The remedy now names the root cause.
    assert note["fix"] == "skill-manager sync deploy-helm"
    text = check_mod.render_text(report)
    assert "artifact computeq is stale (deploy-helm moved)" in text
    assert "rebuild with: skill-manager sync deploy-helm" in text


def test_the_cause_carries_both_hashes_when_the_unit_check_found_them(tmp_path):
    """The literal notification the ticket asks for.

    `deploy-helm moved a3f21c8 -> 9b17e40` is only sayable when the SAME
    pass also found a new version for that unit; the hashes then come from
    the typed `new-version` row, never from parsing the verdict's prose.
    """
    row = {"id": "cli-shim:skill-script/computeq", "name": "computeq",
           "kind": "cli-shim", "owner": "deploy-helm", "reason": "inputs moved",
           "because": ["unit-store:deploy-helm"]}
    state = {"state": "ok", "rows": [row], "rebuildable": 1}
    unit_notes = [{"kind": "new-version", "unit": "deploy-helm",
                   "installed": "a3f21c8", "remote": "9b17e40",
                   "message": "..."}]
    notes = check_mod._artifact_notifications(state, unit_notes)
    assert notes[0]["message"] == (
        "artifact computeq is stale (deploy-helm moved a3f21c8 -> 9b17e40)"
    )


def test_an_artifact_stale_on_its_own_inputs_quotes_its_own_reason(tmp_path):
    row = {"id": "cli-shim:skill-script/computeq", "name": "computeq",
           "kind": "cli-shim", "owner": "deploy-helm",
           "reason": "its declared inputs moved: recorded 1a2b3c4d, now 9f8e7d6c",
           "because": []}
    notes = check_mod._artifact_notifications({"state": "ok", "rows": [row]}, [])
    assert "recorded 1a2b3c4d, now 9f8e7d6c" in notes[0]["message"]


def test_a_declared_but_never_built_artifact_is_not_a_notification(tmp_path):
    """A lazily-provisioned home's normal state is not news.

    STALE_DOC holds three stale artifacts: a materialized shim, a shim that
    was never built, and a unit-store row `build` has no producer for. Only
    the first may be offered with `skt build`.
    """
    repo, home = home_with_artifacts(tmp_path)
    report = check_mod.collect(repo)
    notes = [n["name"] for n in report["notifications"] if n["kind"] == "stale-artifact"]
    assert notes == ["computeq"]
    assert report["artifacts"]["stale"] == 3
    assert report["artifacts"]["not_built"] == 1
    assert report["artifacts"]["rebuildable"] == 1


def test_notifications_are_bounded_and_the_rest_are_counted(tmp_path):
    doc = json.loads(json.dumps(STALE_DOC))
    template = doc["stale"][0]
    doc["stale"] = [
        dict(template, id=f"cli-shim:skill-script/tool{i}") for i in range(9)
    ]
    doc["summary"]["stale"] = 9
    repo, home = home_with_artifacts(tmp_path, doc)
    report = check_mod.collect(repo)
    named = [n for n in report["notifications"] if n["kind"] == "stale-artifact"]
    assert len(named) == check_mod.MAX_ARTIFACT_NOTIFICATIONS
    text = check_mod.render_text(report)
    assert "+6 more stale artifact(s) — rebuild them with: skt build --stale" in text


def test_an_artifact_no_other_line_explains_is_named_first(tmp_path):
    """Found by the end-to-end demonstration, on a real home.

    Three rows are named. Without ordering they were all downstream of two
    units whose `new-version` lines sat three lines above — and the one
    artifact whose own fingerprint had diverged, the only thing in the
    report nothing else said, fell into `+5 more`.
    """
    rows = [
        {"id": f"cli-shim:brew/downstream{i}", "name": f"downstream{i}",
         "kind": "cli-shim", "owner": "deploy-helm",
         "reason": "it is built from unit-store:deploy-helm, which is stale",
         "because": ["unit-store:deploy-helm"]}
        for i in range(4)
    ]
    rows.append({
        "id": "cli-shim:skill-script/tracing-observability",
        "name": "tracing-observability", "kind": "cli-shim",
        "owner": "tracing-observability",
        "reason": "its declared inputs moved: recorded 85159a51, now 0fda67d9",
        "because": ["provisioned-tree:cache/tracing-observability-wheelhouse"],
    })
    unit_notes = [{"kind": "new-version", "unit": "deploy-helm",
                   "installed": "a2e79d57", "remote": "a367aa00", "message": "..."}]
    notes = check_mod._artifact_notifications(
        {"state": "ok", "rows": rows, "rebuildable": len(rows)}, unit_notes
    )
    assert notes[0]["name"] == "tracing-observability"
    assert len(notes) == check_mod.MAX_ARTIFACT_NOTIFICATIONS


def test_status_names_the_same_artifacts_check_named(tmp_path):
    """The record's rows are stored in the order the notifications chose.

    `skt status` reads this block back and names a few; naming a DIFFERENT
    few than the notifications two lines below is how a report stops being
    believed.
    """
    doc = json.loads(json.dumps(STALE_DOC))
    downstream = dict(doc["stale"][0], id="cli-shim:brew/downstream",
                      reason="it is built from unit-store:deploy-helm, which is stale",
                      because=["unit-store:deploy-helm"])
    direct = dict(doc["stale"][0], id="cli-shim:skill-script/planted",
                  reason="its declared inputs moved", because=[])
    doc["stale"] = [downstream, direct]
    repo, home = home_with_artifacts(tmp_path, doc)
    report = check_mod.collect(repo)
    # No new-version notification exists in this fixture, so both rows are
    # "not explained elsewhere" and the order is stable — assert the record
    # AGREES with the notifications rather than a particular order.
    named = [n["artifact"] for n in report["notifications"]
             if n["kind"] == "stale-artifact"]
    assert [row["id"] for row in report["artifacts"]["rows"]] == named


def test_notifications_make_check_exit_10(tmp_path):
    repo, home = home_with_artifacts(tmp_path)
    assert check_mod.run(as_json=True, cached=False, start=repo) == check_mod.NOTIFY_EXIT


def test_a_home_with_nothing_stale_notifies_nothing(tmp_path):
    doc = {"schema": 2, "home": "/h",
           "summary": {"artifacts": 12, "stale": 0, "unverifiable": 0,
                       "current": 12, "stale_by_kind": {}},
           "stale": [], "unverifiable": []}
    repo, home = home_with_artifacts(tmp_path, doc)
    report = check_mod.collect(repo)
    assert report["notifications"] == []
    assert report["artifacts"]["state"] == "ok"
    assert check_mod.run(as_json=False, cached=False, start=repo) == 0


# ------------------------------------------------------- honest degrading


def test_a_pre_artifacts_cli_says_so_and_notifies_nothing(tmp_path):
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    fake_cli(home, stderr="Unmatched arguments from index 0: 'artifacts'\n", exit_code=2)
    report = check_mod.collect(repo)
    assert report["artifacts"]["state"] == "unsupported"
    assert report["notifications"] == []
    text = check_mod.render_text(report)
    assert "artifacts not checked (unsupported)" in text
    assert "predates the artifact graph" in text


def test_a_home_with_no_cli_pin_is_silent_not_broken(tmp_path):
    """Every fixture home in this suite is in that state; it must be quiet."""
    repo = make_repo(tmp_path / "repo")
    make_home(repo)
    report = check_mod.collect(repo)
    assert report["artifacts"]["state"] == "no-cli"
    assert check_mod._artifact_lines(report) == []


def test_a_hung_cli_is_a_timeout_not_a_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(check_mod, "ARTIFACT_BUDGET_SECONDS", 1)
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    fake_cli(home, stdout=json.dumps(STALE_DOC), sleep=30)
    started = time.monotonic()
    report = check_mod.collect(repo)
    assert time.monotonic() - started < 20
    assert report["artifacts"]["state"] == "timeout"
    assert report["notifications"] == []


def test_the_probe_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SKT_ARTIFACTS", "0")
    repo, home = home_with_artifacts(tmp_path)
    report = check_mod.collect(repo)
    assert report["artifacts"]["state"] == "off"
    assert report["notifications"] == []


def test_a_crashing_probe_never_escapes_as_a_traceback(tmp_path, monkeypatch):
    from skt import artifacts as artifacts_mod

    def boom(*a, **k):
        raise ValueError("something nobody predicted")

    monkeypatch.setattr(artifacts_mod, "stale", boom)
    repo, home = home_with_artifacts(tmp_path)
    report = check_mod.collect(repo)  # must not raise
    assert report["artifacts"]["state"] == "error"
    assert "ValueError" in report["artifacts"]["reason"]


# -------------------------------------------- the cache contract, in full


def forbid_every_spawn(monkeypatch):
    """Poison every route to a child process AND to a worker thread.

    Broader than `subprocess`, on purpose: the point of the 2-second
    PostToolUse budget is that this path costs one file read, and a probe
    that reached for `os.posix_spawn` or a thread pool would satisfy a
    narrower sentinel while breaking the same contract.
    """

    def boom(*a, **k):
        raise AssertionError("a child process was started on the cache-only path")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    for name in ("fork", "posix_spawn", "posix_spawnp", "system",
                 "spawnv", "spawnvp", "spawnvpe", "popen"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, boom)
    monkeypatch.setattr(
        concurrent.futures.ThreadPoolExecutor, "submit",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("work was queued on the cache-only path")
        ),
    )


def seed_record(home: Path, *, artifacts: dict, checked_at: float,
                notifications: list | None = None) -> None:
    record = {
        "schema": check_mod.SCHEMA_VERSION,
        "home": str(home),
        "tier": "project",
        "artifacts": artifacts,
        "checked_units": ["alpha"],
        "unverifiable": [],
        "network": True,
        "checked_at": checked_at,
        "notifications": notifications or [],
    }
    path = check_mod.state_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


ARTIFACT_BLOCK = {
    "state": "ok", "total": 190, "stale": 55, "unverifiable": 66, "current": 69,
    "not_built": 11, "rebuildable": 1,
    "rows": [{"id": "cli-shim:skill-script/computeq", "name": "computeq",
              "kind": "cli-shim", "owner": "deploy-helm",
              "reason": "its declared inputs moved", "because": []}],
}

NOTE = {
    "kind": "stale-artifact", "artifact": "cli-shim:skill-script/computeq",
    "name": "computeq", "owner": "deploy-helm",
    "message": "artifact computeq is stale (deploy-helm moved)",
    "fix": "skt build computeq",
}


#: Every `artifacts.state` a cached record can carry. The four non-`ok`
#: ones exist because a cached-path spawn conditioned on "the last pass
#: could not measure, so measure now" is a plausible regression that a
#: suite seeded only with `ok` records never drives — it would pass the
#: whole poisoning matrix below while breaking the contract the matrix
#: exists to protect.
ARTIFACT_STATES = {
    "ok": ARTIFACT_BLOCK,
    "unsupported": {"state": "unsupported",
                    "reason": "the pin predates the artifact graph",
                    "fix": "skt sync skill-manager"},
    "timeout": {"state": "timeout", "reason": "did not finish inside 6.0s", "fix": "skt check"},
    "error": {"state": "error", "reason": "exit 70", "fix": "run it yourself"},
    "no-cli": {"state": "no-cli", "reason": "this home has no pin", "fix": "home shims"},
    "off": {"state": "off", "reason": "SKT_ARTIFACTS is set to off"},
}


@pytest.mark.parametrize("artifact_state", sorted(ARTIFACT_STATES))
@pytest.mark.parametrize("state", ["fresh", "expired"])
def test_cached_spawns_nothing_for_any_recorded_artifact_state(
    tmp_path, monkeypatch, capsys, state, artifact_state
):
    """The cross product the first version of this suite did not drive.

    A cached record whose `artifacts.state` is `timeout` or `error` is
    exactly the shape that invites "the last pass could not measure it, so
    measure it now" — a repair on the 2-second path. There is no such
    repair, in any of the six states, fresh or expired.
    """
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    fake_cli(home, stdout=json.dumps(STALE_DOC))  # present, and never used
    checked_at = time.time() if state == "fresh" else time.time() - 10_000
    seed_record(home, artifacts=ARTIFACT_STATES[artifact_state], checked_at=checked_at)

    forbid_every_spawn(monkeypatch)
    started = time.monotonic()
    rc = check_mod.run(as_json=True, cached=True, ttl=900, start=repo)
    assert time.monotonic() - started < 2
    report = json.loads(capsys.readouterr().out)
    assert rc == 0  # no notifications were seeded in either record
    if state == "fresh":
        assert report["artifacts"]["state"] == artifact_state
    else:
        assert report["cache_state"] == check_mod.CACHE_EXPIRED
        assert "artifacts" not in report


@pytest.mark.parametrize("state", ["fresh", "expired", "missing", "malformed", "no-home"])
def test_cached_spawns_nothing_in_any_cache_state(tmp_path, monkeypatch, capsys, state):
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    # A real, working CLI pin is present in every case but the last: the
    # contract is that --cached does not USE it, not that it is absent.
    fake_cli(home, stdout=json.dumps(STALE_DOC))
    if state == "fresh":
        seed_record(home, artifacts=ARTIFACT_BLOCK, checked_at=time.time(),
                    notifications=[NOTE])
    elif state == "expired":
        seed_record(home, artifacts=ARTIFACT_BLOCK, checked_at=time.time() - 10_000,
                    notifications=[NOTE])
    elif state == "malformed":
        path = check_mod.state_file(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")
    elif state == "no-home":
        repo = make_repo(tmp_path / "homeless")
        monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "nowhere"))

    forbid_every_spawn(monkeypatch)
    started = time.monotonic()
    rc = check_mod.run(as_json=True, cached=True, ttl=900, start=repo)
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out
    assert elapsed < 2, f"the cache-only path took {elapsed:.2f}s"

    if state == "no-home":
        assert rc == 1
        return
    report = json.loads(out)
    assert report["cache_state"] == {
        "fresh": check_mod.CACHE_FRESH,
        "expired": check_mod.CACHE_EXPIRED,
        "missing": check_mod.CACHE_MISSING,
        "malformed": check_mod.CACHE_MISSING,
    }[state]
    if state == "fresh":
        assert rc == check_mod.NOTIFY_EXIT
        assert report["artifacts"]["stale"] == 55
    else:
        # Stale content rides under `stale` and never at the top level, so a
        # cached artifact notification can never re-fire as exit 10.
        assert rc == 0
        assert report["notifications"] == []
        assert "artifacts" not in report
        if state == "expired":
            assert report["stale"]["artifacts"]["stale"] == 55


def test_check_does_not_import_the_probe_at_module_scope(tmp_path):
    """The import lives inside `collect()`; this asserts it by AST.

    The first spelling of this test was `"import artifacts" not in <top of
    file>` — a substring scan of ONE spelling, which `from .artifacts
    import stale` walks straight past. Parsing the module and looking at
    its top-level body catches every spelling.

    Worth being exact about what this does and does not claim. It does NOT
    claim `skt.artifacts` is absent from `sys.modules` on the hook path: the
    real entry point imports the `skt` package, whose `__init__` re-exports
    the surface, so it is loaded there — at no spawn and no measurable cost.
    What it claims is that nothing on the cached path in THIS module reaches
    for it, which is the property the poisoning tests then drive.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "skt" / "check.py").read_text()
    module = ast.parse(source)
    offenders = []
    for node in module.body:  # TOP LEVEL only — a nested import is the point
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if "artifacts" in a.name]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith("artifacts"):
                offenders.append(node.module)
            offenders += [a.name for a in node.names if a.name == "artifacts"]
    assert offenders == [], (
        f"skt.artifacts must be imported inside collect(), not at module scope: {offenders}"
    )
    # And it really is imported somewhere, or the assertion above is vacuous.
    assert "import artifacts as artifacts_mod" in source


def test_the_dedup_marker_stays_unconditional(tmp_path):
    """skt#22: an env-GATED dedup re-injected on every tool call.

    ARTI-10 adds a notification kind, which is exactly the kind of change
    that tempts a caller to gate the block that surfaces it. The hook's
    marker must still key on (session, checked_at) with no env condition.
    """
    hook = (Path(__file__).resolve().parents[1] / "hooks" / "skt-post-tool.sh").read_text()
    marker = hook.split("HOME_DIR=", 1)[1]
    assert 'MARKER="$HOME_DIR/logs/skt/.notified-' in marker
    assert "${TMPDIR:-/tmp}/skt-notified-" in marker
    # The fallback exists precisely so the dedup is not conditional on the
    # home being resolvable.
    assert 'if [ -z "$HOME_DIR" ]' in hook


def test_a_pass_that_decided_nothing_is_not_cached_as_a_result(tmp_path):
    """`state == "ok"` means the CLI answered, not that anything was decided.

    All remotes unreachable AND every artifact `unverifiable` is a pass that
    resolved exactly as much as one that reached no remote at all. Caching
    it lets `--cached` serve `all current` at exit 0 for a whole TTL over a
    home nothing in that pass could decide.
    """
    doc = {"schema": 2, "home": "/h",
           "summary": {"artifacts": 190, "stale": 0, "unverifiable": 190,
                       "current": 0, "stale_by_kind": {}},
           "stale": [],
           "unverifiable": [{"id": f"cli-shim:brew/t{i}", "kind": "cli-shim",
                             "owner": "u", "freshness": "unverifiable",
                             "materialization": "unknown",
                             "reason": "no install fingerprint", "because": []}
                            for i in range(190)]}
    repo, home = home_with_artifacts(tmp_path, doc)
    report = check_mod.collect(repo)
    report["network"] = True
    report["checked_units"] = ["alpha"]
    report["unverifiable"] = ["alpha"]  # the remote half resolved nothing either
    check_mod._write_cache(report)
    assert not check_mod.state_file(home).exists(), (
        "a pass that decided nothing on either axis must not be cached"
    )
    # And it must not read as an all-clear where it IS shown.
    assert "could be decided" in check_mod.render_text(report)


def test_one_decided_artifact_is_still_a_result_worth_caching(tmp_path):
    """The offline session this predicate was widened for, unchanged."""
    repo, home = home_with_artifacts(tmp_path)  # 3 stale, 1 unverifiable
    report = check_mod.collect(repo)
    report["network"] = True
    report["checked_units"] = ["alpha"]
    report["unverifiable"] = ["alpha"]
    check_mod._write_cache(report)
    assert check_mod.state_file(home).exists()


def test_the_printed_command_survives_the_operators_shell(tmp_path):
    """`skt build jinja2-cli[yaml]` is `zsh: no matches found` in zsh.

    One of the seven rebuildable artifacts in the operator's own project
    home is named exactly that. A notification whose entire value is a
    retypable command has to be quoted.
    """
    doc = json.loads(json.dumps(STALE_DOC))
    doc["stale"] = [dict(doc["stale"][0], id="cli-shim:pip/jinja2-cli[yaml]")]
    repo, home = home_with_artifacts(tmp_path, doc)
    report = check_mod.collect(repo)
    note = [n for n in report["notifications"] if n["kind"] == "stale-artifact"][0]
    assert note["fix"] == "skt build 'jinja2-cli[yaml]'"
    assert "skt build 'jinja2-cli[yaml]'" in check_mod.render_text(report)
    # A name that needs no quoting must not acquire any.
    plain = check_mod._artifact_notifications(
        {"state": "ok", "rows": [{"id": "cli-shim:skill-script/computeq",
                                  "name": "computeq", "kind": "cli-shim",
                                  "owner": "u", "reason": "moved", "because": []}]}, [])
    assert plain[0]["fix"] == "skt build computeq"


def test_use_network_governs_the_remote_phase_only(tmp_path):
    """It always meant "no remote", never "no subprocess" — root-tier
    `_local_state` has always run git under it. The artifact probe is local
    too, so it gets its OWN switch rather than widening that flag's meaning
    behind a caller's back."""
    log = tmp_path / "argv.log"
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    fake_cli(home, stdout=json.dumps(STALE_DOC), argv_log=log)
    check_mod.collect(repo, use_network=False)
    assert log.exists(), "a local probe is not governed by use_network"
    log.unlink()
    report = check_mod.collect(repo, use_network=False, probe_artifacts=False)
    assert not log.exists()
    assert report["artifacts"]["state"] == "off"


# --------------------------------------------------------- status read-back


def test_status_reads_the_counts_check_recorded(tmp_path):
    log = tmp_path / "argv.log"
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo, units={"alpha": {}})
    fake_cli(home, stdout=json.dumps(STALE_DOC), argv_log=log)
    seed_record(home, artifacts=ARTIFACT_BLOCK, checked_at=time.time() - 240)
    report = status_mod.collect(repo)
    # `skt status` already shells out to git for checkout context; what it
    # must NOT do is add a second spawn of the skill-manager CLI in front
    # of every session for a number `skt check` already recorded.
    assert not log.exists(), "skt status must not run the skill-manager CLI"
    assert report["artifacts"]["stale"] == 55
    text = status_mod.render_text(report)
    assert "artifacts  55 stale of 190 — 1 rebuildable, 11 declared-not-built" in text
    assert "measured 4m ago" in text
    assert "rebuild with: skt build --stale   (computeq)" in text


def test_status_is_silent_when_check_has_never_run(tmp_path):
    repo = make_repo(tmp_path / "repo")
    make_home(repo, units={"alpha": {}})
    report = status_mod.collect(repo)
    assert report["artifacts"] is None
    assert "artifacts" not in status_mod.render_text(report)


def test_status_reads_a_pre_arti_10_record_without_inventing_a_line(tmp_path):
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo, units={"alpha": {}})
    path = check_mod.state_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 2, "home": str(home), "checked_at": time.time(),
                                "notifications": [], "checked_units": []}))
    report = status_mod.collect(repo)
    assert report["artifacts"] is None


def test_status_stays_bounded_with_many_rebuildable(tmp_path):
    block = dict(ARTIFACT_BLOCK, rebuildable=9, rows=[
        {"id": f"cli-shim:skill-script/t{i}", "name": f"t{i}", "kind": "cli-shim",
         "owner": "u", "reason": "moved", "because": []} for i in range(9)
    ])
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo, units={"alpha": {}})
    seed_record(home, artifacts=block, checked_at=time.time())
    text = status_mod.render_text(status_mod.collect(repo))
    assert "+5 more" in text
    assert len([ln for ln in text.splitlines() if ln.startswith(("artifacts", "     "))]) <= 2


def test_status_names_a_pre_artifacts_cli_rather_than_a_count(tmp_path):
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo, units={"alpha": {}})
    seed_record(home, checked_at=time.time(), artifacts={
        "state": "unsupported", "reason": "the pin predates the artifact graph"})
    text = status_mod.render_text(status_mod.collect(repo))
    assert "artifacts  not measured (unsupported)" in text


# ------------------------------------------------------------- skt build


def test_build_renders_what_it_did_and_what_it_would_not(tmp_path, capsys):
    repo, home = home_with_artifacts(tmp_path, BUILD_DOC)
    rc = build_mod.run(["cli-shim:skill-script/computeq"], start=repo)
    out = capsys.readouterr().out
    assert rc == 0
    assert "built       cli-shim:skill-script/computeq" in out
    assert "now: current" in out
    # Named, loudly, never buried under the success line.
    assert "not rebuilt here — nothing in `skt build` produces these:" in out
    assert "projection:default:claude:x#claude/skills/x" in out


def test_build_resolves_the_short_name_a_notification_prints(tmp_path, capsys):
    """`skt build computeq` — the command the notification tells an agent
    to run — must reach `skill-manager build <full id>`."""
    log = tmp_path / "argv.log"
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    list_doc = {
        "schema": 1, "home": str(home),
        "ledger": {"path": "artifacts.lock.toml", "present": True, "schema": 1,
                   "recorded_at": "now", "artifacts": 1},
        "artifacts": [{"id": "cli-shim:skill-script/computeq", "kind": "cli-shim",
                       "owner": "deploy-helm", "materialization": "materialized",
                       "agreement": "agrees", "origin": "recorded", "inputs": [],
                       "observed_inputs": [], "outputs": [], "source": None,
                       "recorded": {}, "actual": {}}],
        "summary": {"artifacts": 1, "by_kind": {}, "by_materialization": {},
                    "by_agreement": {}, "by_origin": {}},
    }
    # One script, two documents: the list call is answered first, the build
    # call second, so the resolution really happens through the CLI.
    cli = home / "bin" / "cli" / "skill-manager"
    cli.parent.mkdir(parents=True, exist_ok=True)
    (cli.parent / "list.json").write_text(json.dumps(list_doc))
    (cli.parent / "build.json").write_text(json.dumps(BUILD_DOC))
    cli.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        f'case "$1" in artifacts) cat "{cli.parent}/list.json";; '
        f'*) cat "{cli.parent}/build.json";; esac\n'
    )
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    rc = build_mod.run(["computeq"], start=repo)
    capsys.readouterr()
    assert rc == 0
    calls = log.read_text().splitlines()
    assert calls[0] == "artifacts list --json"
    assert calls[1] == "build --json cli-shim:skill-script/computeq"


def test_build_refuses_with_one_error_line_and_one_fix_line(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    fake_cli(home, stderr="Unmatched arguments from index 0: 'build'\n", exit_code=2)
    rc = build_mod.run([], start=repo)
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("error: this home's skill-manager has no `build` verb")
    assert "fix:   skt sync skill-manager" in err


def test_build_prints_did_you_mean_for_an_unknown_short_name(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    home = make_home(repo)
    fake_cli(home, stdout=json.dumps({
        "schema": 1, "home": str(home),
        "ledger": {"path": "x", "present": False, "schema": None,
                   "recorded_at": None, "artifacts": 0},
        "artifacts": [{"id": "cli-shim:brew/computeq-helper", "kind": "cli-shim",
                       "owner": "u", "materialization": "materialized",
                       "agreement": "agrees", "origin": "recorded", "inputs": [],
                       "observed_inputs": [], "outputs": [], "source": None,
                       "recorded": {}, "actual": {}}],
        "summary": {"artifacts": 1, "by_kind": {}, "by_materialization": {},
                    "by_agreement": {}, "by_origin": {}},
    }))
    rc = build_mod.run(["computeq"], start=repo)
    err = capsys.readouterr().err
    assert rc == 2
    assert "no artifact named 'computeq'" in err
    assert "did you mean:" in err
    assert "cli-shim:brew/computeq-helper" in err


def test_build_passes_a_failing_exit_code_through_with_its_report(tmp_path, capsys):
    doc = json.loads(json.dumps(BUILD_DOC))
    doc["steps"][0].update(outcome="failed", freshness_after="stale")
    doc["summary"].update(rebuilt=0, failed=1, still_stale=1)
    repo, home = home_with_artifacts(tmp_path, doc, exit_code=1)
    rc = build_mod.run([], start=repo)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILED      cli-shim:skill-script/computeq" in out
    assert "1 rebuild(s) failed" in out
    assert "1 of the selected artifact(s) are still stale" in out


def test_build_says_why_unverifiable_is_not_a_failure(tmp_path, capsys):
    doc = json.loads(json.dumps(BUILD_DOC))
    doc["steps"][0].update(freshness_after="unverifiable", verifiable=False)
    repo, home = home_with_artifacts(tmp_path, doc)
    build_mod.run([], start=repo)
    out = capsys.readouterr().out
    assert "the rebuild ran; this home records no install fingerprint" in out


def test_build_via_the_cli_entry_point(tmp_path):
    repo, home = home_with_artifacts(tmp_path, BUILD_DOC)
    cli = Path(__file__).resolve().parents[1] / "src" / "skt" / "cli.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "build", "--dry-run", "--json"],
        capture_output=True, text=True, cwd=repo,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["summary"]["selected"] == 2
