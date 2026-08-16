import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skt import check as check_mod  # noqa: E402

from test_status import make_home, make_repo  # noqa: E402


GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


def make_unit_upstream(base: Path, name: str) -> tuple[Path, str]:
    """A bare origin plus its current tip hash."""
    bare = base / f"{name}-upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = base / f"{name}-seed"
    work.mkdir()
    (work / "SKILL.md").write_text(f"# {name}\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run([*GIT, "-C", str(work), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(work), "commit", "-q", "-m", "v1"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", str(bare), "main"], check=True
    )
    tip = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return bare, tip


def advance_upstream(bare: Path, base: Path) -> str:
    clone = base / f"{bare.stem}-advance"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    (clone / "SKILL.md").write_text("# advanced\n")
    subprocess.run([*GIT, "-C", str(clone), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(clone), "commit", "-q", "-m", "v2"], check=True)
    subprocess.run(["git", "-C", str(clone), "push", "-q"], check=True)
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def isolate_root_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_MANAGER_HOME", raising=False)
    monkeypatch.setenv("SKT_ROOT_HOME", str(tmp_path / "fake-root" / ".skill-manager"))


def unit_record(bare: Path, tip: str) -> dict:
    return {"origin": str(bare), "gitHash": tip, "gitRef": "main"}


def test_current_unit_reports_nothing(tmp_path):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    make_home(repo, units={"alpha": unit_record(bare, tip)})
    report = check_mod.collect(repo)
    assert report["notifications"] == []
    assert report["checked_units"] == ["alpha"]


def test_stale_unit_notifies_new_version(tmp_path):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    make_home(repo, units={"alpha": unit_record(bare, tip)})
    new_tip = advance_upstream(bare, tmp_path)
    report = check_mod.collect(repo)
    assert len(report["notifications"]) == 1
    note = report["notifications"][0]
    assert note["kind"] == "new-version"
    assert note["remote"] == new_tip[:8]
    assert "skt sync alpha" in note["message"]


def test_multiple_stale_units_get_dependency_hint(tmp_path):
    repo = make_repo(tmp_path / "repo")
    units = {}
    for name in ("alpha", "beta"):
        bare, tip = make_unit_upstream(tmp_path, name)
        units[name] = unit_record(bare, tip)
        advance_upstream(bare, tmp_path)
    make_home(repo, units=units)
    report = check_mod.collect(repo)
    assert len(report["notifications"]) == 2
    assert "dependency order" in report["hint"]


def test_root_tier_prompts_publish_for_dirty_store_unit(tmp_path, monkeypatch):
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# locally improved\n")
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))
    report = check_mod.collect(repo)
    kinds = {n["kind"] for n in report["notifications"]}
    assert "sync-with-root" in kinds
    note = next(n for n in report["notifications"] if n["kind"] == "sync-with-root")
    assert note["state"] == "dirty"
    assert "publish changes globally" in note["message"]


def test_project_tier_never_prompts_push_side(tmp_path):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# locally improved\n")
    report = check_mod.collect(repo)
    assert all(n["kind"] != "sync-with-root" for n in report["notifications"])


