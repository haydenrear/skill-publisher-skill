"""Fixture builders shared by the `sktSurface` nodes.

Not a node. Nothing in here asserts anything — it only CONSTRUCTS the
disk states skt reads (skill-manager homes, checkouts, cache records) so
each node can spend its assertions on skt's behaviour instead of on
re-deriving the same fixture four times.

`sources/*.py` reach it with

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))

which is why this lives under `support/` and not under `sources/`: the
Gradle plugin describes every script in a registered `sourcesDir`, and a
helper module with no `NodeSpec` is not a node.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# support/ -> test_graph/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "skill-scripts" / "install-skt.sh"
SESSION_START_HOOK = REPO_ROOT / "hooks" / "skt-session-start.sh"
POST_TOOL_HOOK = REPO_ROOT / "hooks" / "skt-post-tool.sh"

# The interpreter matrix. `pyproject.toml` says `requires-python = ">=3.11"`;
# these are the versions that turn the claim into a measurement, and they are
# deliberately the same pair `.github/workflows/ci.yml` runs pytest on. Bare
# `python3` is NOT among them on purpose: it is 3.14 on the machine this was
# authored on and 3.12 on a hosted runner, so an unpinned probe measures
# whatever the box happens to carry.
DEFAULT_PYTHONS = ("3.11", "3.13")


def pinned_versions() -> list[str]:
    raw = os.environ.get("SKT_TG_PYTHONS", "")
    if raw.strip():
        return [v.strip() for v in raw.split(",") if v.strip()]
    return list(DEFAULT_PYTHONS)


class ToolMissing(RuntimeError):
    """A precondition this node cannot supply for itself."""


def uv_bin() -> str:
    found = shutil.which(os.environ.get("SKT_TG_UV", "uv"))
    if not found:
        raise ToolMissing(
            "uv is not on PATH; the interpreter matrix is provisioned with "
            "`uv python find` / `uv python install`"
        )
    return found


def resolve_interpreter(version: str) -> str:
    """Absolute path to CPython `version`, downloading it if uv must.

    `uv python find` does not install, so a runner with no 3.11 answers
    with a failure rather than a path — hence the install fallback. The
    ABSOLUTE path is what matters: install-skt.sh bakes it into the
    wrapper, which is what makes "this wrapper ran on 3.11" a fact about
    an interpreter and not about the PATH the node happened to inherit.
    """
    uv = uv_bin()
    probe = subprocess.run(
        [uv, "python", "find", version], capture_output=True, text=True, timeout=120
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return probe.stdout.strip()
    install = subprocess.run(
        [uv, "python", "install", version], capture_output=True, text=True, timeout=900
    )
    if install.returncode != 0:
        raise ToolMissing(
            f"no CPython {version} available and `uv python install {version}` failed: "
            f"{install.stderr.strip()[:400]}"
        )
    probe = subprocess.run(
        [uv, "python", "find", version], capture_output=True, text=True, timeout=120
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise ToolMissing(f"CPython {version} still unresolvable after install")
    return probe.stdout.strip()


# --------------------------------------------------------------- home fixtures


def unit_record(
    name: str,
    *,
    version: str = "1.0.0",
    kind: str = "SKILL",
    origin: str | None = None,
    git_hash: str | None = None,
    git_ref: str | None = None,
    errors: list[str] | None = None,
) -> dict:
    """One `installed/<name>.json` record, in skill-manager's own key case.

    `origin` + `gitHash` together are what `Unit.change_managed` reads, and
    change-managed is the property `skt check` selects on — so a fixture
    that omits them is a fixture in which `check` has nothing to do.
    """
    record: dict[str, object] = {"name": name, "version": version, "unitKind": kind}
    if origin is not None:
        record["origin"] = origin
    if git_hash is not None:
        record["gitHash"] = git_hash
    if git_ref is not None:
        record["gitRef"] = git_ref
    if errors is not None:
        record["errors"] = json.dumps(errors)
    return record


def build_home(
    home: Path,
    *,
    units: list[dict] | None = None,
    loaded: set[str] | None = None,
    unreadable: list[str] | None = None,
    plugins: list[str] | None = None,
    policy: str | None = None,
    drift: bool | None = None,
    cli_tools: dict[str, list[str]] | None = None,
) -> Path:
    """Materialize a skill-manager home the way skt reads one.

    `loaded` names the units that also get a `.projections.json` sidecar —
    that file's PRESENCE is skt's whole definition of "loaded".
    """
    home.mkdir(parents=True, exist_ok=True)
    installed = home / "installed"
    installed.mkdir(parents=True, exist_ok=True)
    for record in units or []:
        name = str(record["name"])
        (installed / f"{name}.json").write_text(json.dumps(record))
        if loaded and name in loaded:
            (installed / f"{name}.projections.json").write_text(json.dumps({"name": name}))
    for name in unreadable or []:
        (installed / f"{name}.json").write_text("{not json at all")
    for name in plugins or []:
        (home / "plugins" / name).mkdir(parents=True, exist_ok=True)
    if policy is not None:
        (home / "home.policy.toml").write_text(f'policy = "{policy}"\n')
    if drift is not None:
        (home / "home.drift.json").write_text(json.dumps({"acknowledged": not drift}))
    if cli_tools:
        lines = []
        for backend, tools in cli_tools.items():
            for tool in tools:
                lines.append(f"[{backend}.{tool}]\nversion = \"1.0.0\"\n")
        (home / "cli-lock.toml").write_text("\n".join(lines))
    return home


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120
    )


def init_repo(root: Path, *, branch: str = "main", integration: bool = False) -> Path:
    """A real git checkout — skt classifies checkouts with git plumbing.

    Committer identity is set LOCALLY rather than assumed: a hosted runner
    has no global one, and `git commit` failing there would look like an
    skt defect.
    """
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", branch, cwd=root)
    git("config", "user.email", "test-graph@skt.invalid", cwd=root)
    git("config", "user.name", "skt test graph", cwd=root)
    git("config", "commit.gpgsign", "false", cwd=root)
    (root / "README.md").write_text("fixture checkout\n")
    if integration:
        (root / "integration.toml").write_text('[integration]\nname = "fixture"\n')
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "fixture", cwd=root)
    return root


def child_env(**overrides: str | None) -> dict[str, str]:
    """os.environ with skt's home/tier switches cleared, then overridden.

    The node process itself runs inside a real skill-manager home (the
    checkout's own), and every one of these variables would otherwise leak
    into the fixture and decide the answer.
    """
    env = dict(os.environ)
    for key in (
        "SKILL_MANAGER_HOME",
        "SKT_ROOT_HOME",
        "SKT_PYTHON",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_SESSION_ID",
        "SKILL_MANAGER_BIN_DIR",
        "SKILL_MANAGER_CACHE_DIR",
        "SKILL_DIR",
    ):
        env.pop(key, None)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env
