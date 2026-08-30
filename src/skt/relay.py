"""Relay a failed child command's OWN words, instead of a fragment of them.

`skt` shells out — to the home's pinned `skill-manager`, and to
git-issue-workflow's `bootstrap-home.sh` — and when one of those fails the
only thing the reader has is what skt chooses to print. Measured
(skill-manager#264, second defect): `skt ticket new` printed the LAST line
of the child's output and nothing else, so a provisioning failure rendered
as

    error: home bootstrap failed (exit 1); worktree and branch rolled back
             against the operator's global home.

— a dangling fragment of a five-line message whose FIRST line carried the
diagnosis, and whose own `log:` line named a file with the whole story in
it. Reproducing the failure by hand was the only way to learn what went
wrong. The rollback was right; only the reporting hid it.

So the rule here is: **the child's words are evidence and are never
summarised away.** skt may ADD a conclusion above them; it may not
replace them with one.

## Why a tail is not a summary

`tail[-1]` and `text[-1500:]` are both "keep the end", and the end is the
wrong half. A shell `die` prints its diagnosis first and its consequences
after, so a tail keeps the consequence and drops the cause. When the
output is genuinely too long to print, this module keeps the HEAD and the
TAIL and says how many lines it dropped between them — an elision the
reader can see is an elision, not a truncation they cannot.

## Why the cause is hoisted rather than trusted to survive

A bounded relay can still drop the middle, and the substantive line is
often in the middle: a cross-home refusal is emitted by the shim under a
probe, deep inside a long bootstrap log. :func:`_hoist` therefore lifts a
recognised refusal — the shim's own text, with the two homes it names —
above the elision, so the one line the reader needs cannot be the line the
budget cuts.

This mirrors the shape `skt.artifacts` already uses for CLI refusals
(`reason` / `fix` / `detail`, one typed value per case a caller acts on
differently). It is a dataclass rather than an exception because these
failures are rendered where they happen, not raised across a boundary.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

#: `LauncherShims.HOME_MISMATCH_EXIT_CODE`. A `bin/cli` shim binds the home
#: it LIVES in, so when `SKILL_MANAGER_HOME` names a different one it
#: refuses rather than silently editing the other — and exits 79
#: specifically so a caller can tell that refusal apart from every other
#: failure. 79 collides with nothing else skt runs.
HOME_MISMATCH_EXIT = 79

#: The shim's own first line. Matched on the TEXT as well as the exit code
#: because the exit code is frequently gone by the time skt sees the
#: output: `bootstrap-home.sh`'s capability probe runs the shim inside a
#: pipeline (`"$1" home clone --help 2>&1 | grep -q -- '--to'`), where the
#: function's status is grep's and 79 is discarded. The words survive that
#: pipeline whenever stderr is captured; the number does not.
_REFUSAL_SIGNATURE = "refusing to run against a home you did not name"

#: How much of a child's output is printed before it is elided. Generous:
#: a bootstrap failure is not on a hook path and nothing is waiting on it.
RELAY_BUDGET_CHARS = 6000

#: Lines kept at each end when the budget is exceeded. Both ends, because
#: a shell script's diagnosis is at the top and its verdict at the bottom.
RELAY_HEAD_LINES = 40
RELAY_TAIL_LINES = 20

#: And a bound on ONE line, since "lines" is not a bound on bytes.
RELAY_LINE_CHARS = 2000

#: `log:       /tmp/bootstrap-home-A3Uj8x.log` — bootstrap-home.sh writes
#: one and announces it on its first line. #264 asked for exactly this to
#: be surfaced; today it is inside the text the tail cut away.
_LOG_LINE = re.compile(r"^\s*log:\s+(\S+)\s*$", re.MULTILINE)

#: The two homes the refusal names, which are the whole of its content: the
#: one the environment asked for, and the one this shim actually serves.
#: Which of the two is wrong decides the remedy, so a caller that can tell
#: them apart can print a fix that works verbatim instead of a guess.
_NAMED_HOME = re.compile(r"^\s*you named:\s+(\S.*?)\s*$", re.MULTILINE)
_SHIM_HOME = re.compile(r"^\s*this shim would have edited:\s+(\S.*?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Relayed:
    """A failed child command, ready to print. Nothing here is a summary.

    `reason` is skt's own one-line conclusion; `cause` is the substantive
    lines lifted out of the child's output so a budget cannot drop them;
    `detail` is the child's output itself, whole or visibly elided; `log`
    is a file the child wrote that has more.
    """

    label: str
    exit_code: int
    reason: str
    fix: str = ""
    cause: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    log: str | None = None
    elided: int = 0
    refused: bool = False
    named_home: str | None = None
    shim_home: str | None = None


def combined(proc: subprocess.CompletedProcess) -> str:
    """The child's output in the order a terminal would have shown it.

    stdout and stderr are separately captured and separately buffered, so
    interleaving cannot be recovered exactly. Concatenating stdout-then-
    stderr at least keeps each stream's own ORDER, which
    `stdout[-1500:] + stderr[-1500:]` did not: that cuts each stream
    mid-line and joins the two halves without a newline between them.
    """
    return (proc.stdout or "") + (proc.stderr or "")


def _hoist(text: str) -> tuple[str, ...]:
    """The cross-home refusal, if the child's output carries one.

    The shim prints its headline unindented and every following line of
    the refusal indented, so the block is delimited by its own shape. The
    two home paths are inside it and are the entire point: the operator
    has to know WHICH home was named and WHICH one the shim serves before
    any remedy means anything.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _REFUSAL_SIGNATURE not in line.lower():
            continue
        block = [line.rstrip()]
        for follower in lines[index + 1:]:
            if follower.strip() and not follower.startswith((" ", "\t")):
                break
            if follower.strip():
                block.append(follower.rstrip())
        return tuple(block)
    return ()