def test_cached_path_avoids_network(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    report = check_mod.collect(repo)
    check_mod._write_cache(report)
    def boom(*a, **k):
        raise AssertionError("network hit on cached path")
    monkeypatch.setattr(check_mod, "_remote_tip", boom)
    cached = check_mod.cached_report(home, ttl=900)
    assert cached["from_cache"] is True and cached["cache_state"] == "fresh"
    t0 = time.monotonic()
    check_mod.cached_report(home, ttl=900)
    assert time.monotonic() - t0 < 0.05


def test_expired_cache_is_not_served_as_current(tmp_path):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    report = check_mod.collect(repo)
    report["checked_at"] = time.time() - 10_000
    check_mod._write_cache(report)
    cached = check_mod.cached_report(home, ttl=900)
    assert cached["cache_state"] == "expired"
    assert cached["notifications"] == []
    assert cached["stale"]["checked_units"] == ["alpha"]


def test_exit_codes_distinguish_notify(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    make_home(repo, units={"alpha": unit_record(bare, tip)})
    assert check_mod.run(as_json=False, cached=False, start=repo) == 0
    advance_upstream(bare, tmp_path)
    assert check_mod.run(as_json=False, cached=False, start=repo) == check_mod.NOTIFY_EXIT


# --- skill-publisher-skill#15: `ahead` was reading a stale LOCAL ref ---------
#
# `@{upstream}` is a local remote-tracking ref that only a fetch moves. The
# store checkout is advanced by a path that fetches into FETCH_HEAD and
# resets — so `rev-list @{upstream}..HEAD` counts commits that are already
# published, and `skt check` called it unpushed work.
#
# Measured in this repo's project home, six units at once: git-integration-repo
# 52, acp-cdc-ai-python 11, skill-dev-skill 8, test-graph 7,
# vision-toolbelt-skill 5, skill-manager 5 — every one with HEAD equal to its
# live remote tip, i.e. fully published.


def stale_upstream_store(home: Path, bare: Path, name: str, base: Path) -> tuple[Path, str]:
    """A store whose HEAD is published but whose `@{upstream}` is behind.

    Built the way the defect is built: fetch by URL (not by remote name,
    so no remote-tracking ref is updated) and reset onto FETCH_HEAD.
    """
    unit_dir = home / "skills" / name
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    new_tip = advance_upstream(bare, base)
    subprocess.run(
        ["git", "-C", str(unit_dir), "fetch", "--no-tags", "--quiet", str(bare), "main"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(unit_dir), "reset", "--hard", "--quiet", "FETCH_HEAD"], check=True
    )
    return unit_dir, new_tip


def ahead_count(unit_dir: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(unit_dir), "rev-list", "--count", "@{upstream}..HEAD"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def test_stale_upstream_ref_is_not_reported_as_ahead(tmp_path, monkeypatch):
    """The shape measured live: published HEAD, behind local ref, rev-list > 0."""
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    unit_dir, new_tip = stale_upstream_store(home, bare, "alpha", tmp_path)
    (home / "installed" / "alpha.json").write_text(
        json.dumps({"name": "alpha", "version": "1.0.0", "unitKind": "SKILL",
                    **unit_record(bare, new_tip)})
    )
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))
    assert ahead_count(unit_dir) > 0  # the local ref really is behind

    report = check_mod.collect(repo)
    assert all(n["kind"] != "sync-with-root" for n in report["notifications"]), report
    assert report["upstream_stale"] == ["alpha"]


def test_genuinely_unpushed_work_still_reports_ahead(tmp_path, monkeypatch):
    """The true positive must survive: a commit the remote does NOT have.

    A GUARD, not before/after evidence: it passes on the parent commit
    too, and that is the point — it fails only if the #15 adjudication
    ever starts swallowing work nobody else has. It therefore asserts
    behaviour and deliberately says nothing about the new report keys.
    """
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# work nobody else has\n")
    subprocess.run([*GIT, "-C", str(unit_dir), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(unit_dir), "commit", "-q", "-m", "local only"], check=True)
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))

    report = check_mod.collect(repo)
    note = next(n for n in report["notifications"] if n["kind"] == "sync-with-root")
    assert note["state"] == "ahead"
    assert "skt publish alpha" in note["message"]


