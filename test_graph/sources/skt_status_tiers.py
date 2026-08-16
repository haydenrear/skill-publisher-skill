# /// script
# requires-python = ">=3.10"
# dependencies = ["testgraphsdk"]
#
# [tool.uv.sources]
# testgraphsdk = { path = "../sdk/python", editable = true }
# ///
"""skt.status-tiers — what `skt status` reports about a home it is handed.

`skt status` is the SessionStart disclosure: the one thing an agent reads
before it does anything else. Its two load-bearing claims are

  1. WHICH HOME this session writes, and at which TIER — root, project or
     worktree. Get that wrong and an agent edits the operator's global
     home believing it is editing a ticket's.
  2. WHAT IS IN that home — units, which of them are change-managed,
     which are loaded, which are broken, the policy, and whether a drift
     gate will refuse the next launch.

The three tiers are built here as three REAL checkouts on disk: an
operator root, a main working tree with its own home, and a linked
worktree of that main tree with its own home. The distinction skt draws
is a git-plumbing one (`git rev-parse --git-dir` vs `--git-common-dir`),
so nothing short of a real linked worktree exercises it.

Every assertion runs once PER PINNED INTERPRETER, through the wrapper
`skt.wrapper-installed` built. Same fixtures, same expected answers: if
3.11 and 3.13 disagree about a home, that is the finding.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from testgraphsdk import NodeResult, NodeSpec, node

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from skt_fixture import (  # noqa: E402
    build_home,
    child_env,
    git,
    init_repo,
    unit_record,
)

UPSTREAM = "skt.wrapper-installed"

SPEC = (
    NodeSpec("skt.status-tiers")
    .kind("assertion")
    .depends_on(UPSTREAM)
    .tags("skt", "status", "matrix")
    .timeout("600s")
    .side_effects("fs:tmp")
    .output("tiersChecked", "string")
)

TICKET_PLAN = """\
name: skt-fixture-workflow
tickets:
  - id: ARTI-19
    status: open
  - id: ARTI-14
    status: closed