def _clip(line: str) -> str:
    """One line, bounded. A child that writes a whole file on one line —
    a JSON blob, a base64 payload — must not be able to make a failure
    report unreadable, and "lines" is the wrong unit for that alone."""
    if len(line) <= RELAY_LINE_CHARS:
        return line
    return f"{line[:RELAY_LINE_CHARS]} ... [{len(line) - RELAY_LINE_CHARS} more characters]"


def _bounded(text: str) -> tuple[tuple[str, ...], int]:
    """(lines, elided). Head and tail, never tail alone. See the module docs."""
    lines = [_clip(line.rstrip()) for line in text.strip().splitlines()]
    if len(text) <= RELAY_BUDGET_CHARS or len(lines) <= RELAY_HEAD_LINES + RELAY_TAIL_LINES:
        return tuple(lines), 0
    dropped = len(lines) - RELAY_HEAD_LINES - RELAY_TAIL_LINES
    return (
        tuple(lines[:RELAY_HEAD_LINES])
        + (f"... {dropped} line(s) omitted from the middle ...",)
        + tuple(lines[-RELAY_TAIL_LINES:]),
        dropped,
    )


def _covered_by(detail: tuple[str, ...], cause: tuple[str, ...]) -> bool:
    """Would printing `detail` after `cause` repeat it and say nothing new?"""
    already = {line.strip() for line in cause}
    return all(not line.strip() or line.strip() in already for line in detail)


def relay(
    label: str,
    proc: subprocess.CompletedProcess,
    *,
    reason: str,
    fix: str = "",
    refusal_fix: str | Callable[[Relayed], str] = "",
) -> Relayed:
    """Turn a failed child into something printable that keeps its words.

    `refusal_fix` replaces `fix` for a cross-home refusal — the one case
    where skt's usual remedy is not merely unhelpful but wrong ("upgrade",
    for a CLI that is current). It may be a callable, because the remedy
    depends on WHICH of the two homes the refusal names is the mistaken
    one, and only the refusal text knows that: it is handed the part-built
    `Relayed` carrying both. Nothing else is inferred from the exit code —
    every other failure keeps the caller's reason and the child's whole
    output, which is the half the reader could not previously see.
    """
    text = combined(proc)
    cause = _hoist(text)
    refused = bool(cause) or proc.returncode == HOME_MISMATCH_EXIT
    if refused and not cause:
        cause = (
            f"the CLI refused with exit {HOME_MISMATCH_EXIT}: a bin/cli shim binds the "
            "home it lives in, and SKILL_MANAGER_HOME named a different one",
        )
    detail, elided = _bounded(text)
    if cause and _covered_by(detail, cause):
        # The child said nothing the hoist has not already printed, so
        # printing it twice is noise. The hoist is kept rather than the
        # raw block because it is the half that cannot be elided.
        detail, elided = (), 0
    named = _NAMED_HOME.search(text)
    shim = _SHIM_HOME.search(text)
    log_match = _LOG_LINE.search(text)
    relayed = Relayed(
        label=label,
        exit_code=proc.returncode,
        reason=(
            f"{reason} — the CLI refused a cross-home run, it is not out of date"
            if refused
            else reason
        ),
        fix=fix,
        cause=cause,
        detail=detail,
        log=log_match.group(1) if log_match else None,
        elided=elided,
        refused=refused,
        named_home=named.group(1) if named else None,
        shim_home=shim.group(1) if shim else None,
    )
    if refused and refusal_fix:
        resolved = refusal_fix(relayed) if callable(refusal_fix) else refusal_fix
        relayed = replace(relayed, fix=resolved or fix)
    return relayed


def render(relayed: Relayed) -> list[str]:
    """The lines to print, in the order wt-style refusals print them."""
    lines = [f"error: {relayed.reason}"]
    for index, line in enumerate(relayed.cause):
        lines.append(f"cause: {line}" if index == 0 else f"       {line}")
    if relayed.fix:
        lines.append(f"fix:   {relayed.fix}")
    if relayed.log:
        lines.append(f"log:   {relayed.log}")
    if relayed.detail:
        lines.append(f"--- {relayed.label} said ---")
        lines.extend(f"  {line}" if line else "" for line in relayed.detail)
    return lines


def emit(relayed: Relayed, *, stream=None) -> None:
    print("\n".join(render(relayed)), file=stream or sys.stdout)


def label_for(command: str | Path) -> str:
    """A short name for the thing that failed — its basename, not its path."""
    return Path(str(command)).name or str(command)
