# Deprecation Policy

A deprecation policy has existed here in practice for some time. It had never been written down,
which meant it could not be relied on by anyone outside the repo and could not be applied
consistently by anyone inside it. This document states it.

It covers four consumer-facing surfaces: the **CLI flags**, the **MCP tool surface**, the
**usage-log record shape**, and the **two HTTP surfaces**. Each has consumers that cannot be
migrated in lockstep — a script, an orchestrator's tool cache, a log reader, a monitor — which is
what makes them contracts rather than implementation.

## The pre-1.0 problem, stated honestly

TangleBrain is `0.20.x`. Under semver, `0.y.z` carries no compatibility guarantee at all: any
release may break anything. That is the literal rule and it is not what this project does.

So there are two things to keep separate:

- **What semver permits here:** everything. A `0.x` project owes you nothing.
- **What TangleBrain commits to anyway:** the rules below.

These commitments are real but they are *policy*, not *semver*. They can be changed by amending this
document, and doing so is a decision that gets recorded. At 1.0 the commitments below become semver
guarantees and this distinction disappears.

The honest reason for stating it before 1.0 rather than after: its first real test was a dependency
floor decision ([#90](https://github.com/Jason-Vaughan/TangleBrain/issues/90), the mcp 2.x
migration), and a rule invented while such a decision is in flight is not a rule. Writing it first
is what made that an application of a policy rather than a justification for one.

## Per-surface stability

### CLI flags

**A flag that is removed becomes an accepted no-op, not an error.** This is already practice:
`--route` still parses and does nothing, because the frontier-first router it once selected is now
the default. Its help text says so.

*Why a no-op rather than an error:* the consumer of a CLI flag is usually a script or an alias that
someone wrote once and forgot. Erroring turns a silent no-op into a broken pipeline for a flag whose
behavior is now the default anyway — a failure that costs the user real time to diagnose and buys
nothing. The cost of the no-op is one argparse entry and one line of help text.

*The limit:* a no-op is honest only while the behavior it selected is what happens anyway. A flag
whose removal changes what the tool does must **fail loudly**, never silently do nothing — silently
ignoring `--local` would route work to a paid backend the user believed they had excluded. When
that case arises, the flag errors and the CHANGELOG says so.

Flag *behavior* may change within a release when the change is a correction: `--roster`'s help text
misstated the default resolution order and was corrected in place rather than deprecated, because
the documented behavior was never the real behavior.

### MCP tool surface

The delegate server exposes `delegate_local`, `delegate`, `delegate_targets` and `delegate_many`.

**Tool names and parameter names are stable; new parameters are optional.** An orchestrator caches
the tool description at server startup, so a renamed tool or a newly-required parameter breaks a
running session with no error the user can act on.

**A removed tool is not a no-op.** Unlike a CLI flag, a tool that accepts a call and does nothing
returns a plausible answer to a model that will act on it. Removal means the tool is absent from the
listing, which orchestrators handle.

### Usage-log record shape

**Fields are additive and optional. A field is never removed, renamed, or given a new meaning.**

This is the strongest commitment in this document, and the reason is that the log is append-only
history: records written months ago are read by today's code. A renamed field does not break a
reader, it silently changes a number the reader believes — the spend-avoided figure the product
exists to justify. A reader predating any field must stay correct, which is why `task_id`,
`parent_task_id`, `origin` and `failures` are all written only when present.

Consequence: a field that turns out to be wrong is superseded by a new one, not repaired in place.
The old one keeps its original meaning forever.

### HTTP surfaces

`tanglebrain-gui` and `tanglebrain-serve` bind loopback-only and are unauthenticated by design.
Their consumers are the operator's own browser and local tooling. Response shapes follow the same
additive rule as the usage log; routes are stable within a minor.

**One bounded exception, and it is deliberately narrow: `tanglebrain-gui`'s `/api/stats` may drop a
field its own panel does not render.** The rule exists because a reader that predates a field must
stay correct and a reader that outlives one must not silently read a different number — neither
risk exists here. This endpoint has exactly one consumer, the packaged `index.html`, which ships in
the same wheel as the code answering it, so the two cannot be at different versions on any install;
the payload is not history, it is recomputed from the store on every request; and nothing persists
it. What the exception buys is the thing the additive rule cannot: an endpoint that returns an
open-ended passthrough grows a *de facto* contract out of fields nobody chose to publish, and the
only way to give it one is to be able to say what it does **not** send.

**It does not extend to `tanglebrain-serve`**, whose consumers are arbitrary OpenAI clients this
project has never seen, nor to any other surface, nor to the record shape above. Widening it is a
ruling, not a precedent to follow — the carve-out rests on single-consumer, same-package,
not-persisted, and a surface losing any one of those loses the exception with it.

## How a break is announced

1. **`CHANGELOG.md` under `[Unreleased]`**, in the subsection matching user-visible impact. A
   breaking change carries the `BREAKING:` marker, which is what drives the release's major bump.
2. **The affected design document** is updated in the same PR, per `CONTRIBUTING.md`.
3. **A deprecated-but-present surface says so where the user meets it** — `--help` text for a flag,
   the tool description for an MCP tool.

**Pre-1.0, "announced as breaking" does not mean the `BREAKING:` marker.** That marker drives a
*major* bump, and from `0.20.x` a major bump is `1.0.0` — a claim about the project's maturity that
no single change earns. Under semver a `0.y.z` breaking change rides a **minor** bump, so a
breaking change here ships as `0.(y+1).0`, announced in prose under `### Changed` with what breaks
and what to do about it stated plainly. The marker is reserved for after 1.0, when it means what it
says. Announcement is a duty to the reader; the marker is a lever on the version number, and
conflating them would either understate the break or overstate the release.

**Horizon.** A surface announced as deprecated in release *N* is not removed before *N+2*, and never
inside a patch release. There is no time-based window: this is a tool people install and forget, and
a calendar deadline would expire against users who simply had no reason to upgrade.

## Dependency floors

**A Dependabot pull request can incur this rule, and its green check does not say so.**
`.github/dependabot.yml` sets `versioning-strategy: increase`, which raises the *lower* bound as
well as crossing the upper one — so an ordinary in-range bump narrows what a user may install.
`tests/test_packaging.py` asserts ceilings only and will not catch it. Whoever merges the bump is
the announcement's author; read the floor change, not just the check.

This is the clause that governed the `mcp >= 2` floor
([#90](https://github.com/Jason-Vaughan/TangleBrain/issues/90)) and the one most easily got wrong,
because **no TangleBrain code has to change for a user's install to break.**

**Raising a dependency's major floor is a breaking change, and is announced as one** — even though
the CLI, the tools and the record shape are all untouched. The user's experience is that
`pip install` resolves differently, or refuses.

Three rules:

1. **Every dependency carries an upper bound** (`tests/test_packaging.py` asserts it over
   `build-system.requires`, `project.dependencies` and every extra). An unbounded floor is how a
   major lands unannounced, which is the defect behind
   [#87](https://github.com/Jason-Vaughan/TangleBrain/issues/87) and
   [#92](https://github.com/Jason-Vaughan/TangleBrain/issues/92). A floor bump therefore raises a
   ceiling too: `>= 2, < 3`, never `>= 2`.

   **`build-system.requires` is covered by the ceiling rule and exempt from the announcement
   rule, and the two halves are separate on purpose.** It is covered because an unbounded build
   requirement lets a major land in an sdist build with no announcement and no commit to blame —
   the same defect, at a narrower blast radius. It is exempt from the announcement because this
   clause governs **what a user may install**, and a build requirement is resolved when building
   from an sdist, never by someone installing a wheel: raising it changes no user's install and
   strands nobody. Not a hypothetical distinction — it is why `setuptools` could be moved off a
   floor carrying a HIGH and a MODERATE advisory as an ordinary patch-tier change, while two
   *runtime* floor raises proposed in the same batch were declined for buying nothing.

2. **Users on the older major are not stranded — they are pinned.** TangleBrain does not carry
   compatibility shims for two majors of a dependency; that doubles a tested surface permanently to
   serve a window that closes on its own. What they get instead is that **every previous release
   stays on PyPI and keeps working.** A user who needs the old major installs the last TangleBrain
   release that allowed it. The CHANGELOG entry names that release explicitly, so the instruction is
   in the record rather than in an issue thread.

3. **The blast radius is stated in the announcement.** A floor raised on a core dependency
   (`httpx`, `PyYAML`) affects every user. A floor raised inside an optional extra affects only
   users of that extra — `[delegate]` is opt-in, so its floor is a smaller decision than its
   diff suggests. Say which it is; the two deserve different scrutiny.

**Dependency *surface* is a separate question from dependency *version*, and it is not covered by
this policy.** Adopting a new major that arrives with materially more transitive dependencies is a
decision against the security model's stated stance — the dependency surface is kept small on
purpose, because every dependency avoided is one that cannot be compromised. That trade-off is
weighed on the issue that makes it, not pre-authorized here. This policy governs *how a floor is
announced*, not *whether the new major is worth adopting*.

## Amending this policy

Changing a rule above is a decision, recorded in the CHANGELOG with its reasoning, not a silent
edit. Practice that diverges from this document is a bug in one or the other — say which.
