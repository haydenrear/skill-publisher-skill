#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "PyYAML>=6.0.2,<7",
# ]
# ///
"""Validate every installable unit this repository ships.

WHY THIS EXISTS
---------------
A skill whose SKILL.md frontmatter is not valid YAML does not fail loudly. It
DISAPPEARS. The parser that reads a unit gets an exception, the unit drops out
of the sync, and the damage surfaces somewhere else entirely — descendant
worktrees deadlocking on exit 6 while blaming links, which is what an unquoted
`: ` in one description cost during a previous epic (skill-dev-skill#4,
git-issue-workflow#16). Nothing in CI looked at the frontmatter, because there
was no CI.

So this is deliberately a PARSE, not a lint. Every check below is "the same
read a consumer performs, performed early":

  * SKILL.md frontmatter is delimited, is valid YAML, is a mapping, and carries
    a usable `name` and `description`. An unquoted `: ` inside an unfolded
    description raises here, one second after the commit that introduced it,
    instead of one epic later.
  * every unit manifest (skill-manager.toml, skill-manager-plugin.toml,
    harness.toml) is valid TOML and declares exactly the fields an installer
    reads: name, version, description.
  * the names a unit states about ITSELF agree. A SKILL.md and the
    skill-manager.toml beside it name the same unit; a plugin's
    .claude-plugin/plugin.json (read by the harness) and its
    skill-manager-plugin.toml (read by skill-manager) agree on name AND
    version, which is an invariant those files state in prose and nothing
    enforced.

It takes no arguments in the normal case and prints one line per unit checked,
so a green run is evidence about a set of files rather than an exit code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml

# Directories that never contain a unit this repository ships: VCS metadata,
# build output, virtualenvs, and — importantly — a checkout's own Skill Manager
# home and agent projections, which hold COPIES of other repositories' units.
# Validating those would report another repo's defects as this one's.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".skill-manager",
    ".claude",
    ".codex",
    ".gemini",
    ".ruff_cache",
    ".tools",
    ".idea",
    "build",
    "dist",
    "target",
}

# The unit tables an installer looks for. `doc-repo` is spelled with a hyphen in
# the manifests that ship today; accept the underscore spelling too rather than
# passing a file that meant to declare a unit and misspelled the table.
UNIT_TABLES = ("skill", "plugin", "doc-repo", "doc_repo", "harness")
REQUIRED_FIELDS = ("name", "version", "description")

# Unit names address a directory (`$SKILL_MANAGER_HOME/skills/<name>`), so they
# have to survive being one.
NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

FRONTMATTER_FENCE = "---"


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.checked: list[str] = []

    def fail(self, path: Path, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def ok(self, path: Path, message: str) -> None:
        self.checked.append(f"{path}: {message}")


def iter_files(root: Path, name: str) -> list[Path]:
    found: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except (PermissionError, FileNotFoundError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRS:
                    continue
                stack.append(entry)
            elif entry.name == name:
                found.append(entry)
    return sorted(found)


def split_frontmatter(text: str) -> str | None:
    """Return the frontmatter block, or None when the file is not delimited."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_FENCE:
            return "\n".join(lines[1:i])
    return None


def check_text_field(report: Report, path: Path, data: dict[str, Any], field: str,
                     where: str, check_name: bool = False) -> str | None:
    value = data.get(field)
    if value is None:
        report.fail(path, f"{where} declares no `{field}`")
        return None
    if not isinstance(value, str):
        # This is the OTHER half of the unquoted-colon failure: when the stray
        # `: ` lands somewhere YAML can absorb, the description parses into a
        # dict instead of raising, and the unit installs with a description no
        # agent can read.
        report.fail(
            path,
            f"{where} `{field}` parsed as {type(value).__name__}, not a string "
            f"— an unquoted `: ` or a stray `#` will do this",
        )
        return None
    if not value.strip():
        report.fail(path, f"{where} `{field}` is empty")
        return None
    if check_name and not NAME_RE.match(value):
        report.fail(path, f"{where} `{field}` is not a usable unit name: {value!r}")
        return None
    return value


