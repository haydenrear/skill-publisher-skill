# /// script
# requires-python = ">=3.10"
# dependencies = ["testgraphsdk"]
#
# [tool.uv.sources]
# testgraphsdk = { path = "../sdk/python", editable = true }
# ///
"""skt.ticket-roundtrip — a worktree created and torn down for real.

`skt ticket new|info|close` is the lifecycle skt puts its name on. It
does not reimplement `wt`: it imports git-issue-workflow's typed Python
surface and adds skt's framing. Two things follow, and only an
end-to-end run reaches either:

  * the ROUND TRIP. `new` must produce a real linked worktree on a real
    branch, `info` must answer about that same worktree, and `close`
    must remove the directory while KEEPING the branch. Every one of
    those is a fact about a filesystem and a git object store.

  * the REFUSALS. When the git-issue-workflow surface is not importable,
    skt names the remedy that fits THIS home — and telling the four
    cases apart was itself a merged fix (`fix(ticket): tell
    not-installed from not-synced, and name a remedy that runs`, #25),
    landed because `sync` was being prescribed for a unit that was not
    installed, in five different homes. A remedy that cannot run is
    worse than no remedy: it costs the agent a failed command and a
    wrong mental model.

`INTEGRATION_SKIP_HOME=1` is exported for the round trip. That is
new-change.sh's own documented switch, and it draws the node's boundary
honestly in both directions: provisioning a per-worktree Skill Manager
home needs a `skill-manager` binary plus a source home to clone from,
which is a skill-manager claim and not an skt one, so it is left out
rather than faked.

WHAT THIS NODE THEREFORE DOES NOT COVER, stated so the gap is not
mistaken for coverage: `skt ticket close` also carries a GATE that
refuses while the worktree's home holds unpublished skill edits
(`publish.edited_units` + `wt close`). With no home there is nothing for
that gate to inspect, so it is neither exercised nor asserted here.
Covering it needs a `skill-manager` on PATH and a source home, which is
a CI dependency this graph deliberately does not take on.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from testgraphsdk import NodeResult, NodeSpec, node

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from skt_fixture import build_home, child_env, git, init_repo, unit_record  # noqa: E402

UPSTREAM = "skt.wrapper-installed"
GIW_REMOTE = "https://github.com/haydenrear/git-issue-workflow-skill.git"

SPEC = (
    NodeSpec("skt.ticket-roundtrip")
    .kind("assertion")
    .depends_on(UPSTREAM)
    .tags("skt", "ticket", "lifecycle")
    .timeout("900s")
    .side_effects("fs:tmp", "net:external")
    .output("giwSource", "string")
)


def _resolve_giw(workdir: Path) -> tuple[Path, str]:
    """The git-issue-workflow unit this node drives, and where it came from.

    An installed copy is preferred because it needs no network; the clone
    is the fallback a hosted runner takes. WHICH ONE was used is
    published, because "the round trip passed" means a different thing
    against a working copy than against the pushed main.
    """
    override = os.environ.get("SKT_TG_GIW")
    if override:
        return Path(override), f"SKT_TG_GIW={override}"
    for base in (os.environ.get("SKILL_MANAGER_HOME"), str(Path.home() / ".skill-manager")):
        if not base:
            continue
        candidate = Path(base) / "skills" / "git-issue-workflow"
        if (candidate / "src" / "git_issue_workflow").is_dir() and (
            candidate / "scripts" / "wt"
        ).is_file():
            return candidate, f"installed:{candidate}"
    target = workdir / "git-issue-workflow"
    if not target.exists():
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", GIW_REMOTE, str(target)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"git clone {GIW_REMOTE} failed: {clone.stderr.strip()[:400]}")
    return target, f"clone:{GIW_REMOTE}"


def _skt(wrapper: str, args: list[str], *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [wrapper, *args], cwd=str(cwd), capture_output=True, text=True, timeout=600, env=env
    )


@node(SPEC)
def main(ctx):
    result = NodeResult.pass_(ctx.node_id)
    wrappers = json.loads(ctx.get(UPSTREAM, "wrappers") or "{}")
    if not wrappers:
        return NodeResult.fail(ctx.node_id, f"{UPSTREAM} published no wrappers")
    # One interpreter is enough here: the lifecycle is git and shell, and
    # the matrix claim is already carried by the nodes that read state.
    version, wrapper = sorted(wrappers.items())[0]

    # The clone is kept OUT of the wiped tree so a rerun does not re-fetch
    # it; everything else is rebuilt from scratch, because a `git worktree
    # add` for a branch a previous attempt already created fails, and a
    # node that only passes on a clean report directory is a node that
    # cannot be rerun. (Measured: the second run of this node in the same
    # reportDir failed for exactly that reason.)
    cache = ctx.report_dir / "fixtures" / "giw-source"
    cache.mkdir(parents=True, exist_ok=True)
    work = ctx.report_dir / "fixtures" / "skt-ticket"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        giw, source = _resolve_giw(cache)
    except RuntimeError as exc:
        return NodeResult.fail(ctx.node_id, str(exc))
    result.log(f"git-issue-workflow from {source}")
    result.assertion(
        "the git-issue-workflow python surface is present",
        (giw / "src" / "git_issue_workflow" / "wt.py").is_file(),
    )
    result.assertion("its `wt` front door is present", (giw / "scripts" / "wt").is_file())

    # ------------------------------------------------------------ round trip
    home = build_home(work / "home", units=[unit_record("skt", version="0.3.1", kind="PLUGIN")])
    skills = home / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    link = skills / "git-issue-workflow"
    if not link.exists():
        link.symlink_to(giw)

    repo = init_repo(work / "subject-repo")
    env = child_env(SKILL_MANAGER_HOME=str(home), INTEGRATION_SKIP_HOME="1")
    ticket = "TG-1"
    expected_worktree = work / f"subject-repo-{ticket}"

    created = _skt(wrapper, ["ticket", "new", ticket], cwd=repo, env=env)
    result.assertion(f"{version}: ticket new exits zero", created.returncode == 0)
    if created.returncode != 0:
        # Return the ACCUMULATED result, not a bare fail(): NodeResult.fail
        # builds a fresh object, so every assertion and log gathered so far
        # would be dropped and the envelope would say only "it failed".
        result.log(f"stdout: {created.stdout[-1200:]}")
        result.log(f"stderr: {created.stderr[-1200:]}")
        return result
    contract = _contract(created.stdout)
    result.assertion(
        "new prints the worktree and branch keys",
        contract.get("worktree") == str(expected_worktree.resolve())
        and contract.get("branch", "").startswith(f"feature/{ticket}"),
    )
    result.assertion(
        "new names its own close command",
        contract.get("close", "").startswith("skt ticket close"),
    )
    result.assertion(
        "new warns that home-side skill edits are in no git diff",
        "no git diff" in created.stdout and "skt publish" in created.stdout,
    )
    result.assertion("the worktree directory exists", expected_worktree.is_dir())
    result.assertion(
        "it is a LINKED worktree, not a copy",
        (expected_worktree / ".git").is_file(),
    )
    listing = git("worktree", "list", "--porcelain", cwd=repo).stdout
    result.assertion(
        "git knows about it", str(expected_worktree.resolve()) in listing.replace("/private", "")
        or str(expected_worktree.resolve()) in listing,
    )
    branches = git("branch", "--list", "--format=%(refname:short)", cwd=repo).stdout.split()
    result.assertion(f"the branch feature/{ticket} exists", f"feature/{ticket}" in branches)

    info = _skt(wrapper, ["ticket", "info", ticket], cwd=repo, env=env)
    info_keys = _contract(info.stdout)
    result.assertion(
        "info answers about the same worktree",
        info.returncode == 0 and info_keys.get("worktree") == contract.get("worktree"),
    )
    result.assertion(
        "info reports the worktree's base against its parent",
        "in sync with parent" in info.stdout,
    )

    closed = _skt(wrapper, ["ticket", "close", ticket], cwd=repo, env=env)
    result.assertion(f"{version}: ticket close exits zero", closed.returncode == 0)
    if closed.returncode != 0:
        result.log(f"stdout: {closed.stdout[-800:]}")
        result.log(f"stderr: {closed.stderr[-800:]}")
    result.assertion("close removes the worktree directory", not expected_worktree.exists())
    after = git("worktree", "list", "--porcelain", cwd=repo).stdout
    result.assertion("git no longer lists it", f"subject-repo-{ticket}" not in after)
    branches_after = git("branch", "--list", "--format=%(refname:short)", cwd=repo).stdout.split()
    result.assertion(
        "close KEEPS the branch — the work is not what is being torn down",
        f"feature/{ticket}" in branches_after,
    )
    result.assertion("close says the branch was kept", "kept" in closed.stdout)

    # ------------------------------------------------------------- refusals
    #
    # Four homes, four different faults, four remedies that must each be
    # runnable in the home they are printed for.
    bare_env = child_env(PYTHONPATH="")
    orphan = Path(tempfile.mkdtemp(prefix="skt-ticket-nohome-"))
    cases = [
        (
            "no home at all",
            child_env(
                PYTHONPATH="",
                SKT_ROOT_HOME=str(work / "does-not-exist"),
            ),
            orphan,
            ["no skill-manager home was found from here", "agent-home.sh"],
            ["skill-manager sync"],
        ),
    ]

    installed_no_surface = build_home(work / "home-installed-no-surface", units=[])
    (installed_no_surface / "skills" / "git-issue-workflow").mkdir(parents=True, exist_ok=True)
    cases.append(
        (
            "installed but no importable surface",
            {**bare_env, "SKILL_MANAGER_HOME": str(installed_no_surface)},
            repo,
            ["carries no importable python surface", "sync git-issue-workflow --git-latest"],
            ["is neither installed", "project resolve"],
        )
    )

    declared_repo = init_repo(work / "declared-repo")
    (declared_repo / "skill-project.toml").write_text(
        '[skills.git-issue-workflow]\nsource = "github:haydenrear/git-issue-workflow-skill"\n'
    )
    git("add", "-A", cwd=declared_repo)
    git("commit", "-q", "-m", "declare", cwd=declared_repo)
    declared_home = build_home(work / "home-declared", units=[])
    cases.append(
        (
            "declared in skill-project.toml but not installed",
            {**bare_env, "SKILL_MANAGER_HOME": str(declared_home)},
            declared_repo,
            ["is not installed in", "project resolve"],
            ["sync git-issue-workflow --git-latest"],
        )
    )

    undeclared_home = build_home(work / "home-undeclared", units=[])
    cases.append(
        (
            "neither installed nor declared",
            {**bare_env, "SKILL_MANAGER_HOME": str(undeclared_home)},
            repo,
            ["is neither installed", "`sync` cannot install it", "install github:haydenrear/"],
            ["project resolve"],
        )
    )

    for name, case_env, cwd, expected, forbidden in cases:
        proc = _skt(wrapper, ["ticket", "new", "TG-REFUSED"], cwd=cwd, env=case_env)
        blob = proc.stdout + proc.stderr
        result.assertion(f"refusal [{name}]: exits non-zero", proc.returncode != 0)
        for needle in expected:
            result.assertion(f"refusal [{name}]: names {needle!r}", needle in blob)
        for needle in forbidden:
            result.assertion(
                f"refusal [{name}]: does NOT prescribe {needle!r}", needle not in blob
            )
        result.assertion(
            f"refusal [{name}]: creates no worktree",
            not (Path(cwd).parent / f"{Path(cwd).name}-TG-REFUSED").exists(),
        )

    shutil.rmtree(orphan, ignore_errors=True)
    result.metric("refusalCases", len(cases))
    return result.publish("giwSource", source)


def _contract(text: str) -> dict[str, str]:
    keys = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in ("worktree", "branch", "close", "launch", "propagate"):
            keys[parts[0]] = parts[1].strip()
    return keys


if __name__ == "__main__":
    main()
