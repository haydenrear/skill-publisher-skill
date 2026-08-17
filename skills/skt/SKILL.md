---
name: skt
description: 'Skill-lifecycle orientation and change management for skill-manager homes, via the `skt` CLI. Use at SESSION START to learn what is loaded and where you are standing — which skill-manager skills and plugins are installed, which support change management, which have a NEW VERSION AVAILABLE and how to pull/sync them, which home tier this session writes (root ~/.skill-manager, project <repo>/.skill-manager, or a ticket worktree home), and whether you are inside an epic, a ticket, or an active spec workflow. Use whenever you edited a skill in your home and need it to survive — sync it up a tier, publish it to its own repo — or see "please sync with root to publish changes globally". Use for the worktree ticket lifecycle: `skt ticket new/close` creates and tears down a ticket worktree WITH its own Skill Manager home, and `skt ticket list/sweep` enumerates the ticket worktrees of an epic and retires them in one safety-gated pass instead of a hand-rolled `git worktree remove --force` loop. Trigger on: "what skills are loaded", "am I in a ticket/epic", "update this skill", "new version", "sync skills", "publish my skill edit", "start/finish a ticket", "clean up/retire/sweep worktrees", "reclaim disk space from worktrees", session startup orientation.'
---

# skt

One CLI for the questions every session has and nobody used to answer:
*what is loaded, where am I standing, is anything stale, and how does my
skill edit survive this worktree?*

```bash
skt status            # startup report: units, plugins, home tier, epic/ticket state
skt check             # new-version-available, recorded unit errors, stale artifacts
skt sync <unit>       # pull a unit to its latest pushed source
skt ticket new <T>    # ticket worktree + its own Skill Manager home, one command
skt ticket close <T>  # teardown through the close-out gate
skt ticket list       # every ticket worktree here, and what blocks retiring it
skt ticket sweep      # retire many at once — dry run unless you pass --yes
skt publish [<unit>]  # a home-edited skill -> up one tier -> its own git repo
```

`skt` is on `PATH` in every skill-manager home (`<home>/bin/cli/skt`).
`skt --help` is authoritative for syntax.

## When `skt check` says a unit is NOT stale

A unit whose installed hash disagrees with its remote tip is usually
behind it. Sometimes the home already knows better: the installer
records `errors[*].kind` when it leaves a store in a state it could not
finish, and for `MERGE_CONFLICT`, `NO_GIT_REMOTE` and
`NEEDS_GIT_MIGRATION` that record *is* the explanation for the
disagreement. `skt check` reads it first and emits a `unit-error`
notification in place of the pull prompt:

```
deploy-helm is not stale — its store is mid-merge (MERGE_CONFLICT):
unmerged paths remain. Local work is preserved at stash@{0}.
Syncing re-runs the merge that made them.
  resolve with: git -C <home>/skills/deploy-helm status
```

**Do not answer this with `skt sync` or `skill-manager sync --merge`.**
`--merge` is documented as the flag that *sets* `MERGE_CONFLICT`, and
the state clears only when the store has no unmerged files left. The
stash the message names is somebody's uncommitted work and any reset of
the store destroys it. Resolve in the store directory, `git add` +
`git commit`, and the next command clears the error by itself.

## Startup disclosure

In Claude Code this plugin's `SessionStart` hook injects `skt status`
into every session automatically and performs the one bounded live
refresh of the check cache; the `PostToolUse` hook is cache-only — it
surfaces "new version available" notifications from that cached result
and never runs a check itself (every hook injection appends a line to
`<home>/logs/skt/hook.log`).
Harnesses without a hook runtime (codex, gemini) get the projected skt
skill plus an instruction snippet instead — the honest per-harness
matrix is `../../references/harness-capabilities.md`.

`skt ticket new/close` wraps git-issue-workflow's `wt`; the raw path
form still works everywhere and is the fallback when skt is not
installed:

```bash
"${SKILL_MANAGER_HOME:-$HOME/.skill-manager}/skills/git-issue-workflow/scripts/wt" new <TICKET>
```

## Retiring an epic's worktrees at the end: `list` and `sweep`