def check_skill_md(report: Report, path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    block = split_frontmatter(text)
    if block is None:
        report.fail(
            path,
            "no YAML frontmatter: the file must open with a `---` line and close "
            "the block with another `---` line",
        )
        return None
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        detail = str(exc).replace("\n", " ")
        report.fail(path, f"frontmatter is not valid YAML — the unit will be DROPPED by a sync: {detail}")
        return None
    if not isinstance(data, dict):
        report.fail(path, f"frontmatter parsed as {type(data).__name__}, not a mapping")
        return None

    name = check_text_field(report, path, data, "name", "frontmatter", check_name=True)
    check_text_field(report, path, data, "description", "frontmatter")
    return name


def check_unit_toml(report: Report, path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        report.fail(path, f"not valid TOML: {exc}")
        return {}
    except UnicodeDecodeError as exc:
        report.fail(path, f"not readable as UTF-8: {exc}")
        return {}

    tables = {k: v for k, v in data.items() if k in UNIT_TABLES and isinstance(v, dict)}
    if not tables:
        report.fail(
            path,
            "declares no unit table — expected one of "
            + ", ".join(f"[{t}]" for t in UNIT_TABLES),
        )
        return {}

    for table, body in tables.items():
        for field in REQUIRED_FIELDS:
            check_text_field(report, path, body, field, f"[{table}]", check_name=(field == "name"))
    return tables


def check_project_toml(report: Report, path: Path) -> None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        report.fail(path, f"not valid TOML: {exc}")
        return
    project = data.get("project")
    if not isinstance(project, dict):
        report.fail(path, "declares no [project] table")
        return
    check_text_field(report, path, project, "name", "[project]", check_name=True)
    report.ok(path, "skill project manifest parses")


def check_plugin_json(report: Report, path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.fail(path, f"not valid JSON: {exc}")
        return None
    if not isinstance(data, dict):
        report.fail(path, f"parsed as {type(data).__name__}, not an object")
        return None
    check_text_field(report, path, data, "name", "plugin.json", check_name=True)
    check_text_field(report, path, data, "version", "plugin.json")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=".",
        help="repository root to scan (default: the current directory)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = Report()

    skill_mds = iter_files(root, "SKILL.md")
    unit_tomls = (
        iter_files(root, "skill-manager.toml")
        + iter_files(root, "skill-manager-plugin.toml")
        + iter_files(root, "harness.toml")
    )
    project_tomls = iter_files(root, "skill-project.toml")
    plugin_jsons = [p for p in iter_files(root, "plugin.json") if p.parent.name == ".claude-plugin"]

    if not skill_mds and not unit_tomls and not plugin_jsons:
        print(f"error: no units found under {root} — this check would pass vacuously", file=sys.stderr)
        return 2

    frontmatter_names: dict[Path, str] = {}
    for path in skill_mds:
        name = check_skill_md(report, path)
        if name:
            frontmatter_names[path.parent] = name
            report.ok(path, f"frontmatter valid (name={name})")

    toml_tables: dict[Path, dict[str, dict[str, Any]]] = {}
    for path in unit_tomls:
        tables = check_unit_toml(report, path)
        toml_tables[path] = tables
        for table, body in tables.items():
            report.ok(path, f"[{table}] valid (name={body.get('name')!r})")

    json_data: dict[Path, dict[str, Any]] = {}
    for path in plugin_jsons:
        data = check_plugin_json(report, path)
        if data is not None:
            json_data[path] = data
            report.ok(path, f"valid (name={data.get('name')!r}, version={data.get('version')!r})")

    for path in project_tomls:
        check_project_toml(report, path)

    # --- the two agreements a unit states about itself -----------------------
    for path, tables in toml_tables.items():
        skill = tables.get("skill")
        if not skill:
            continue
        md_name = frontmatter_names.get(path.parent)
        if md_name and skill.get("name") and md_name != skill["name"]:
            report.fail(
                path,
                f"[skill].name is {skill['name']!r} but the SKILL.md beside it says "
                f"{md_name!r} — one unit cannot have two names",
            )

    for path, tables in toml_tables.items():
        plugin = tables.get("plugin")
        if not plugin:
            continue
        sidecar = path.parent / ".claude-plugin" / "plugin.json"
        data = json_data.get(sidecar)
        if data is None:
            report.fail(
                path,
                f"declares [plugin] but there is no {sidecar.relative_to(path.parent)} "
                "beside it — the harness reads that file",
            )
            continue
        for field in ("name", "version"):
            if plugin.get(field) != data.get(field):
                report.fail(
                    path,
                    f"[plugin].{field} is {plugin.get(field)!r} but "
                    f"{sidecar.name} says {data.get(field)!r} — skill-manager and the "
                    "harness would disagree about which unit this is",
                )

    for line in report.checked:
        print(f"ok    {Path(line.split(':', 1)[0]).relative_to(root)}: {line.split(':', 1)[1].strip()}")
    print(f"\nchecked {len(report.checked)} unit declaration(s) under {root}")

    if report.errors:
        print(f"\n{len(report.errors)} problem(s):", file=sys.stderr)
        for err in report.errors:
            print(f"  FAIL  {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