def test_store_ahead_of_remote_tip_is_not_a_new_version(tmp_path):
    """`installed != tip` is ancestry-blind: ARTI-00's `debugging` case."""
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# one commit past the tip\n")
    subprocess.run([*GIT, "-C", str(unit_dir), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(unit_dir), "commit", "-q", "-m", "ahead"], check=True)
    local = subprocess.run(["git", "-C", str(unit_dir), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    (home / "installed" / "alpha.json").write_text(
        json.dumps({"name": "alpha", "version": "1.0.0", "unitKind": "SKILL",
                    **unit_record(bare, local)})
    )
    assert local != tip

    report = check_mod.collect(repo)
    assert all(n["kind"] != "new-version" for n in report["notifications"]), report
    assert report["ahead_of_remote"] == ["alpha"]


def test_a_really_stale_store_still_gets_its_new_version_notice(tmp_path):
    """The suppression must not swallow the case the notification is for.

    A GUARD in the same sense: behavioural only, and green on both sides.
    """
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(repo, units={"alpha": unit_record(bare, tip)})
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    new_tip = advance_upstream(bare, tmp_path)  # store never fetched it

    report = check_mod.collect(repo)
    note = next(n for n in report["notifications"] if n["kind"] == "new-version")
    assert note["remote"] == new_tip[:8]
    assert "skt sync alpha" in note["message"]


def test_unpushed_work_on_top_of_a_fetched_tip_still_reports_ahead(tmp_path, monkeypatch):
    """The case where the ancestry answer is the ONLY thing deciding it.

    In the two cases above, `merge-base --is-ancestor` has an escape
    hatch: a genuinely stale store does not hold the remote tip object at
    all, so git answers "cannot decide" and the verdict falls through to
    `ahead` without the ancestry ever being consulted. Here the store
    HOLDS the tip — it fetched it — and carries an unpushed commit ON TOP
    of it. The object is present, the probe runs, and the answer has to
    come out False. Found by this change's review.
    """
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(fake_root, units={"alpha": unit_record(bare, tip)})
    unit_dir, new_tip = stale_upstream_store(home, bare, "alpha", tmp_path)
    # ...and now a commit the remote has never seen, on top of that tip
    (unit_dir / "SKILL.md").write_text("# nobody else has this\n")
    subprocess.run([*GIT, "-C", str(unit_dir), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(unit_dir), "commit", "-q", "-m", "unpushed"], check=True)
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))

    # the tip object really is present, so the "unknown object" path cannot fire
    assert subprocess.run(
        ["git", "-C", str(unit_dir), "cat-file", "-e", new_tip], capture_output=True
    ).returncode == 0

    report = check_mod.collect(repo)
    note = next(n for n in report["notifications"] if n["kind"] == "sync-with-root")
    assert note["state"] == "ahead"
    assert report["upstream_stale"] == []


# --- ARTI-23: a unit whose store records an error is not "stale" ---------
#
# The home is honest and the reader was not. `errors[0].kind` is recorded by
# the installer, parsed by `homes.py`, marked by `status.py` — and `check.py`
# contained zero occurrences of `errors`, so a mid-merge store was framed as
# routine staleness whose remedy is the one action that re-enters the merge.


