# /// script
# requires-python = ">=3.10"
# dependencies = ["testgraphsdk"]
#
# [tool.uv.sources]
# testgraphsdk = { path = "../sdk/python", editable = true }
# ///
"""skt.check-cached-costs-one-read — the PostToolUse budget, measured.

`hooks/hooks.json` gives the PostToolUse hook **2 seconds**, and it runs
on every tool call of every session. The property that makes that budget
survivable is not "this is usually fast": it is that `skt check --cached`
is CONTRACT-CACHE-ONLY — one state-file read, no subprocess, no network,
no repair, in every cache state including the cold one.

A wall-clock assertion cannot establish that. It passes on an idle laptop
and fails on a loaded runner while measuring neither the spawn nor the
read. So this node drives `check.run(cached=True)` in-process, on each
pinned interpreter, under two independent instruments — CPython's audit
hook and a poisoned `subprocess`/`os.spawn*`/`fork`/`ThreadPoolExecutor`
surface — across all five cache states plus the corrupt-record fold.

Both instruments are CONTROLLED on the same run: one scenario makes a
real `Popen` call to prove the audit hook is installed, and one runs the
LIVE path of the same function to prove `--cached` and non-`--cached`
are distinguishable to this probe. Without those two, "spawns: []" is
indistinguishable from an instrument that was never armed.

The probe itself is `test_graph/support/cached_no_spawn_probe.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from testgraphsdk import NodeResult, NodeSpec, node, procs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from skt_fixture import REPO_ROOT, child_env  # noqa: E402

UPSTREAM = "skt.wrapper-installed"
PROBE = Path(__file__).resolve().parents[1] / "support" / "cached_no_spawn_probe.py"

SPEC = (
    NodeSpec("skt.check-cached-costs-one-read")
    .kind("assertion")
    .depends_on(UPSTREAM)
    .tags("skt", "check", "hooks", "matrix")
    .timeout("600s")
    .side_effects("fs:tmp")
    .output("cacheStates", "string")
)

# scenario -> (exit code, cache_state, state-file reads)
EXPECTED = {
    "no-home": (1, None, 0),
    "missing": (0, "missing", 1),
    "corrupt": (0, "missing", 1),
    "expired": (0, "expired", 1),
    "expired-text": (0, None, 1),
    "fresh-quiet": (0, "fresh", 1),
    "fresh-notify": (10, "fresh", 1),
}


@node(SPEC)
def main(ctx):
    result = NodeResult.pass_(ctx.node_id)
    interpreters = json.loads(ctx.get(UPSTREAM, "interpreters") or "{}")
    if not interpreters:
        return NodeResult.fail(ctx.node_id, f"{UPSTREAM} published no interpreters")

    work = ctx.report_dir / "fixtures" / "skt-check-cached"
    work.mkdir(parents=True, exist_ok=True)
    states_seen: set[str] = set()

    for version, interpreter in sorted(interpreters.items()):
        out = work / f"probe-{version}.json"
        record = procs.run(
            ctx,
            f"cached-probe-{version}",
            [interpreter, str(PROBE), str(REPO_ROOT), str(work / version), str(out)],
            cwd=str(work),
            env=child_env(),
        )
        result.process(record)
        if record.log_path:
            result.artifact(f"cached-probe-{version}-log", record.log_path)
        result.assertion(f"{version}: probe exits zero", record.exit_code == 0)
        if not out.is_file():
            return NodeResult.fail(
                ctx.node_id,
                f"{version}: probe wrote no result (exit {record.exit_code}); see {record.log_path}",
            )
        payload = json.loads(out.read_text())
        result.artifact(f"cached-probe-{version}-result", str(out))
        by_scenario = {r["scenario"]: r for r in payload["results"]}
        result.assertion(
            f"{version}: probe ran on the pinned interpreter",
            payload["python"].startswith(version + "."),
        )
        result.log(f"{version}: probe python {payload['python']} at {payload['executable']}")

        # --- the controls, FIRST: without them nothing below is evidence
        audit_control = by_scenario.get("control-audit-sees-a-real-spawn", {})
        result.assertion(
            f"{version}: control — the audit hook records a real spawn",
            "subprocess.Popen" in (audit_control.get("spawns") or []),
        )
        live_control = by_scenario.get("control-live-path-does-spawn", {})
        result.assertion(
            f"{version}: control — the LIVE check path does spawn",
            bool(live_control.get("spawns")),
        )

        for scenario, (rc, cache_state, reads) in EXPECTED.items():
            record_ = by_scenario.get(scenario)
            if record_ is None:
                result.assertion(f"{version}/{scenario}: scenario ran", False)
                continue
            states_seen.add(scenario)
            label = f"{version}/{scenario}"
            result.assertion(f"{label}: exits {rc}", record_["exit_code"] == rc)
            if cache_state is not None:
                result.assertion(
                    f"{label}: reports cache_state {cache_state}",
                    record_["cache_state"] == cache_state,
                )
            # THE CLAIM.
            result.assertion(f"{label}: spawns nothing", record_["spawns"] == [])
            result.assertion(
                f"{label}: reads the state file exactly {reads} time(s)",
                record_["state_file_reads"] == reads,
            )
            result.assertion(f"{label}: opens no other file", record_["other_opens"] == [])
            result.assertion(f"{label}: writes nothing back", record_["cache_unchanged"] is True)

        # --- the two states whose CONTENT is the contract, not just a label
        expired = by_scenario["expired"]
        result.assertion(
            f"{version}: expired keeps stale content out of `notifications`",
            expired["notifications"] == [] and expired["has_stale"] is True,
        )
        result.assertion(
            f"{version}: expired preserves the stale content under `stale`",
            bool(expired["stale_notifications"]),
        )
        expired_text = by_scenario["expired-text"]
        result.assertion(
            f"{version}: expired text labels every stale line [stale]",
            "[stale]" in expired_text["printed"] and "expired" in expired_text["printed"],
        )
        result.assertion(
            f"{version}: fresh notifications are the only state that exits 10",
            len(by_scenario["fresh-notify"]["notifications"] or []) == 1
            and by_scenario["fresh-quiet"]["notifications"] == []
            and by_scenario["fresh-quiet"]["exit_code"] == 0,
        )
        result.assertion(
            f"{version}: a home that does not exist is an error, not a fresh cache",
            by_scenario["no-home"]["error"] is not None,
        )

    result.metric("cacheStates", len(states_seen))
    return result.publish("cacheStates", ",".join(sorted(states_seen)))


if __name__ == "__main__":
    main()
