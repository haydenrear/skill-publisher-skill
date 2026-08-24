# Derived artifacts: what a home builds, what a clone inherits, and when to rebuild

**This is the one place this contract is stated.** `skill-manager`,
`git-issue-workflow` and `git-epic-workflow` link here rather than restate it —
a second copy is covered by nothing and drifts.

Read it when `skt status` or `skt check` mentions artifacts, when a command on
your `PATH` refuses with **exit 86**, or when you are about to conclude that
your worktree home is broken because a tool "is missing".

---

## 0. The short version

1. A **derived artifact** is anything a home *produced* rather than authored: a
   CLI entry point, a venv, a projection symlink, the unit store itself.
   `skill-manager artifacts list` names every one.
2. A clone **inherits** the artifacts its parent actually holds — they stay
   usable, on `PATH`, through links in the clone's own `bin/`. What the parent
   does not hold, the clone **declares**: recorded in the ledger, not built.
3. **Rebuild after *you* change the unit that owns the artifact. Not on
   arrival.** A fresh clone that has built nothing is a healthy clone.
4. The command is `skill-manager build <artifact-id>` (`skt build <id>` is the
   same thing), and whatever refused already printed the exact id to pass it.

Everything below is the reasoning, the exact output, and the two wrong answers
this mechanism has already produced in the field.

---

## 1. What a derived artifact is

An artifact is a thing in a home with a **producer** and **recorded inputs**.
Fetching a unit's git repo produces one; installing a CLI dep produces one;
projecting a skill into the agent's config dir produces one. Every artifact can
be thrown away and re-derived from the records it names — which is exactly why
a home may decline to build one until it is needed.

### The ledger

`<home>/artifacts.lock.toml` is the register of what a home is *entitled* to
hold. Its own generated header states the property that makes cloning cheap:

```toml
# skill-manager artifact ledger — every derived thing this home holds.
# Auto-managed by `skill-manager artifacts record`; rebuildable at any
# time from the records the `source` fields name. Identity only: no
# fingerprint, hash or timestamp of an artifact is copied here, and no
# absolute path is written, so a home clone carries this file unchanged.
```

**Identity only, and home-relative only.** The ledger never says what is
currently on disk; the two are compared at read time, every time. Deleting the
ledger loses no capability — it "is an optimisation and a memory of what USED
to exist, never a prerequisite", and `skill-manager artifacts record` rewrites
it. The reconciliation rule between the two, when they disagree, is: **the home
wins on facts, the ledger wins on existence.**

A row:

```toml
[[artifact]]
id = "provisioned-tree:venvs/jinja2-cli"
kind = "provisioned-tree"
owner = "spec-double-compiler"
inputs = ["unit:spec-double-compiler", "spec:pip:jinja2-cli[yaml]==0.8.2"]
outputs = ["venvs/jinja2-cli"]
source = "cli-lock.toml"
```

`id` is what you pass to `build`. `owner` is the unit that declared it.
`source` is the record it is re-derived from. Input references use a small set
of schemes — `unit:`, `store:`, `spec:`, `git:`, `binding:`, `record:` — and an
id never contains an absolute path, a timestamp or a content hash, because
"an id that changed when the bytes changed would make *this artifact is stale*
unsayable".

### The nine kinds, and what rebuilds each

**Only `cli-shim` has a per-artifact producer.** Everything else is *reported*
by `build` with the command that does rebuild it, and is never claimed to have
been built by `build`. This is the single most common misreading of a `build`
run, so it is the second row of the table:

