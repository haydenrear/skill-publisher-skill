"""`skt build` — rebuild one derived artifact instead of all of them.

(The file is `build_cmd.py` and not `build.py`: `skt/__init__.py`
re-exports `skt.artifacts.build` as `skt.build`, the way
`git_issue_workflow` re-exports `wt_new`, and a sibling MODULE named
`build` would shadow that function on the package depending on import
order. The other five subcommand modules keep their verb names because
none of them collides with an exported symbol.)

The verb `skt check`'s `stale-artifact` notification names. It is a thin
framing over `skt.artifacts.build`, which is the only parser of
`skill-manager build --json`; what this module adds is skt's two habits —
short names, and wt-style refusals.

**Short names.** A notification has to be retypable, so it says
`skt build computeq` and not `skt build cli-shim:skill-script/computeq`.
`skill-manager build` accepts ids only, so a token with no `:` in it is
resolved through the home's own artifact list first, and an AMBIGUOUS one
is refused with both candidates rather than guessed at. That resolution
costs one extra CLI call and is skipped entirely when the caller already
holds ids.

**Refusals.** One error line, one fix line, and the exit code the
underlying command chose — including the two this command must not
flatten:

  0 with `not buildable here` rows — `build` names artifacts it cannot
    rebuild, with the command that can. That is not a failure of this run
    and exiting non-zero for it would make every printed remedy fail
    after doing its job correctly.
  1 with a complete report — a rebuild failed, or an attempted artifact
    is still stale afterwards. The report is printed either way, because
    it is the description of what happened.

`freshness_after` is a re-derivation from disk, not an inference from an
exit code, and it can legitimately read `unverifiable` after a successful
rebuild for a backend that records no install fingerprint (#120). This
renderer says so in those words rather than presenting it as a failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import artifacts as artifacts_mod


def _refuse(err: artifacts_mod.ArtifactError) -> int:
    print(f"error: {err.reason}", file=sys.stderr)
    if err.fix:
        print(f"fix:   {err.fix}", file=sys.stderr)
    if isinstance(err, artifacts_mod.UnknownArtifact) and err.candidates:
        print("       did you mean:", file=sys.stderr)
        for candidate in err.candidates:
            print(f"         {candidate}", file=sys.stderr)
    elif err.detail:
        print(err.detail[-800:], file=sys.stderr)
    return err.exit_code or 1


def render_text(result: artifacts_mod.BuildResult) -> str:
    lines = [f"skt build{' (dry run)' if result.dry_run else ''} — {result.home}"]
    if not result.steps:
        lines.append("  nothing to build — no artifact in this home is stale")
        return "\n".join(lines)
    not_buildable = []
    for step in result.steps:
        if step.action == "not-buildable":
            not_buildable.append(step)
            continue
        if step.action == "already-current":
            lines.append(f"  skipped     {step.id}")
            lines.append(f"      {step.reason}")
            continue
        mark = {
            "built": "built      ",
            "no-op": "no-op      ",
            "failed": "FAILED     ",
            "planned": "would build",
        }.get(step.outcome, "skipped    ")
        lines.append(f"  {mark} {step.id}")
        lines.append(f"      {step.reason}")
        if step.freshness_after is not None:
            note = f"      now: {step.freshness_after}"
            if not step.verifiable and step.freshness_after == "unverifiable":
                # Said in words: "unverifiable" after a successful build
                # reads like a failure and is not one.
                note += (" — the rebuild ran; this home records no install fingerprint "
                         "for that backend, so it cannot confirm what it produced")
            lines.append(note)
    if not_buildable:
        # Not a footnote. These are artifacts the caller asked about and
        # this command did not repair.
        lines.append("")
        lines.append("not rebuilt here — nothing in `skt build` produces these:")
        for step in not_buildable:
            lines.append(f"  {step.id}  ({step.freshness_before})")
            lines.append(f"      {step.reason}")
    lines.append("")
    verb = "to build" if result.dry_run else "built"
    count = result.selected - result.already_current - result.not_buildable
    lines.append(
        f"{result.selected} selected: {count if result.dry_run else result.rebuilt} {verb}, "
        f"{result.already_current} already current, {result.not_buildable} not buildable here"
    )
    if result.no_op:
        lines.append(
            f"{result.no_op} producer(s) ran and wrote nothing — the backend reported the "
            "dependency already satisfied from outside this home"
        )
    if result.failed:
        lines.append(f"{result.failed} rebuild(s) failed")
    if not result.dry_run and result.still_stale:
        lines.append(f"{result.still_stale} of the selected artifact(s) are still stale")
    return "\n".join(lines)


def run(
    ids: list[str] | None = None,
    *,
    stale_only: bool = False,
    all_artifacts: bool = False,
    dry_run: bool = False,
    force: bool = False,
    yes: bool = False,
    as_json: bool = False,
    start: str | Path = ".",
) -> int:
    ids = list(ids or [])
    try:
        home = artifacts_mod.resolve_home(start)
        resolved = artifacts_mod.resolve_ids(ids, home=home) if ids else ()
        result = artifacts_mod.build(
            resolved,
            home=home,
            stale_only=stale_only,
            all_artifacts=all_artifacts,
            dry_run=dry_run,
            force=force,
            yes=yes,
        )
    except artifacts_mod.ArtifactError as err:
        return _refuse(err)
    if as_json:
        print(json.dumps(
            {
                "home": result.home,
                "dry_run": result.dry_run,
                "exit_code": result.exit_code,
                "summary": {
                    "selected": result.selected,
                    "rebuilt": result.rebuilt,
                    "no_op": result.no_op,
                    "failed": result.failed,
                    "already_current": result.already_current,
                    "not_buildable": result.not_buildable,
                    "still_stale": result.still_stale,
                },
                "steps": [
                    {
                        "id": s.id,
                        "kind": s.kind,
                        "owner": s.owner,
                        "action": s.action,
                        "producer": s.producer,
                        "reason": s.reason,
                        "freshness_before": s.freshness_before,
                        "freshness_after": s.freshness_after,
                        "outcome": s.outcome,
                        "verifiable": s.verifiable,
                    }
                    for s in result.steps
                ],
            },
            indent=2,
        ))
    else:
        print(render_text(result))
    return result.exit_code
