"""`skt.artifacts` — the typed Python surface over this home's artifact graph.

`skill-manager artifacts list|stale` and `skill-manager build` speak three
versioned JSON documents (`ArtifactReport`, `StaleReport`, `BuildReport`),
and they were shaped as an API for exactly two consumers: this module and
the `skt` surface above it. This is the ONE place that parses them, so
`skt build`, `skt check` and `skt status` cannot come to disagree about the
same home through three parsing implementations.

The shape mirrors git-issue-workflow's `wt.py`, which `skt.ticket` already
imports rather than parsing stdout: frozen dataclasses, and a typed
exception hierarchy whose subclasses are exactly the cases a caller acts on
differently.

## Stdlib only

`src/skt/cli.py`'s docstring is the constraint: the skill-script installer
runs skt with the system `python3` and no venv, so nothing here may import
beyond the standard library.

## The CLI it runs, and the one it refuses to run

Always `<home>/bin/cli/skill-manager` — the home's own pin — and never a
bare `skill-manager` from `PATH`, which in a ticket worktree is whichever
home happened to export it. The environment comes from
`publish._cli_env()`, which strips `SKILL_MANAGER_CLI`: older pins are the
unguarded ``cli="${SKILL_MANAGER_CLI:-<abs>}"`` form and exec themselves
forever when a session exports one.

## Degrading against a CLI that predates the artifact graph

An operator's home holds whatever pin it was provisioned with, and the
`artifacts` and `build` verbs are newer than most of them. A pin without
them answers with picocli's unmatched-argument refusal, which this module
turns into :class:`ArtifactsUnsupported` carrying the CLI's own words —
never a traceback, never an empty list. An empty list would be a lie of
exactly the kind this epic exists to remove: "nothing is stale" and "I
could not ask" are different answers.

The same applies in the other direction. Every document is versioned and
the version is bumped when a consumer would have to change to keep reading
it, so a document from the FUTURE is refused rather than half-parsed. An
OLDER document is read as far as it goes: `StaleReport` schema 1 carries no
`materialization`, so those rows report it as ``unknown``, which is what it
is.

## Timeouts — reads are bounded, builds are not

Every READ is bounded, and the child runs in its own process group that is
SIGKILLed and reaped on the deadline — the same treatment `check._run_git`
gives git, for the same reason: the CLI spawns package managers and
installers that inherit the pipes and outlive a plain `Popen.kill()`.

That same group-kill is why :func:`build` is UNBOUNDED by default. Reaping
a hung read costs nothing; reaping a running install costs a half-written
tree where the artifact used to be, so the verb called to repair staleness
would leave the home worse than it found it. `_run` has no fallback
default: reads pass :data:`READ_TIMEOUT_SECONDS`, `build` passes
:data:`BUILD_TIMEOUT_SECONDS` (None), and neither can inherit the other's.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import homes
from .publish import _cli, _cli_env

#: Highest document version of each report this module knows how to read.
#: A higher one is refused (see the module docstring); a lower one is read.
LIST_SCHEMA = 1
STALE_SCHEMA = 2
BUILD_SCHEMA = 1

#: Default wall budget for one READ. The measured per-invocation CLI floor
#: is ~1.2 s (`evals/artifacts/baseline/cli_floor.json`), so this is
#: generous for a read and a hard stop for a hang. Callers on a shared
#: budget — `skt check`'s live path — pass their own remaining time.
READ_TIMEOUT_SECONDS = 20.0

#: And NOTHING for a build, deliberately.
#:
#: `_run` kills the whole process GROUP on a deadline, which is right for a
#: read (the CLI's children are helpers holding a pipe open) and is damage
#: on a write: a package install SIGKILLed partway through leaves a
#: half-installed tree behind, and the artifact this verb was called to
#: repair is then worse than it was. A read budget on a write operation is
#: how a remedy becomes the incident.
#:
#: An install has no defensible upper bound anyway — the wheelhouse this
#: repo's own `skill-script/tracing-observability` builds takes longer than
#: any number that could be written here. `build` is an explicit,
#: foreground, operator-initiated command; it is not on the hook path and
#: nothing behind it is waiting on a deadline. A caller that DOES have one
#: can still pass `timeout=`.
BUILD_TIMEOUT_SECONDS: float | None = None

#: Kept as the old spelling of the read budget: it was only ever reached by
#: reads, and naming it "default" is what let a write inherit it.
DEFAULT_TIMEOUT_SECONDS = READ_TIMEOUT_SECONDS

#: The one kind `skill-manager build` has a per-artifact producer for
#: (`ArtifactBuild`: "Exactly one kind is buildable here: CLI_SHIM").
#: Everything else is reported with the command that does rebuild it and is
#: never claimed to have been built — so `skt` must not offer `skt build`
#: as the remedy for an artifact `build` would only decline.
BUILDABLE_KINDS = frozenset({"cli-shim"})

#: Materializations that mean "the artifact IS on disk". A stale artifact
#: that was never built is a different sentence from one whose inputs moved
#: under it, and only the second is news.
PRESENT_MATERIALIZATIONS = frozenset({"materialized", "partial"})

#: picocli's refusals when a verb or option does not exist on this pin.
_UNSUPPORTED_MARKERS = (
    "unmatched argument",
    "unmatched arguments",
    "unknown subcommand",
    "unknown option",
    "unknown command",
    "usage: skill-manager",
)

_NOT_A_HOME = "is not a skill manager home"

#: Exit codes the CLI reserves. 2 is BOTH picocli usage AND
#: `NotAHomeException`, which is why the classification below reads stderr
#: rather than trusting the number alone.
_EXIT_NOT_A_HOME = 2
_POLICY_EXITS = frozenset({5, 6})
_EXIT_FROZEN = 9


# --------------------------------------------------------------- exceptions


class ArtifactError(RuntimeError):
    """A call into the artifact graph failed. One reason, one fix.

    `wt.py`'s shape: `reason` is what happened, `fix` is the command that
    resolves it, `exit_code` is what a CLI wrapping this should return.
    `detail` carries the CLI's own output when there is any, so a refusal
    can be shown without the caller re-running anything.
    """

    def __init__(self, reason: str, fix: str = "", exit_code: int = 1, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.fix = fix
        self.exit_code = exit_code
        self.detail = detail


class HomeNotFound(ArtifactError):
    """No skill-manager home, or a path that is not one."""


class CliUnavailable(ArtifactError):
    """The home holds no `bin/cli/skill-manager` pin to run."""


class ArtifactsUnsupported(ArtifactError):
    """This home's CLI cannot answer: it predates the verb, or postdates us.

    Raised for a pin with no `artifacts`/`build` verb AND for a document
    whose schema is newer than this module reads. Both mean "no answer was
    obtained", which is the thing a caller must not confuse with "nothing
    is stale".
    """


class ProbeTimeout(ArtifactError):
    """The call did not finish inside its budget; the group was killed."""


class UnknownArtifact(ArtifactError):
    """No artifact with that id — or a short name that names several.

    `candidates` carries what the home does hold, so a caller can print
    "did you mean" without a second call.
    """

    def __init__(self, reason: str, fix: str = "", exit_code: int = 2,
                 detail: str = "", candidates: tuple[str, ...] = ()):
        super().__init__(reason, fix=fix, exit_code=exit_code, detail=detail)
        self.candidates = tuple(candidates)


class BuildRefused(ArtifactError):
    """Refused before anything was built: a policy gate, or a frozen home."""


# --------------------------------------------------------------- data model


@dataclass(frozen=True)
class ArtifactOutput:
    """One path an artifact claims to have produced, and whether it is there."""

    path: str
    scope: str
    presence: str


def _short_name(artifact_id: str) -> str:
    """The last readable segment of an id: what an operator would type.

    `cli-shim:skill-script/computeq` -> `computeq`;
    `unit-store:deploy-helm` -> `deploy-helm`;
    `projection:<binding>#claude/skills/x` -> `x`.

    This is a DISPLAY name and is not unique by construction — resolution
    back to an id goes through :func:`resolve_ids`, which refuses an
    ambiguous one instead of picking.
    """
    key = artifact_id.split(":", 1)[1] if ":" in artifact_id else artifact_id
    return key.rsplit("/", 1)[-1] or key


@dataclass(frozen=True)
class Artifact:
    """One row of `artifacts list --json`.

    `recorded` and `actual` are the two halves of the epic's question — what
    a producer wrote down about its inputs, and what those inputs read as
    now — and they are kept apart here because they are apart there.
    """

    id: str
    kind: str
    owner: str | None
    materialization: str
    agreement: str
    origin: str
    inputs: tuple[str, ...] = ()
    observed_inputs: tuple[str, ...] = ()
    outputs: tuple[ArtifactOutput, ...] = ()
    source: str | None = None
    # Mappings, deliberately not frozen deeply: their keys are backend-defined
    # and a caller reads them, never mutates them.
    recorded: dict = field(default_factory=dict)
    actual: dict = field(default_factory=dict)

    @property
    def short_name(self) -> str:
        return _short_name(self.id)

    @property
    def buildable(self) -> bool:
        return self.kind in BUILDABLE_KINDS

    @property
    def present(self) -> bool:
        return self.materialization in PRESENT_MATERIALIZATIONS


@dataclass(frozen=True)
class StaleReason:
    """One verdict row of `artifacts stale --json`.

    `because` names the UPSTREAM artifacts that decided it, and is empty
    when the artifact's own inputs decided it. That distinction is the
    whole reason the field exists: "computeq moved" and "the unit computeq
    is built from moved" are different findings with different remedies.
    """

    id: str
    kind: str
    owner: str | None
    freshness: str
    materialization: str
    reason: str
    because: tuple[str, ...] = ()

    @property
    def short_name(self) -> str:
        return _short_name(self.id)

    @property
    def buildable(self) -> bool:
        return self.kind in BUILDABLE_KINDS

    @property
    def present(self) -> bool:
        return self.materialization in PRESENT_MATERIALIZATIONS

    @property
    def not_built(self) -> bool:
        """Declared and never materialized — normal in a lazily-built home."""
        return self.materialization == "declared-only"


@dataclass(frozen=True)
class StaleSurvey:
    """The whole of `artifacts stale --json`, counts included.

    Iterating yields the STALE rows, because that is what a caller almost
    always wants; `unverifiable` stays reachable beside it and is never
    folded into `current`. `StaleReport`'s own contract is that a consumer
    must be able to see the undecided set — a schema that reported "stale"
    and "everything else" would let "a missing input is unverifiable, never
    current" be violated in silence.
    """

    home: str
    total: int
    stale: tuple[StaleReason, ...] = ()
    unverifiable: tuple[StaleReason, ...] = ()
    current: int = 0
    stale_by_kind: dict = field(default_factory=dict)

    def __iter__(self):
        return iter(self.stale)

    def __len__(self) -> int:
        return len(self.stale)

    @property
    def rebuildable(self) -> tuple[StaleReason, ...]:
        """Stale, on disk, and something `skt build` can actually rebuild.

        The set a notification may name: an artifact that exists and no
        longer describes its inputs, with a one-command remedy that is real.
        """
        return tuple(r for r in self.stale if r.buildable and r.present)

    @property
    def not_built(self) -> tuple[StaleReason, ...]:
        """Declared but never materialized. Normal in a lazy home; not news."""
        return tuple(r for r in self.stale if r.not_built)


@dataclass(frozen=True)
class BuildStep:
    """One row of `build --json`.

    `freshness_after` is a MEASUREMENT re-derived from the home, not an
    inference from the exit code, and it is None on a dry run or when
    nothing ran. `verifiable` is False when the backend records no install
    fingerprint, so a successful rebuild can legitimately end
    `unverifiable` — that is the true answer, not a failure.
    """

    id: str
    kind: str
    owner: str | None
    action: str
    producer: str | None
    reason: str
    freshness_before: str | None = None
    materialization_before: str | None = None
    freshness_after: str | None = None
    materialization_after: str | None = None
    outcome: str = "skipped"
    verifiable: bool = False

    @property
    def short_name(self) -> str:
        return _short_name(self.id)

    @property
    def repaired(self) -> bool:
        """Attempted, and the home now holds something that is not stale."""
        return self.outcome in ("built", "no-op") and self.freshness_after not in (None, "stale")


@dataclass(frozen=True)
class BuildResult:
    """The whole of `build --json`, plus the exit code the CLI returned.

    `exit_code` is kept because it carries information the document does
    not: 0 with `not_buildable` rows means "named what it will not build,
    and that is not a failure of this run".
    """

    home: str
    dry_run: bool
    selected: int = 0
    rebuilt: int = 0
    no_op: int = 0
    failed: int = 0
    already_current: int = 0
    not_buildable: int = 0
    still_stale: int = 0
    steps: tuple[BuildStep, ...] = ()
    exit_code: int = 0

    def __iter__(self):
        return iter(self.steps)

    @property
    def ok(self) -> bool:
        """Nothing failed and nothing this run touched is still stale.

        Deliberately not "exit_code == 0": a `not-buildable` row leaves the
        exit code alone by design, and a caller asking "did my rebuild
        land" is asking about the rows that were attempted.
        """
        return self.failed == 0 and self.still_stale == 0


# ------------------------------------------------------------------ plumbing


def resolve_home(start: str | Path = ".") -> Path:
    home = homes.find_home(start)
    if home is None:
        raise HomeNotFound(
            "no skill-manager home found (checked $SKILL_MANAGER_HOME, ancestor "
            ".skill-manager dirs, and the operator root)",
            fix="cd into a checkout with a home, or export SKILL_MANAGER_HOME",
        )
    return home


def _cli_path(home: Path) -> Path:
    cli = _cli(home)
    if not cli.is_file():
        raise CliUnavailable(
            f"this home has no skill-manager CLI pin at {cli}",
            fix=f"skill-manager home shims --root {home}   # re-writes the pin",
        )
    return cli


def _run(home: Path, argv: list[str], timeout: float | None) -> subprocess.CompletedProcess:
    """One call into the home's pinned CLI, bounded when a budget is given.

    `subprocess.run(timeout=)` kills only the direct child, and this CLI
    spawns brew/npm/pip/uv children that inherit the pipes and keep the
    caller open past its budget — `check._run_git` carries the same note
    about git's helpers. Own session + killpg reaps the lot.

    `timeout=None` means UNBOUNDED, and there is no fallback default here:
    the same group-kill that correctly reaps a hung read would tear a
    half-finished install apart, so which calls carry a deadline is a
    decision each caller makes explicitly. Reads pass
    :data:`READ_TIMEOUT_SECONDS`; :func:`build` passes
    :data:`BUILD_TIMEOUT_SECONDS`, which is None.
    """
    cli = _cli_path(home)
    budget = None if timeout is None else float(timeout)
    if budget is not None and budget <= 0:
        raise ProbeTimeout(
            f"no time left in this pass to ask {cli.name} about artifacts",
            fix="skt check   # an explicit refresh has the whole budget",
        )
    proc = subprocess.Popen(
        [str(cli), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_cli_env(),
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:  # group gone or unkillable: fall back to the child
            proc.kill()
        try:
            proc.communicate(timeout=5)  # reap — SIGKILL cannot be blocked
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise ProbeTimeout(
            f"skill-manager {' '.join(argv)} did not finish inside {budget:.1f}s",
            fix=f"{cli} {' '.join(argv)}   # run it yourself to see why",
        ) from None
    return subprocess.CompletedProcess([str(cli), *argv], proc.returncode, out, err)


def _classify(proc: subprocess.CompletedProcess, argv: list[str]) -> ArtifactError:
    """Turn a CLI refusal into the typed error a caller can act on.

    Order matters. Exit 2 is BOTH picocli's usage error and
    `NotAHomeException`, and "did you mean:" is printed by `artifacts show`
    for a near-miss id as well as by picocli for an unknown verb — so the
    specific markers are tested before the generic ones.
    """
    err = (proc.stderr or "") + (proc.stdout or "")
    low = err.lower()
    verb = " ".join(argv)
    tail = err.strip()[-1200:]

    if "no artifact with id" in low:
        candidates = tuple(
            line.strip() for line in err.splitlines()
            if line.startswith("    ") and line.strip()
        )
        return UnknownArtifact(
            next((ln.strip() for ln in err.splitlines() if "no artifact with id" in ln.lower()),
                 "no artifact with that id in this home"),
            fix="skill-manager artifacts list   # the ids this home holds",
            detail=tail,
            candidates=candidates,
        )
    if _NOT_A_HOME in low.replace("-", " "):
        return HomeNotFound(
            "that path is not a Skill Manager home",
            fix="skt status   # names the home this session writes",
            exit_code=_EXIT_NOT_A_HOME,
            detail=tail,
        )
    # Exit code ONLY. `"frozen" in stderr` also matches every CPython
    # traceback, which contains `<frozen importlib._bootstrap>` — and it was
    # tested ahead of the unsupported-verb markers, so an interpreter error
    # would have been reported as a policy refusal.
    if proc.returncode == _EXIT_FROZEN:
        return BuildRefused(
            "this home's policy is `frozen` — a rebuild would rewrite what was frozen",
            fix="skill-manager home policy --live   # then re-run",
            exit_code=_EXIT_FROZEN,
            detail=tail,
        )
    if proc.returncode in _POLICY_EXITS:
        return BuildRefused(
            f"refused by this home's policy.install gate (exit {proc.returncode})",
            fix="skt build <artifact> --yes   # or relax policy.install",
            exit_code=proc.returncode,
            detail=tail,
        )
    if any(marker in low for marker in _UNSUPPORTED_MARKERS):
        return ArtifactsUnsupported(
            f"this home's skill-manager has no `{verb.split()[0]}` verb — "
            "the pin predates the artifact graph",
            fix="skt sync skill-manager   # then re-run",
            detail=tail,
        )
    return ArtifactError(
        f"skill-manager {verb} failed (exit {proc.returncode})",
        fix=f"run it yourself to see why: skill-manager {verb}",
        exit_code=proc.returncode or 1,
        detail=tail,
    )


def _decode(proc: subprocess.CompletedProcess, argv: list[str], supported: int) -> dict:
    """Parse the document, or raise the typed refusal.

    stdout is parsed BEFORE the exit code is consulted, on purpose:
    `build --json` exits 1 when a rebuild failed and still prints a
    complete, valid `BuildReport`. Reading the code first would throw away
    the only description of what happened.
    """
    text = (proc.stdout or "").strip()
    data = None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        raise _classify(proc, argv)
    schema = data.get("schema")
    if isinstance(schema, int) and schema > supported:
        raise ArtifactsUnsupported(
            f"skill-manager {' '.join(argv)} emits schema {schema}; this skt reads "
            f"up to {supported}",
            fix="skt sync skt   # this surface is the older half",
        )
    return data


# -------------------------------------------------------------------- surface


def list_artifacts(
    start: str | Path = ".",
    *,
    kind: str | None = None,
    owner: str | None = None,
    home: Path | None = None,
    timeout: float | None = None,
) -> tuple[Artifact, ...]:
    """Every artifact this home derived, typed.

    Raises :class:`ArtifactsUnsupported` against a CLI with no `artifacts`
    verb rather than returning `()`, because an empty tuple would read as
    "this home derived nothing".
    """
    home = home or resolve_home(start)
    argv = ["artifacts", "list", "--json"]
    if kind:
        argv += ["--kind", kind]
    if owner:
        argv += ["--owner", owner]
    budget = READ_TIMEOUT_SECONDS if timeout is None else timeout
    data = _decode(_run(home, argv, budget), argv, LIST_SCHEMA)
    return tuple(_artifact(row) for row in data.get("artifacts") or [])


def _artifact(row: dict) -> Artifact:
    return Artifact(
        id=row.get("id", ""),
        kind=row.get("kind", "?"),
        owner=row.get("owner"),
        materialization=row.get("materialization") or "unknown",
        agreement=row.get("agreement") or "unknown",
        origin=row.get("origin") or "unknown",
        inputs=tuple(row.get("inputs") or ()),
        observed_inputs=tuple(row.get("observed_inputs") or ()),
        outputs=tuple(
            ArtifactOutput(
                path=o.get("path", ""),
                scope=o.get("scope") or "unknown",
                presence=o.get("presence") or "unknown",
            )
            for o in row.get("outputs") or []
        ),
        source=row.get("source"),
        recorded=dict(row.get("recorded") or {}),
        actual=dict(row.get("actual") or {}),
    )


def stale(
    start: str | Path = ".",
    *,
    kind: str | None = None,
    home: Path | None = None,
    timeout: float | None = None,
) -> StaleSurvey:
    """What in this home no longer describes the inputs it was built from."""
    home = home or resolve_home(start)
    argv = ["artifacts", "stale", "--json"]
    if kind:
        argv += ["--kind", kind]
    budget = READ_TIMEOUT_SECONDS if timeout is None else timeout
    data = _decode(_run(home, argv, budget), argv, STALE_SCHEMA)
    summary = data.get("summary") or {}
    return StaleSurvey(
        home=data.get("home") or str(home),
        total=int(summary.get("artifacts") or 0),
        stale=tuple(_reason(r) for r in data.get("stale") or []),
        unverifiable=tuple(_reason(r) for r in data.get("unverifiable") or []),
        current=int(summary.get("current") or 0),
        stale_by_kind=dict(summary.get("stale_by_kind") or {}),
    )


def _reason(row: dict) -> StaleReason:
    return StaleReason(
        id=row.get("id", ""),
        kind=row.get("kind", "?"),
        owner=row.get("owner"),
        freshness=row.get("freshness") or "unverifiable",
        # Schema 1 carried no materialization. `unknown` is that document's
        # honest reading, not a guess at `materialized`.
        materialization=row.get("materialization") or "unknown",
        reason=row.get("reason") or "",
        because=tuple(row.get("because") or ()),
    )


def build(
    ids: tuple[str, ...] | list[str] = (),
    start: str | Path = ".",
    *,
    stale_only: bool = False,
    all_artifacts: bool = False,
    dry_run: bool = False,
    force: bool = False,
    yes: bool = False,
    home: Path | None = None,
    timeout: float | None = BUILD_TIMEOUT_SECONDS,
) -> BuildResult:
    """Rebuild named artifacts, everything stale, or everything buildable.

    With no ids and no flags this is `build --stale`, which is the CLI's own
    default. Ids are passed through verbatim — resolve a short name with
    :func:`resolve_ids` first if that is what you hold.

    UNBOUNDED by default, and that is the point: this runs a real install
    through a real backend, and `_run` kills the whole process group on a
    deadline. A `pip`/`brew`/wheelhouse build cut off partway leaves a
    half-installed tree where the artifact used to be — the remedy doing
    more damage than the staleness it was called to fix. Pass `timeout=` to
    take a bound deliberately.
    """
    home = home or resolve_home(start)
    argv = ["build"]
    if stale_only:
        argv.append("--stale")
    if all_artifacts:
        argv.append("--all")
    if force:
        argv.append("--force")
    if dry_run:
        argv.append("--dry-run")
    if yes:
        argv.append("--yes")
    argv.append("--json")
    argv += list(ids)
    # No fallback to a read budget here — see BUILD_TIMEOUT_SECONDS.
    proc = _run(home, argv, timeout)
    data = _decode(proc, argv, BUILD_SCHEMA)
    summary = data.get("summary") or {}
    return BuildResult(
        home=data.get("home") or str(home),
        dry_run=bool(data.get("dry_run")),
        selected=int(summary.get("selected") or 0),
        rebuilt=int(summary.get("rebuilt") or 0),
        no_op=int(summary.get("no_op") or 0),
        failed=int(summary.get("failed") or 0),
        already_current=int(summary.get("already_current") or 0),
        not_buildable=int(summary.get("not_buildable") or 0),
        still_stale=int(summary.get("still_stale") or 0),
        steps=tuple(_step(row) for row in data.get("steps") or []),
        exit_code=proc.returncode,
    )


def _step(row: dict) -> BuildStep:
    return BuildStep(
        id=row.get("id", ""),
        kind=row.get("kind", "?"),
        owner=row.get("owner"),
        action=row.get("action") or "?",
        producer=row.get("producer"),
        reason=row.get("reason") or "",
        freshness_before=row.get("freshness_before"),
        materialization_before=row.get("materialization_before"),
        freshness_after=row.get("freshness_after"),
        materialization_after=row.get("materialization_after"),
        outcome=row.get("outcome") or "skipped",
        verifiable=bool(row.get("verifiable")),
    )


def resolve_ids(
    tokens: tuple[str, ...] | list[str],
    start: str | Path = ".",
    *,
    home: Path | None = None,
    timeout: float | None = None,
    known: tuple[Artifact, ...] | None = None,
) -> tuple[str, ...]:
    """Turn what an operator typed into ids `build` will accept.

    A notification says `skt build computeq`, because that is what a person
    can retype; `skill-manager build` accepts full ids only. This bridges
    the two, and REFUSES a genuinely ambiguous short name rather than
    picking one — guessing which of two artifacts to rebuild is the kind of
    help nobody asked for. A name shared only across KINDS is not ambiguous
    for this verb: exactly one kind has a producer, so it resolves.

    A token that already contains `:` is an id and is passed through
    untouched, so the common machine path costs no extra CLI call.
    """
    tokens = list(tokens)
    if all(":" in token for token in tokens):
        return tuple(tokens)
    catalogue = known if known is not None else list_artifacts(
        start, home=home, timeout=timeout
    )
    by_id = {a.id: a for a in catalogue}
    out: list[str] = []
    for token in tokens:
        if token in by_id:
            out.append(token)
            continue
        matches = [a for a in catalogue if a.short_name == token]
        if not matches:
            matches = [a for a in catalogue if a.id.endswith("/" + token)
                       or a.id.endswith(":" + token)]
        if len(matches) == 1:
            out.append(matches[0].id)
            continue
        if not matches:
            near = tuple(a.id for a in catalogue if token in a.id)[:8]
            raise UnknownArtifact(
                f"no artifact named {token!r} in this home",
                fix="skt status --json   # or: skill-manager artifacts list",
                candidates=near,
            )
        # A unit's name is shared by its store row, its shim, and its
        # projection into every harness — measured, `tracing-observability`
        # names 18 artifacts in the operator's project home. That is not a
        # real ambiguity for THIS verb: exactly one kind has a producer, so
        # narrowing to the buildable candidates resolves it without
        # guessing. It matters because `skt check` prints `rebuild with:
        # skt build <short name>`, and a remedy that refuses when it is
        # retyped is worse than no remedy at all.
        buildable = [a for a in matches if a.buildable]
        if len(buildable) == 1:
            out.append(buildable[0].id)
            continue
        if buildable:
            ids = tuple(a.id for a in buildable)
            raise UnknownArtifact(
                f"{token!r} names {len(ids)} buildable artifacts in this home",
                fix=f"skt build {ids[0]}   # name the id you meant",
                candidates=ids[:8],
            )
        # Every candidate is a kind `build` has no producer for, so the fix
        # must NOT be `skt build <one of them>`: that command would refuse
        # in its turn. Same defect the ambiguity fix above removed, one
        # branch along.
        ids = tuple(a.id for a in matches)
        raise UnknownArtifact(
            f"{token!r} names {len(ids)} artifacts in this home, none of them buildable",
            fix=f"skill-manager artifacts show {ids[0]}   # names what does rebuild it",
            candidates=ids[:8],
        )
    return tuple(out)