| kind | what it is | rebuilt by |
| --- | --- | --- |
| `cli-shim` | a generated executable in `bin/cli/`, one per locked CLI dep | **`skill-manager build <id>`** |
| `unit-store` | a unit's bytes under `skills/<n>` or `plugins/<n>` | `skill-manager sync <unit>` |
| `provisioned-tree` | a machine-provisioned tree under `cache/`, `venvs/`, `tools/`, `npm/`, `pm/` | `sync`, or the shim that fronts it |
| `projection` | one agent-visible link/copy a binding produced | `skill-manager sync`, or `rebind <unit>` |
| `marketplace-entry` | one plugin row in the generated `plugin-marketplace/` tree | `skill-manager sync` |
| `harness-instance` | one instantiated harness sandbox | `harness instantiate` / `sync harness:<name>` |
| `mcp-registration` | one MCP server registered with the gateway | `skill-manager sync --include-mcp` |
| `doc-import` | a doc unit's managed import set | `skill-manager sync` |
| `unit-digest` | one unit's entry in `home.digest.json` | `skill-manager home drift --record` |

### Naming them

```bash
skill-manager artifacts list                  # every artifact, its state, its owner
skill-manager artifacts list --kind cli-shim  # or --owner <unit>
skill-manager artifacts show <id>             # one artifact: inputs, outputs, records
skill-manager artifacts stale                 # only the ones that no longer describe their inputs
skill-manager artifacts stale --unverifiable  # ... plus the ones nothing could decide
skt build / skill-manager build               # the rebuild side
```

`artifacts list` prints no header row. After the id come **materialization**,
**agreement**, **owner**:

| word | column | meaning |
| --- | --- | --- |
| `materialized` | materialization | every output is present and usable |
| `partial` | materialization | some outputs present, some not |
| `declared-only` | materialization | the ledger claims it; nothing usable at its path |
| `agrees` | agreement | the recorded input fingerprint still matches |
| `disagrees` | agreement | it does not |
| `unrecorded` | agreement | the home holds it, the ledger has no fingerprint row |
| `unverifiable` | agreement | there is nothing to compare against here |

**`materialization` is probed, never asserted.** Nothing writes "this is
built"; the command looks.

---

## 2. What a clone inherits, and what it merely declares

A ticket worktree home is a **clone** (`skill-manager home clone`, or `skt
ticket new`, or `wt new`, which run it for you). Cloning does not copy the
parent's provisioned trees and does not rebuild them. It skips `cache/`,
`tmp/`, `logs/`, `venvs/`, `tools/` and `npm/` outright — **but not `pm/`**,
which holds the pinned package managers and is copied, deliberately, because
a home that cannot run `uv` cannot build anything. It then does two different
things to two different sets.

> **Two different words, and they are not the same set.** The clone inherits
> what its **immediate source home's `bin/` actually held** — `bin/` is copied,
> so its entries travel. What a copied link is *allowed* to reach is decided
> separately, over the **whole descent chain**, which is why `parentStores` is
> a list. An entry point the source home never had is simply not there to
> inherit, no matter what a grandparent holds.

### Inherited — built, usable, and sanctioned

For an entry point the source home held as a link into a parent store, the
clone's own `bin/cli/<name>` is a symlink at that store's copy. `home verify`
names these out loud and passes them:

```
5 shim(s) in <clone>/.skill-manager link at its parent store — a child home
shares the parent's provisioned tools by design; the parent must outlive this home
    bin/cli/computeq -> /Users/…/.skill-manager
    bin/cli/helm-deploy -> /Users/…/.skill-manager
    bin/cli/monitoring -> /Users/…/.skill-manager
    … 2 more
descent: <clone>/.skill-manager records that it was cloned from <project>/.skill-manager;
1 of 1 recorded parent store(s) still re-derive as ancestors of this home
    /Users/…/.skill-manager  — re-derived, so its artifacts are shared by right
```

**"Shared by right"** is the operative phrase. A link into the parent store is
not a leak the clone got away with; it is the contract working, and `home
verify` exits 0 on it.

> **It is not PATH inheritance.** Launching through a home puts *that home's*
> `bin/cli`, `bin/mcp` and `bin/launch` in front, and actively **strips every
> foreign home's bin directory** out of the inherited `PATH`. You reach the
> parent's tool through *your* `bin/cli/<name>`, one entry at a time, on
> purpose. Nothing about a clone gives you a second home's whole toolbox.