def conflicted_store(home: Path, bare: Path, name: str, base: Path) -> tuple[Path, str]:
    """A store checkout with real unmerged paths and a retained stash.

    Reproduces the shape the operator's project home actually holds:
    `skill-manager sync` stashed local work as `skill-manager-sync`,
    merged the upstream, and the stash pop conflicted — leaving `UU`
    files and `stash@{0}` still on the stack. HEAD is a local commit that
    does NOT contain the new remote tip, which is the `deploy-helm` /
    `spec-double-compiler` case: the check would otherwise emit
    `new version available — pull with: skt sync`.
    """
    unit_dir = home / "skills" / name
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# local divergence\n")
    subprocess.run([*GIT, "-C", str(unit_dir), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(unit_dir), "commit", "-q", "-m", "local"], check=True)
    (unit_dir / "SKILL.md").write_text("# uncommitted work somebody cares about\n")
    subprocess.run(
        [*GIT, "-C", str(unit_dir), "stash", "push", "-q", "-m", "skill-manager-sync"],
        check=True,
    )
    new_tip = advance_upstream(bare, base)
    subprocess.run(
        ["git", "-C", str(unit_dir), "fetch", "--no-tags", "--quiet", str(bare), "main"],
        check=True,
    )
    merged = subprocess.run(
        [*GIT, "-C", str(unit_dir), "merge", "--no-edit", "FETCH_HEAD"], capture_output=True
    )
    assert merged.returncode != 0, "the fixture must actually conflict"
    unmerged = subprocess.run(
        ["git", "-C", str(unit_dir), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert unmerged, "the fixture must leave unmerged paths"
    stash = subprocess.run(
        ["git", "-C", str(unit_dir), "stash", "list"], capture_output=True, text=True, check=True
    ).stdout
    assert "skill-manager-sync" in stash, "the fixture must preserve the stash"
    return unit_dir, new_tip


MERGE_CONFLICT_ERROR = {
    "kind": "MERGE_CONFLICT",
    "message": (
        "stash pop conflict after merging https://github.com/x/alpha main "
        "— local changes preserved at stash@{0}"
    ),
    "firstSeenAt": "2026-08-11T22:24:13.710799Z",
}


def test_merge_conflict_unit_is_never_told_to_sync(tmp_path):
    """The reported defect, driven as a SEQUENCE.

    A unit records MERGE_CONFLICT, its store holds unmerged paths and a
    preserved stash, and its recorded gitHash disagrees with the live
    remote tip. The emitted notification must not be a sync instruction:
    `skt sync` re-runs the merge that produced the conflict and is the
    one action that puts the stashed work at risk.
    """
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(
        repo, units={"alpha": {**unit_record(bare, tip), "errors": [MERGE_CONFLICT_ERROR]}}
    )
    unit_dir, new_tip = conflicted_store(home, bare, "alpha", tmp_path)
    assert new_tip != tip  # the record really is behind the live tip

    report = check_mod.collect(repo, probe_artifacts=False)
    assert all(n["kind"] != "new-version" for n in report["notifications"]), report
    note = next(n for n in report["notifications"] if n["kind"] == "unit-error")
    assert note["unit"] == "alpha"
    assert note["state"] == "MERGE_CONFLICT"
    text = check_mod.render_text(report)
    assert "skt sync alpha" not in text, text
    assert "MERGE_CONFLICT" in text
    assert str(unit_dir) in text  # the remedy names the store to resolve IN
    assert "stash@{0}" in text  # ...and the work that must not be lost


def test_merge_conflict_unit_at_the_tip_is_named_not_called_ahead(tmp_path):
    """`hyper-experiments-finance`: store AT the tip, record behind it.

    Ancestry (ARTI-10) already keeps this out of `new-version`, but it
    lands in `ahead_of_remote` — "ahead of the remote tip (nothing to
    pull)" — which is true about the hashes and silent about the fact
    that the store cannot be synced at all until someone resolves it.
    """
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(
        repo, units={"alpha": {**unit_record(bare, tip), "errors": [MERGE_CONFLICT_ERROR]}}
    )
    unit_dir = home / "skills" / "alpha"
    subprocess.run(["git", "clone", "-q", str(bare), str(unit_dir)], check=True)
    (unit_dir / "SKILL.md").write_text("# conflicted\n")
    subprocess.run([*GIT, "-C", str(unit_dir), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(unit_dir), "commit", "-q", "-m", "past the tip"], check=True)

    report = check_mod.collect(repo, probe_artifacts=False)
    assert report["ahead_of_remote"] == [], report
    assert any(n["kind"] == "unit-error" for n in report["notifications"]), report


def test_merge_conflict_store_is_never_told_to_publish(tmp_path, monkeypatch):
    """The same unread field, on the push side.

    A conflicted store is `dirty` to `git status --porcelain`, so the
    root tier prompted `skt publish` — publishing a half-merged tree.
    """
    fake_root = tmp_path / "fake-root"
    repo = make_repo(fake_root / "anywhere")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    home = make_home(
        fake_root, units={"alpha": {**unit_record(bare, tip), "errors": [MERGE_CONFLICT_ERROR]}}
    )
    conflicted_store(home, bare, "alpha", tmp_path)
    monkeypatch.setenv("SKILL_MANAGER_HOME", str(home))

    report = check_mod.collect(repo, probe_artifacts=False)
    assert all(n["kind"] != "sync-with-root" for n in report["notifications"]), report
    assert "skt publish alpha" not in check_mod.render_text(report)


def test_an_error_that_says_nothing_about_the_store_keeps_its_new_version(tmp_path):
    """The guard: only errors about the STORE CHECKOUT suppress a pull.

    `GATEWAY_UNAVAILABLE` is a record about MCP registration. The store
    is fine, the pull advice is correct, and suppressing it would trade
    one wrong message for a missing right one.
    """
    repo = make_repo(tmp_path / "repo")
    bare, tip = make_unit_upstream(tmp_path, "alpha")
    make_home(
        repo,
        units={
            "alpha": {
                **unit_record(bare, tip),
                "errors": [{"kind": "GATEWAY_UNAVAILABLE", "message": "gateway did not respond"}],
            }
        },
    )
    new_tip = advance_upstream(bare, tmp_path)

    report = check_mod.collect(repo, probe_artifacts=False)
    note = next(n for n in report["notifications"] if n["kind"] == "new-version")
    assert note["remote"] == new_tip[:8]
    assert "skt sync alpha" in note["message"]
