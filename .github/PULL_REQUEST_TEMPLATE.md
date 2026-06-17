<!--
Thanks for contributing to TangleBrain! Please fill in the three sections below.
See CONTRIBUTING.md for branch/PR conventions.
-->

## What

<!-- The change in one or two sentences. -->

## Why

<!-- The motivation / the problem this solves. Link issues: Fixes #N -->

## Test plan

<!-- How you verified it: `make test` output, manual steps, etc. -->

## Checklist

- [ ] `make test` passes (hermetic suite; HTTP is mocked).
- [ ] New behavior has tests; bug fixes have a regression test.
- [ ] **Docs updated in this PR** if behavior changed (`README.md` / `ARCHITECTURE.md`).
- [ ] `CHANGELOG.md` `[Unreleased]` updated (under the right subsection).
- [ ] No secrets committed — `key_ref` references only, never a raw key.
- [ ] Change is consistent with the opt-in / bring-your-own-key posture (`DISCLAIMER.md`).