### Declared — recorded, not built

For an artifact whose backing tree the clone does not carry, the clone writes
the ledger row and stops. Where there was an entry point, it writes a **cold
shim**: a real, executable file whose whole job is to refuse informatively.

```bash
$ jinja2 --version
skill-manager: 'jinja2' is declared in this home and has not been built.
  reason:  it links to ../../venvs/jinja2-cli/bin/jinja2, which this home does not have
  home:    /Users/…/wt-224-his-8/.skill-manager
  build it:  skill-manager build 'cli-shim:pip/jinja2-cli[yaml]'
        or:  skt build 'cli-shim:pip/jinja2-cli[yaml]'
  note:    that command exits 1 even when it built what you asked for (ARTI-06);
           re-run this entry point rather than trusting its status.
$ echo $?
86
```

**Exit 86 means "declared, not built". It does not mean broken.** It is not
127: the command *was* found. The whole message goes to stderr, stdout stays
empty, and the refusal hands you the id. Build it if you need the tool; ignore
it if you do not. (Quote ids containing `[` `]` — that is a shell glob.)

> **A declared artifact does not always have a cold shim.** A cold shim is
> written where the source home *had* an entry point whose backing tree the
> copy does not carry. If the source home never had one, the ledger still
> declares the artifact and `bin/cli/<name>` is simply **absent** — so you get
> plain `127 command not found`, not 86. Measured: in a worktree clone,
> `bin/cli/jinja2` was a cold shim (86) while `cli-shim:pip/pytest` was
> `declared-only` with no file at all (127). Both are the same lazy state; only
> one of them can tell you so.

`home clone` says how many of each it made:

```
declared:  N entry point(s) name `skill-manager build <id>` instead of failing in
           the kernel's words — they were shims into a tree this copy does not carry
deferred:  N virtualenv(s) inside units are declared, not copied — `uv` rebuilds
           each from the lockfile beside it on first use
```

### Why declare instead of build

Because building an artifact nobody changed is waste — wall-clock and disk, at
every `skt ticket new`, for tools most tickets never invoke, producing bytes
identical to what already exists one tier up. A worktree home exists so you can
edit a skill without fighting another ticket for it. It does not exist to
re-provision a toolchain that is already correct.

The behaviour is a policy flag with a default, and the home says so itself in
`<home>/home.policy.toml`:

```toml
# lazy_artifacts — whether this home DECLARES its derived
# artifacts and builds each on demand, instead of
# materializing all of them up front. Default: on for a
# project or worktree home, off for the operator root.
lazy_artifacts = true
```

`home clone --lazy-artifacts true|false` overrides it for one clone; the
decision is written into the copy's own policy file, so it is readable
afterwards rather than inferred.

### A declared-and-not-built artifact is a normal state, not a fault

Say it plainly, because a shipped bug got this wrong:

| command | what it calls a declared, unbuilt artifact |
| --- | --- |
| `artifacts list` | `declared-only` |
| `home verify` | **`DECLARED and not built`** — *reported, never counted* |
| `home repair` | **nothing. There is nothing to repair.** |
| `skt check` | **nothing.** It is filtered out before a notification exists. |

`home verify`'s own words:

```
N entry point(s) in <home> are DECLARED and not built — normal in a home with
`lazy_artifacts = true`, and not a failure. Each one names the command that
builds it when it is run.
```

Those lines **do not affect the exit code.** What does is the neighbouring,
genuinely different category: *unresolved* references — `"provisioning was
never completed, so the tools they name will fail at exec time"` — which comes
with the `build` command that completes it. Declared-not-built and unresolved
look similar on disk and mean opposite things; the partition asks
`lazy_artifacts` **and** whether the path is one of the ledger's declared
outputs, and requires both.