"""


def _skt(wrapper: str, args: list[str], *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [wrapper, *args], cwd=str(cwd), capture_output=True, text=True, timeout=180, env=env
    )


def _json_report(proc: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


@node(SPEC)
def main(ctx):
    result = NodeResult.pass_(ctx.node_id)
    wrappers = json.loads(ctx.get(UPSTREAM, "wrappers") or "{}")
    if not wrappers:
        return NodeResult.fail(ctx.node_id, f"{UPSTREAM} published no wrappers")

    work = ctx.report_dir / "fixtures" / "skt-status"
    work.mkdir(parents=True, exist_ok=True)

    # --- the units every tier's home carries ------------------------------
    units = [
        unit_record(
            "alpha",
            version="2.1.0",
            origin="https://example.invalid/alpha.git",
            git_hash="a" * 40,
            git_ref="refs/heads/main",
        ),
        unit_record("beta", version="0.9.0", kind="PLUGIN"),
        unit_record("gamma", version="1.2.3", errors=["manifest missing a description"]),
    ]

    def home_at(path: Path) -> Path:
        return build_home(
            path,
            units=units,
            loaded={"alpha"},
            unreadable=["broken"],
            plugins=["skt"],
            policy="frozen",
            drift=True,
            cli_tools={"uv": ["ruff", "tlc2"]},
        )

    # --- tier: root -------------------------------------------------------
    root_repo = init_repo(work / "root-checkout")
    root_home = home_at(work / "operator-home")

    # --- tier: project, and tier: worktree off the same main tree ---------
    project_repo = init_repo(work / "project-checkout")
    (project_repo / "specs" / "desired_program_model").mkdir(parents=True, exist_ok=True)
    (project_repo / "specs" / "desired_program_model" / "ticket_plan.yaml").write_text(TICKET_PLAN)
    git("add", "-A", cwd=project_repo)
    git("commit", "-q", "-m", "plan", cwd=project_repo)
    git("branch", "epic/artifact-dag", cwd=project_repo)
    home_at(project_repo / ".skill-manager")

    worktree = work / "project-checkout-ARTI-19"
    added = git("worktree", "add", "-q", str(worktree), "-b", "feature/ARTI-19", cwd=project_repo)
    if added.returncode != 0:
        return NodeResult.fail(ctx.node_id, f"git worktree add failed: {added.stderr.strip()}")
    home_at(worktree / ".skill-manager")

    # --- checkout kind: integration parent and a constituent under it -----
    integration = init_repo(work / "integration-checkout", integration=True)
    constituent = init_repo(integration / "constituents" / "leaf")

    # A root home that is NOT the one under test, so the project and
    # worktree tiers cannot be classified `root` by accident.
    decoy_root = build_home(work / "decoy-root-home", units=[unit_record("decoy")])

    checked: list[str] = []
    for version, wrapper in sorted(wrappers.items()):
        env_root = child_env(SKILL_MANAGER_HOME=str(root_home), SKT_ROOT_HOME=str(root_home))
        env_local = child_env(SKT_ROOT_HOME=str(decoy_root))

        cases = [
            ("root", root_repo, env_root, str(root_home)),
            ("project", project_repo, env_local, str(project_repo / ".skill-manager")),
            ("worktree", worktree, env_local, str(worktree / ".skill-manager")),
        ]
        for tier, cwd, env, expected_home in cases:
            proc = _skt(wrapper, ["status", "--json"], cwd=cwd, env=env)
            report = _json_report(proc)
            label = f"{version}/{tier}"
            result.assertion(f"{label}: status exits zero", proc.returncode == 0)
            result.assertion(f"{label}: reports tier {tier}", report.get("tier") == tier)
            result.assertion(
                f"{label}: reports the home it will write",
                Path(report.get("home", "")).resolve() == Path(expected_home).resolve(),
            )
            if report.get("tier") != tier:
                result.log(f"{label}: got tier={report.get('tier')!r} home={report.get('home')!r}")

            # --- what is IN the home -------------------------------------
            by_name = {u["name"]: u for u in report.get("units", [])}
            result.assertion(f"{label}: all four installed records are read", len(by_name) == 4)
            result.assertion(
                f"{label}: alpha is change-managed and loaded",
                by_name.get("alpha", {}).get("change_managed") is True
                and by_name.get("alpha", {}).get("loaded") is True
                and by_name.get("alpha", {}).get("git_hash") == "a" * 8,
            )
            result.assertion(
                f"{label}: beta has no git provenance, so it is not change-managed",
                by_name.get("beta", {}).get("change_managed") is False
                and by_name.get("beta", {}).get("kind") == "PLUGIN",
            )
            result.assertion(
                f"{label}: gamma's recorded errors survive to the report",
                by_name.get("gamma", {}).get("errors") == ["manifest missing a description"],
            )
            result.assertion(
                f"{label}: an unreadable record is reported, not dropped",
                by_name.get("broken", {}).get("kind") == "UNREADABLE"
                and bool(by_name.get("broken", {}).get("errors")),
            )
            result.assertion(f"{label}: plugins are listed", report.get("plugins") == ["skt"])
            result.assertion(f"{label}: policy is read", report.get("policy") == "frozen")
            result.assertion(
                f"{label}: an unacknowledged drift record is pending",
                report.get("drift_pending") is True,
            )
            result.assertion(
                f"{label}: cli tools come from cli-lock.toml",
                report.get("cli_tools") == ["ruff", "tlc2"],
            )

            # --- the TEXT rendering, which is what a session actually sees
            text = _skt(wrapper, ["status"], cwd=cwd, env=env).stdout
            result.assertion(f"{label}: text names the tier", f"tier: {tier}" in text)
            result.assertion(
                f"{label}: text names the drift remedy",
                "DRIFT PENDING" in text and "skill-manager home drift --ack" in text,
            )
            checked.append(label)

        # --- ticket / epic / spec context, on the worktree ----------------
        wt_report = _json_report(_skt(wrapper, ["status", "--json"], cwd=worktree, env=env_local))
        checkout = wt_report.get("checkout", {})
        result.assertion(
            f"{version}: feature/ARTI-19 yields ticket ARTI-19",
            checkout.get("branch") == "feature/ARTI-19" and checkout.get("ticket") == "ARTI-19",
        )
        result.assertion(
            f"{version}: an epic branch that exists but is not checked out is 'available'",
            checkout.get("epic") == "artifact-dag" and checkout.get("on_epic_branch") is False,
        )
        spec = wt_report.get("spec_workflow", {})
        result.assertion(
            f"{version}: the ticket plan is read",
            spec.get("name") == "skt-fixture-workflow"
            and spec.get("open_tickets") == ["ARTI-19"]
            and spec.get("ticket_in_plan") is True,
        )
        sync = wt_report.get("worktree_sync") or {}
        result.assertion(
            f"{version}: a fresh worktree is in sync with its parent",
            sync.get("in_sync") is True and sync.get("behind") == 0,
        )

        # --- checkout kind ------------------------------------------------
        integ_kind = _json_report(
            _skt(wrapper, ["status", "--json"], cwd=integration, env=env_root)
        ).get("checkout", {})
        result.assertion(
            f"{version}: integration.toml at the root makes the checkout 'integration'",
            integ_kind.get("kind") == "integration",
        )
        leaf_kind = _json_report(
            _skt(wrapper, ["status", "--json"], cwd=constituent, env=env_root)
        ).get("checkout", {})
        result.assertion(
            f"{version}: a checkout under one is 'constituent'",
            leaf_kind.get("kind") == "constituent",
        )
        result.assertion(
            f"{version}: a checkout under neither is 'standalone'",
            _json_report(_skt(wrapper, ["status", "--json"], cwd=root_repo, env=env_root))
            .get("checkout", {})
            .get("kind")
            == "standalone",
        )

        # --- no home at all is an ERROR, not an empty report --------------
        #
        # A SYSTEM temp dir, not one under this report: `find_home` walks
        # ancestors, and every directory inside this checkout has the
        # checkout's own real `.skill-manager` above it. Measured — the
        # first version of this assertion failed for exactly that reason,
        # which is itself the behaviour under test working correctly.
        orphan = Path(tempfile.mkdtemp(prefix="skt-tg-no-home-"))
        missing = _skt(
            wrapper,
            ["status"],
            cwd=orphan,
            env=child_env(SKT_ROOT_HOME=str(work / "does-not-exist")),
        )
        result.assertion(
            f"{version}: no home found exits non-zero and says so",
            missing.returncode != 0 and "no skill-manager home found" in missing.stdout,
        )
        shutil.rmtree(orphan, ignore_errors=True)

    result.metric("tierChecks", len(checked))
    return result.publish("tiersChecked", ",".join(checked))


if __name__ == "__main__":
    main()
