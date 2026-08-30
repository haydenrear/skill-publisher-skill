# /// script
# requires-python = ">=3.10"
# dependencies = ["testgraphsdk"]
#
# [tool.uv.sources]
# testgraphsdk = { path = "../sdk/python", editable = true }
# ///
"""skt.wrapper-installed — the thing skill-manager actually installs.

`skill-scripts/install-skt.sh` is the whole distribution story for this
repo: skill-manager runs it at install time, it picks an interpreter, it
writes ONE wrapper at `$SKILL_MANAGER_BIN_DIR/skt`, and every other
surface — the two hooks, `skt ticket`, an agent typing `skt status` —
reaches skt through that file and nothing else. The pytest suite imports
`skt.cli` directly and therefore never executes it.

This node executes it, once per PINNED interpreter.

WHY PINNED. `pyproject.toml` declares `requires-python = ">=3.11"`. That
is a claim, and until something runs the wrapper on the floor it stays a
claim: bare `python3` is 3.14 on the machine this was written on and
3.12 on a hosted runner, so an unpinned probe measures the box. The
matrix here is the same pair `.github/workflows/ci.yml` runs pytest on,
and `SKT_PYTHON` — the installer's own documented override — is how the
interpreter is forced, so nothing is mocked to arrange it.

The wrapper paths this node publishes are the ONLY skt the downstream
nodes call. That is deliberate: a graph that imported `skt.cli` would be
a slower copy of the unit suite.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

from testgraphsdk import NodeResult, NodeSpec, node, procs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from skt_fixture import (  # noqa: E402
    INSTALL_SCRIPT,
    REPO_ROOT,
    ToolMissing,
    build_home,
    child_env,
    pinned_versions,
    resolve_interpreter,
    unit_record,
)

SPEC = (
    NodeSpec("skt.wrapper-installed")
    .kind("testbed")
    .tags("skt", "install", "matrix")
    .timeout("900s")
    .side_effects("fs:tmp", "net:external")
    .output("wrappers", "string")
    .output("interpreters", "string")
    .output("versions", "string")
    .output("probeHome", "string")
)


def _declared_floor() -> str:
    for line in (REPO_ROOT / "pyproject.toml").read_text().splitlines():
        if line.strip().startswith("requires-python"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _declared_version() -> str:
    for line in (REPO_ROOT / "pyproject.toml").read_text().splitlines():
        if line.strip().startswith("version"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


@node(SPEC)
def main(ctx):
    result = NodeResult.pass_(ctx.node_id)
    workdir = ctx.report_dir / "fixtures" / "skt-wrapper"
    workdir.mkdir(parents=True, exist_ok=True)

    # The home the installer's own `status --json` probe reads. Nothing in
    # it is change-managed, so the probe cannot reach a network.
    probe_home = build_home(
        workdir / "probe-home",
        units=[unit_record("probe-unit")],
        policy="live",
    )

    result.assertion("install script is executable", os.access(INSTALL_SCRIPT, os.X_OK))
    floor = _declared_floor()
    result.assertion("pyproject declares an interpreter floor", floor.startswith(">="))
    result.log(f"declared requires-python: {floor!r}")

    versions = pinned_versions()
    wrappers: dict[str, str] = {}
    interpreters: dict[str, str] = {}
    for version in versions:
        try:
            interpreter = resolve_interpreter(version)
        except ToolMissing as exc:
            # A precondition this node cannot supply is a FAILURE with a
            # name, never a skip: a matrix that quietly shrinks to the one
            # interpreter the runner had is the measurement not happening.
            return NodeResult.fail(ctx.node_id, f"python {version}: {exc}")
        result.log(f"python {version} -> {interpreter}")

        # INSTALL FROM INSIDE THE HOME, because that is the only shape a
        # real install has: skill-manager copies the unit into the home and
        # then runs its install script with $SKILL_DIR pointing INTO that
        # home. The old fixture pointed $SKILL_DIR at the checkout and
        # $SKILL_MANAGER_HOME at an unrelated temp dir -- a pair that cannot
        # occur in production, and the shape that let a frozen wrapper look
        # correct here.
        home_dir = workdir / version
        bin_dir = home_dir / "bin" / "cli"
        cache_dir = home_dir / "cache"
        unit_dir = home_dir / "plugins" / "skt"
        if not unit_dir.exists():
            shutil.copytree(REPO_ROOT, unit_dir,
                            ignore=shutil.ignore_patterns(".git", "test_graph", ".venv"))
        record = procs.run(
            ctx,
            f"install-skt-{version}",
            ["bash", str(INSTALL_SCRIPT)],
            cwd=str(workdir),
            env=child_env(
                SKT_PYTHON=interpreter,
                SKILL_MANAGER_BIN_DIR=str(bin_dir),
                SKILL_MANAGER_CACHE_DIR=str(cache_dir),
                SKILL_DIR=str(unit_dir),
                SKILL_MANAGER_HOME=str(home_dir),
            ),
        )
        result.process(record)
        if record.log_path:
            result.artifact(f"install-{version}-log", record.log_path)
        result.assertion(f"{version}: installer exits zero", record.exit_code == 0)

        wrapper = bin_dir / "skt"
        result.assertion(f"{version}: wrapper written at $SKILL_MANAGER_BIN_DIR/skt", wrapper.is_file())
        if not wrapper.is_file():
            return NodeResult.fail(
                ctx.node_id, f"{version}: install-skt.sh exited {record.exit_code} and wrote no wrapper"
            )
        mode = wrapper.stat().st_mode
        result.assertion(f"{version}: wrapper is executable", bool(mode & stat.S_IXUSR))

        # The interpreter is BAKED IN, not resolved from PATH at call time.
        # That is the property that makes the wrapper survive a minimal
        # PATH, and it is the reason the installer probes at install time.
        body = wrapper.read_text()
        result.assertion(f"{version}: wrapper execs an absolute interpreter", interpreter in body)
        # THE INTERPRETER IS PINNED; THE UNIT PATH IS NOT. Those are
        # deliberately different: the interpreter lives outside every home so
        # an absolute path is a pin, while the unit lives INSIDE this home so
        # an absolute path would be a freeze -- the shim would go on running
        # the installing home's copy from any other home
        # (skill-manager#262). This assertion used to require
        # `<checkout>/src/skt/cli.py` in the body, which is exactly the
        # frozen shape, so the graph defended the defect.
        result.assertion(
            f"{version}: wrapper resolves its unit from the home it lives in",
            'home="$(cd -- "$shim_dir/../.." && pwd -P)"' in body,
        )
        result.assertion(
            f"{version}: wrapper names the unit path home-RELATIVE, not absolute",
            "plugins/skt/src/skt/cli.py" in body
            and str(unit_dir / "src" / "skt" / "cli.py") not in body,
        )

        help_run = procs.run(
            ctx, f"skt-help-{version}", [str(wrapper), "--help"], env=child_env()
        )
        result.process(help_run)
        result.assertion(f"{version}: skt --help exits zero", help_run.exit_code == 0)
        help_text = _log_text(ctx, help_run)
        for verb in ("status", "check", "sync", "ticket", "publish"):
            result.assertion(f"{version}: --help lists `{verb}`", verb in help_text)

        version_run = procs.run(
            ctx, f"skt-version-{version}", [str(wrapper), "--version"], env=child_env()
        )
        result.process(version_run)
        declared = _declared_version()
        result.assertion(
            f"{version}: --version reports the packaged version {declared}",
            version_run.exit_code == 0 and f"skt {declared}" in _log_text(ctx, version_run),
        )

        # NO PYTHON ON PATH AT ALL. This is the condition the baked-in
        # interpreter exists for — install-skt.sh's own comment names it
        # ("a bare `python3` in the wrapper breaks the moment PATH is
        # minimal") — and it is falsifiable: a wrapper that spelled the
        # interpreter `python3` cannot pass this and the current one must.
        #
        # `bash` IS on the minimal PATH, deliberately. The wrapper's
        # shebang is `#!/usr/bin/env bash`, so env resolves the shell
        # through PATH; a genuinely empty PATH fails in the shebang,
        # before the interpreter question is ever reached, and would make
        # this assertion prove something other than what it claims.
        minimal_bin = workdir / version / "minimal-bin"
        minimal_bin.mkdir(parents=True, exist_ok=True)
        bash_path = shutil.which("bash") or "/bin/bash"
        link = minimal_bin / "bash"
        if not link.exists():
            link.symlink_to(bash_path)
        result.assertion(
            f"{version}: the minimal PATH really has no python",
            shutil.which("python3", path=str(minimal_bin)) is None
            and shutil.which("python", path=str(minimal_bin)) is None,
        )
        bare = procs.run(
            ctx,
            f"skt-minimal-path-{version}",
            [str(wrapper), "--version"],
            env=child_env(PATH=str(minimal_bin)),
        )
        result.process(bare)
        result.assertion(
            f"{version}: wrapper answers with no python on PATH", bare.exit_code == 0
        )

        wrappers[version] = str(wrapper)
        interpreters[version] = interpreter

    result.metric("interpreters", len(wrappers))
    return (
        result.publish("wrappers", json.dumps(wrappers))
        .publish("interpreters", json.dumps(interpreters))
        .publish("versions", ",".join(versions))
        .publish("probeHome", str(probe_home))
    )


def _log_text(ctx, record) -> str:
    if not record.log_path:
        return ""
    path = ctx.report_dir / record.log_path
    return path.read_text(errors="replace") if path.is_file() else ""


if __name__ == "__main__":
    main()