**The bug, so nobody rebuilds it.** `home repair` once reported a *fresh,
untouched, healthy clone as damaged* (`PRUNED_INHERITED_ENTRY bin/cli/tofu`).
The check read raw ledger outputs without asking `lazy_artifacts` or the
artifact's materialization, and reasoned that "declared, and absent, and the
parent has it" could not be a normal state. *A lazy home satisfies that
conjunction from birth.* Worse, the verdict depended on the operator's machine:
eight artifacts were `declared-only` in that clone and exactly one fired,
because a store further up the chain happened to hold that one binary while the
clone's own source home did not — the exact gap between the two words in the
box above. On a machine where more had been built, the same untouched clone
reports more.

The lesson generalises past that one bug. **Any rule that treats "declared and
absent" as evidence of damage fires on every healthy clone**, a different
number of times on every machine.

### The descent record is a pointer, not a grant

`<home>/home.provenance.json` records the chain a clone descends from:

```json
{
  "schemaVersion" : 1,
  "clonedFrom" : "/Users/…/skill-manager/.skill-manager",
  "clonedAt" : "2026-08-24T00:27:15.950595Z",
  "parentStores" : [ "/Users/…/.skill-manager" ]
}
```

**That file grants nothing.** `parentStores` is a snapshot kept for reporting
and repair and is *never consulted to grant a sanction*. Every reader
re-derives the chain live, hop by hop, and decides for itself; the record only
says where to look. A copy of a home whose recorded parents no longer re-derive
is not sanctioned by holding the file — `0 of 1 recorded parent store(s) still
re-derive`, and the shims that pointed there become leaks again. **Cloning is
not a laundering step.**

Two things to act on:

- **The parent must outlive the clone.** `home verify` says so. Deleting or
  moving a parent home turns every inherited artifact in every descendant into
  a dangling link. If you must, `skill-manager build` the ones you need first.
- **Do not hand-edit `home.provenance.json` to "fix" a sanction.** Nothing
  reads it as authority. You would change the pointer and not the answer.

---

## 3. When to rebuild

**After *you* change the unit that owns the artifact. Not on arrival.**

| situation | rebuild? |
| --- | --- |
| You just created the worktree and its home | **No.** Nothing has changed. Start working. |
| A tool refuses with exit 86 and you need that tool | Yes — build the id the refusal printed. |
| You edited a `skill-script` CLI dep in a unit you are working on | Yes. That artifact's inputs really did move. |
| You bumped a unit's declared tool version | Yes, for that artifact. |
| `skt check` names an artifact and prints `rebuild with: skt build …` | Yes. That set is already filtered to what is worth acting on (§4). |
| `skt status` says N stale but `0 rebuildable` | **No.** Those are declared-not-built. See §4. |
| You are about to run the ticket's test suite | No. Build what it refuses on, if it refuses. |

The rule is about **authorship, not arrival**. An artifact goes stale because
its inputs moved, and on a fresh clone nobody has moved anything. Rebuilding on
arrival converts a cheap clone into an expensive one and changes no answer.

---

## 4. The command, and how to read `artifacts stale`

```bash
skill-manager build                                # everything stale
skill-manager build --stale --dry-run --json       # what it would do, changing nothing
skill-manager build cli-shim:skill-script/skt      # one artifact + its STALE prerequisites
skill-manager build 'cli-shim:pip/jinja2-cli[yaml]' --force
skt build computeq                                 # skt resolves short names to ids
```

`--dry-run` first is free and names the producer for each target.

**Building a `cli-shim` also provisions the tree behind it.** The nine-kinds
table says `provisioned-tree` is not directly buildable, and that is true of
naming one on the command line — but the shim's producer *is* the installer
that creates its venv, so naming the shim gets you both. Measured end to end:

```
$ ./.skill-manager/bin/cli/jinja2 --version        # exit 86, cold shim
$ ls .skill-manager/venvs/                          # empty
$ skill-manager build 'cli-shim:pip/jinja2-cli[yaml]' --yes
  built       cli-shim:pip/jinja2-cli[yaml]
      now: current — its inputs still hash to the fingerprint recorded at install
  1 selected: 1 built, 0 already current, 0 not buildable here   # exit 0
$ ./.skill-manager/bin/cli/jinja2 --version        # jinja2-cli v0.8.2, exit 0
$ ls .skill-manager/venvs/                          # jinja2-cli
```