An epic keeps every ticket worktree standing until integration is done
and then retires them all at once. **Do not hand-loop
`git worktree remove --force`** — that is `rm -rf` with extra steps: it
deletes each worktree's Skill Manager home, which is gitignored, so the
loss appears in no diff, no PR and no fan-out.

```bash
skt ticket list                       # read-only: what is standing, and what blocks it
skt ticket sweep                      # the plan. Changes NOTHING without --yes
skt ticket sweep --epic <slug> --yes  # retire that epic's worktrees
```

**Pass `--epic <slug>` (or `--target <ref>`) whenever you arm it.**
Containment against the epic branch is the check that says a ticket
actually landed, and with no epic/target branch known it cannot be made
at all — so a bare `skt ticket sweep --yes` in a repository where no
single `epic/*` is discoverable **refuses every worktree** rather than
removing work that is pushed but merged nowhere. That is
git-epic-workflow's worktree-lifecycle §5 hard stop, enforced instead of
warned about.

Run it **from the primary checkout**. `sweep` refuses the worktree it is
running in and never touches the primary. `-y/--yes` is the **arm
switch** here — unlike `skt build -y`, which only suppresses a
confirmation prompt on a command that was going to act anyway.

The order per worktree is **inspect → home gate → inspect → remove**:
the gate takes up to three minutes, so the git answer that decides is
the one taken *after* it. Any one of these makes a worktree **skipped,
not removed** — reported, with the pass carrying on:

- uncommitted changes, or a stash entry made on that worktree's branch;
- commits not pushed, or not contained in the epic/target branch — or
  commits on a **detached HEAD**, where no ref outlives the worktree at
  all, which blocks even in a repository with no remote;
- no epic/target branch to check containment against (above);
- any probe that could not run — including `git remote` itself;
- a non-clean `skill-manager home close-out --home <worktree-home>
  --into <primary-home>` verdict — the same gate `skt ticket close` runs.

`--into` may not name a home inside a worktree the sweep could remove:
a gate whose `--home` and `--into` are the same home calls every unit
clean. That is refused.

Gitignored paths other than the home (`.env`, `.venv`, scratch) are
listed as a **warning**: `git worktree remove` deletes them without
`--force` and no diff will ever show it. They are not a blocker — most
ignored content is disposable and a gate that always fires gets turned
off.

Removal is `git worktree remove`; `--force` is never passed. Exit `0`
means every sweepable worktree was retired (a dry run is 0 too), `4`
means the armed pass completed but a gate **refused** at least one
worktree — the same code `skt ticket close` returns when the same gate
refuses — `1` means something failed, `9` means the destination home is
`frozen` and the pass was abandoned.

The summary reports a **free-space delta**, measured with `statvfs`
before and after, and no per-worktree size. These homes are cloned
copy-on-write, so `du` bills every shared block to every copy and
over-reports by roughly 30x — a home `du` called 1.1 GB cost 33.7 MB of
real space.

## The three-tier home model, in one table

| Tier | Path | Updated by | Your obligation |
| --- | --- | --- | --- |
| root | `~/.skill-manager` | operator installs; `skt sync` | publish local edits globally (`skt check` prompts here) |
| project | `<repo>/.skill-manager` | cloned from root; refreshed via `wt`/`skt ticket` imports | none — pull-side only |
| worktree | `<worktree>/.skill-manager` | cloned from project at `ticket new` | get edits OUT before teardown (`skt publish`; the close gate refuses otherwise) |

Homes are real copies, never symlinks. An edit inside one is **in no git
diff** — `skt publish` is how it survives: `home sync` moves it one tier
up (and no further), `unit publish` is the only route to the skill's own
repository and to other machines.

## Related skills in this plugin

- `unit-authoring` (sibling skill): authoring installable units —
  SKILL.md frontmatter, `skill-manager.toml`, `plugin.json`,
  dependencies, distribution. The deep schemas live in this plugin's
  `../../references/` pages.
- `skill-manager` (separate unit): install/bind/project/home plumbing;
  its CLI help is authoritative for those.
