"""`skt sync <unit>` — pull a unit to its latest pushed source, loudly.

Thin wrapper over `skill-manager sync <unit> --git-latest` that closes
the documented trap: sync over an unpushed commit exits 0 and prints a
full success report while the store stays on the old gitHash. After the
underlying sync, the installed record is re-read and compared to the
remote tip; a mismatch is reported as a failure with the reason.

Which CLI runs is decided by `_cli` and always announced: the home's own
pin when it has one, and — at the ROOT tier only — a PATH
`skill-manager` when it does not.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import check as check_mod
from . import context as ctx_mod
from . import homes


FETCH_TIMEOUT_SECONDS = 20


def _refresh_tracking_refs(store: Path | None) -> bool | None:
    """Move the store's `refs/remotes/*` up to the remote it just synced from.

    The underlying sync advances the CHECKOUT without necessarily moving
    the remote-tracking ref, so `@{upstream}..HEAD` is left non-empty
    even though every one of those commits is already published — and
    the very next `skt check` reads that ref and calls it unpushed work.
    Measured: `skt sync debugging` reported "now at 91909afc (matches
    remote tip)" and the check immediately after it said "debugging
    modified locally (ahead)" with rev-list = 2; a bare `git fetch` took
    it to 0. So the command that fixes staleness was manufacturing the
    false report, and this is where it stops.

    Bounded and advisory: None when there is nothing to fetch, True on
    success, False when the fetch failed — the sync itself already
    succeeded and must not be turned into a failure by a slow network.
    """
    if store is None or not (store / ".git").exists():
        return None
    proc = check_mod._run_git(
        ["git", "-C", str(store), "fetch", "--quiet", "--no-tags"],
        float(FETCH_TIMEOUT_SECONDS),
    )
    return proc is not None and proc.returncode == 0

def _pin(home: Path) -> Path:
    """Where a home keeps its own CLI pin, present or not."""
    return home / "bin" / "cli" / "skill-manager"


def _foreign_pin_owner(candidate: Path, home: Path) -> Path | None:
    """The home a `<home>/bin/cli/skill-manager` pin belongs to, if not `home`.

    A pin derives its home from its OWN location, so running another
    home's pin would silently write that home. `<home>/bin/cli` is on
    PATH inside every launched session, so this is reachable rather than
    theoretical.
    """
    try:
        parts = candidate.resolve().parts
    except OSError:
        return None
    if len(parts) < 4 or parts[-3:] != ("bin", "cli", "skill-manager"):
        return None
    owner = Path(*parts[:-3])
    try:
        return None if owner.resolve() == home.resolve() else owner
    except OSError:
        return owner


def _cli(home: Path, tier: str) -> tuple[Path | None, str, str | None]:
    """(cli, provenance, error) — the CLI this sync should run, and why.

    A home's own pin is always preferred: it names the exact build the
    home was materialized by, which is the reason the pin exists at all.
    But refusing outright when there is no pin makes `skt sync`
    unavailable at the ROOT tier of any home that never ran
    `skill-manager home shims` — the tier that most needs it, and the one
    where a brew/PATH `skill-manager` IS the operator's CLI. So the
    fallback is allowed at ROOT only. Below root, falling through to some
    other CLI is exactly the failure the pin exists to remove, and the
    tier above is where a correct pin already lives.

    The provenance string is returned rather than logged here so the
    caller always says which CLI ran — an unpinned sync must never look
    like a pinned one.
    """
    pin = _pin(home)
    if pin.is_file():
        return pin, f"this home's pinned CLI at {pin}", None
    if tier != "root":
        return None, "", (
            f"skt sync: home CLI not found at {pin}\n"
            f"fix:   this {tier}-tier home has no pinned CLI — regenerate it with "
            f"`skill-manager home shims`, or run the sync from the tier above"
        )
    found = shutil.which("skill-manager")
    if not found:
        return None, "", (
            f"skt sync: home CLI not found at {pin}, and no `skill-manager` on PATH\n"
            f"fix:   install the CLI, or run `skill-manager home shims` to pin one "
            f"into {home}"
        )
    candidate = Path(found)
    foreign = _foreign_pin_owner(candidate, home)
    if foreign is not None:
        return None, "", (
            f"skt sync: home CLI not found at {pin}, and the `skill-manager` on PATH "
            f"({candidate}) is another home's pin — running it would write {foreign}\n"
            f"fix:   run `skill-manager home shims` to pin a CLI into {home}"
        )
    return (
        candidate,
        f"PATH skill-manager at {candidate} (root tier; this home has no pinned CLI)",
        None,
    )


def run(unit_name: str | None, *, start: str | Path = ".") -> int:
    home = homes.find_home(start)
    if home is None:
        print("skt sync: no skill-manager home found")
        return 1
    if not unit_name:
        print("skt sync: name a unit (see `skt check` for which are stale)")
        return 1
    units = {u.name: u for u in homes.read_units(home)}
    unit = units.get(unit_name)
    if unit is None:
        print(f"skt sync: no installed unit named {unit_name!r}")
        return 1
    if not unit.change_managed:
        print(f"skt sync: {unit_name} has no git provenance; nothing to sync from")
        return 1
    tier = ctx_mod.classify_tier(home, ctx_mod.checkout_root(start))
    cli, provenance, error = _cli(home, tier)
    if cli is None:
        print(error)
        return 1
    print(f"skt sync: using {provenance}")
    from .publish import _cli_env  # livelock guard: strip SKILL_MANAGER_CLI

    proc = subprocess.run(
        [str(cli), "sync", unit_name, "--git-latest"],
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    if proc.returncode != 0:
        print(f"skt sync: underlying sync failed (exit {proc.returncode})")
        print(proc.stdout[-2000:] + proc.stderr[-2000:])
        return proc.returncode
    after = {u.name: u for u in homes.read_units(home)}.get(unit_name)
    store = check_mod._store_dir(home, unit)
    refreshed = _refresh_tracking_refs(store)
    tip = check_mod._remote_tip_safe(unit.origin, unit.git_ref)
    if after and tip and after.git_hash == tip:
        print(f"skt sync: {unit_name} now at {tip[:8]} (matches remote tip)")
        if refreshed is False:
            print(
                f"skt sync: WARNING — could not refresh the remote-tracking refs in "
                f"{store}, so `skt check` may report this unit as locally ahead. "
                f"By hand: git -C {store} fetch"
            )
        return 0
    if after and tip:
        print(
            f"skt sync: WARNING — sync reported success but the store is at "
            f"{(after.git_hash or '?')[:8]} while the remote tip is {tip[:8]}. "
            f"This is the silent-no-op trap: the usual cause is an unpushed commit "
            f"in the unit's source repo. Push there, then re-run skt sync {unit_name}."
        )
        return 11
    print(f"skt sync: {unit_name} synced; remote tip unverifiable (offline?)")
    return 0