The `stale` count went `13 → 11` on that one build: the shim **and** its
`provisioned-tree:venvs/jinja2-cli` both became current.

### Exit codes, and one failure the remedy cannot fix

`build` exits **0** when it selected artifacts it cannot produce — that is a
report, not a failure. It exits **1** when a producer failed, *or when a target
is still stale after its producer ran*. Those are not the same event, and the
second is the one worth knowing about:

```
$ skill-manager build 'cli-shim:pip/pytest' --yes
  no-op       cli-shim:pip/pytest
      now: stale — its output bin/cli/pytest is not there
✗       the producer ran and this home still does not hold the artifact — it
        reported the dependency already satisfied from outside this home and
        wrote nothing. This is not a repair.
  1 of the selected artifact(s) are still stale                    # exit 1
```

**A `pip:` dependency whose binary already exists on the ambient `PATH` outside
the home can defeat its own producer.** The backend sees it satisfied, writes
nothing, and the home still has no entry point — `build` says so honestly and
exits 1 rather than claiming success. `--force` does not change it. If you meet
this, the tool is reachable on `PATH` anyway; the home just does not own it.
Do not read the exit 1 as "the artifact system is broken".

### Reading `artifacts stale` on a lazy home

This is the step the whole page exists for. Read the summary line, not just the
list. Measured on a **fresh, healthy, untouched** ticket-worktree clone:

```
$ skill-manager artifacts stale | tail -1
13 stale, 4 unverifiable, 22 current, of 39 artifact(s)

$ skt status | grep artifacts
artifacts  13 stale of 39 — 0 rebuildable, 13 declared-not-built, 4 unverifiable

$ skt check
skt check: all current (4 change-managed unit(s), tier worktree)
```

Read that last line precisely: **`all current` is counting the four
change-managed *units*, not the artifacts.** Artifacts reach `skt check` only
as separate `rebuild with:` notification lines, and here there were none to
raise. The two lines are not in conflict; they are answering different
questions.

**Thirteen stale, zero to act on.** That is not a contradiction and not a
tolerated inaccuracy. `stale` means *this artifact does not describe its
recorded inputs*, and an output that was never built genuinely does not — the
freshness fold is demote-only, and a missing output demotes any verdict to
stale, "whatever they hash to". But *stale* and *needs building now* are
different questions, and the vocabulary that separates them is:

| skt's word | which artifacts | act? |
| --- | --- | --- |
| **rebuildable** | stale, **present on disk**, and of a kind `build` produces | **Yes.** This is the real signal. |
| **declared-not-built** | `declared-only` — lazy, never materialized here | No. Normal from birth. |
| **unverifiable** | nothing in this home could decide them | No. Not "clean", but not actionable either. |

`skt check` only ever raises a notification from the **rebuildable** set, on
purpose: *"a declared-but-never-built artifact is the normal state of a lazily
provisioned home, and a notification that fires in the healthy case is one an
agent learns to ignore."* At most three, each with a `rebuild with:` line.

If you are reading `skill-manager artifacts stale` directly rather than through
skt, make the same split by the reason text:

| reason in the message | what it means | act? |
| --- | --- | --- |
| `its output … is not there` | never built here. Lazy. | Only if you need that tool. |
| `its output … is a link whose target this home does not hold` | inherited path, parent lacks it too. Lazy. | Only if you need that tool. |
| `its declared inputs moved: recorded <a>…, now <b>…` | **the inputs really changed** | **Yes.** |
| `it is built from <id>, which is stale` | propagated from upstream | Fix the upstream one. |

`artifacts show <id>` settles any individual case:

```
materialization: declared-only
agreement:       unverifiable
outputs:
  dangling   home     bin/cli/jinja2
actual:
  unusable_because = declared and not built — this home builds its artifacts
                     on demand, and the entry point names the command that builds this one
```

And note what `--unverifiable` exists for: **an undecided artifact is not a
current one.** `stale`'s summary keeps the three counts separate rather than
folding "could not decide" into "fine".

---

## 5. The two wrong answers, named

Both were reached by reading the source, by an agent working on this very
mechanism, and both were wrong. If your reasoning is arriving at either, stop.

### Wrong answer 1 — "the clone/verify sanction rule is the bug"

*The reasoning:* a clone holds links pointing into another home; `home verify`
exists to refuse cross-home references; therefore the sanction that lets these
through is the defect, and tightening it is the fix.

*Why it is wrong:* the sanction is not a hole in the rule, it **is** the rule.
`home verify` reports parent-store shims as sanctioned and exits 0 by design —
"a child home shares the parent's provisioned tools by design". Tightening it
repairs nothing; it reclassifies the healthy state as a failure and takes
working tools off your `PATH` to make a point about ownership.

The genuine invariant is narrower and already enforced. A parent-store link is
sanctioned only when **all three** hold: it is `bin/cli/<name>` or
`bin/mcp/<name>` exactly one segment deep; it resolves to the *same real entry*
in the other home; and that home re-derives, live, as an ancestor of this one
(§2). Anything else is still refused. Nothing needed loosening or tightening.

### Wrong answer 2 — "a clone should rebuild everything on arrival"

*The reasoning:* a home should own every byte it uses; inherited links are a
hidden coupling; therefore `home clone` should provision the full toolchain
locally, and inherited entry points should be cold refusals until it does.

*Why it is wrong:* it rebuilds what nobody changed. Every ticket worktree pays
full provisioning for tools most tickets never invoke, producing artifacts
byte-for-byte identical to the parent's. And the second half — making
*inherited* shims refuse — **removes working tools from the agent's `PATH`** to
express an ownership preference the agent cannot act on. A cold shim is for an
artifact this home has no copy of. An inherited one it can reach is not that.

The lazy contract is the correct one, and it is durable: a clone keeps its
inherited toolchain reachable and *declares* the rest; nothing prunes and
re-provisions an artifact nobody changed.

### The shape of both mistakes

Both start from "this state looks wrong" and neither asks **what does a healthy
clone look like?** The answer is on this page: links into a re-deriving parent,
`declared-only` rows in the ledger, cold shims at unbuilt paths, a non-zero
`stale` count with `0 rebuildable`, and `home verify` exit 0. If that is what
you are looking at, you are looking at a healthy home.

---

## 6. Known warts and boundaries

- **The cold shim's own ARTI-06 warning is broader than what it now does.** Every
  generated cold shim prints *"that command exits 1 even when it built what you
  asked for (ARTI-06); re-run this entry point rather than trusting its
  status."* Re-measured 2026-08-23: `build 'cli-shim:pip/jinja2-cli[yaml]'`
  succeeded and exited **0**. The exit 1 that *was* observed came from the
  genuinely-unbuilt case in §4, where exiting 1 is correct. The advice —
  **re-run the entry point rather than trusting the status** — is still the
  right habit and costs nothing; treat the warning as that habit, not as a
  claim that a successful build reports failure.
- **`unit-digest` rows are always `unverifiable`.** Verifying one is a full walk
  of the unit and the read does not do one. That is not a fault to chase.
- **The home's CLI pin** (`<home>/bin/cli/skill-manager`) is a *separate*
  mechanism from derived artifacts. Its current spelling has a known defect and
  **HIS-19 owns it**; do not infer the artifact contract from how the pin
  behaves today.
- **Which tier your session writes** — root, project, worktree — is `skt
  status`, and the tier table is in this skill's `SKILL.md`.
- **Getting a home edit out of a worktree** is `skt publish`, not `build`.
  Building an artifact changes nothing anyone else can see.
