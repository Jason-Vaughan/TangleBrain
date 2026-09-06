# TangleBrain — Design Documents

This directory holds the design reasoning behind TangleBrain: what the system promises, why it is
shaped the way it is, and where it is currently weak.

It is written for someone deciding whether to contribute, or about to change something and wondering
what they might break. It is deliberately candid — every document names its own gaps, and each named
gap links to a tracking issue rather than sitting as an unactionable admission.

## Where to start

| If you want to know… | Read |
|---|---|
| What the components are and how they fit together | [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) — canonical for system structure |
| What runs as a separate process, and what breaks between them | [`architecture.md`](architecture.md) |
| What TangleBrain promises to callers, and what it does not | [`api-contract.md`](api-contract.md) |
| What data exists, where it lives, what survives a crash | [`data-model.md`](data-model.md) |
| What is protected, from whom, and what is accepted risk | [`security-model.md`](security-model.md) |
| Which surfaces are contracts you cannot quietly change | [`boundaries.md`](boundaries.md) |
| What is measured, and what is invisible | [`observability.md`](observability.md) |
| Performance, reliability, cost, and compatibility targets | [`nonfunctional-requirements.md`](nonfunctional-requirements.md) |
| How it is installed, configured, and recovered | [`operations.md`](operations.md) |
| What is promised to stay stable, and how a break is announced | [`deprecation-policy.md`](deprecation-policy.md) |

## How these documents relate to the code

`ARCHITECTURE.md` at the repo root is canonical for **system structure** — what the router,
adapters, classifier, delegate, measurement, GUI, and serve endpoint are. These documents do not
restate it. They cover the things a component description does not: process boundaries, contracts
with consumers who cannot be migrated in lockstep, what happens when each dependency is unavailable,
and what is deliberately not built.

Where a document names a file and line, that reference was verified against the code at the time of
writing. If you find one that has drifted, that is a bug worth filing.

## Invariants

Several documents have an **Invariants** section. Those are not descriptions — they bind. Departing
from one is a decision to record and justify, not a doc to bring back into sync afterward. The two
with the highest consequence:

- **Nothing binds off-loopback.** The bind address is the entire authorization model for two
  unauthenticated, quota-spending surfaces. See [`security-model.md`](security-model.md).
- **A paid backend is never reachable without two independent gates, and is never preferred.**
  Both default false. See [`security-model.md`](security-model.md).

## Known gaps

Every gap these documents disclose that is **open work** has an issue. Weaknesses that are
ratified non-goals — no roster integrity check, permissions warned rather than enforced — are
stated in the documents that own them and are deliberately not tracked here; they are decisions,
not backlog.

| Gap | Issue |
|---|---|
| `usage.jsonl` grows without bound — the fold into `totals.json` exists, but nothing triggers it | [#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101) |
| A lost delegate hop is indistinguishable from a parentless record | [#123](https://github.com/Jason-Vaughan/TangleBrain/issues/123) |
| Capability routing ranks by cost only — it routes down, never up | [#97](https://github.com/Jason-Vaughan/TangleBrain/issues/97) |
| The GUI panel is audited for contrast only — keyboard, screen reader, motion and reflow are not | [#131](https://github.com/Jason-Vaughan/TangleBrain/issues/131) |

## Keeping these current

Changes to routing, adapters, or either HTTP surface should update the matching document in the same
PR. See [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md).
