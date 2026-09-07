# Feature Index

<!--
Maintained automatically: the wrap-step handler appends
stubs when PRs touch new files. Fill in descriptions before
next wrap.

Format: - **Name** — short description. `file.js` plus stable anchors:
`file.js#symbolName` for a function/const, or a literal route string
for server routes. NO :line pointers — nothing re-verifies them, so
they rot.
-->

## UI / Web

- **Knob panel** — local web UI for inspecting the roster and firing one-off runs against a chosen
  backend. Static page in `tanglebrain/gui/static/index.html`; served by
  `tanglebrain/gui/server.py`, with request handling split into pure dispatch in
  `tanglebrain/gui/views.py`. `POST /api/*` requires `Content-Type: application/json`.

## Server / API

## Governance / Engines

- **Project map** — the "where things live" structural table-of-contents, consulted before grepping
  or filesystem search. `PROJECT-MAP.md`
- **Architecture overview** — canonical for system structure: what the router, adapters, classifier,
  delegate, measurement, GUI and serve endpoint are, and why each is shaped that way.
  `ARCHITECTURE.md`
- **Contributor guide** — dev setup, branch and PR conventions, test requirements, and where to file
  issues. `CONTRIBUTING.md`
- **Runtime architecture** — the runtime view of the same system: process topology and what breaks
  between processes. Defers to the root doc for structure rather than restating it.
  `docs/design/architecture.md`
- **API contract** — the four surfaces (HTTP, MCP, CLI, GUI) and what each promises, with the OWASP
  disposition table. `docs/design/api-contract.md`
- **Boundaries** — the contract surfaces that cannot be changed quietly, and which consumer breaks
  when they are. Most consumers live outside this repo. `docs/design/boundaries.md`
- **Data model** — no database: every entity, where it lives on disk, and whether it survives a
  crash, including the two-file measurement store. `docs/design/data-model.md`
- **Non-functional requirements** — performance, scalability, reliability, cost and compatibility
  targets, each stating how it is verified. `docs/design/nonfunctional-requirements.md`
- **Observability** — the append-only usage log: what it answers, what it cannot answer, and why a
  figure is priced once and never restated. `docs/design/observability.md`
- **Operations** — install, configure, enable a paid backend, diagnose a mis-route, and recover what
  can be recovered. `docs/design/operations.md`
- **Security model** — what is protected, from whom, and what each control does and does not buy;
  owns the Known-gaps accounting for the no-persistence guarantee. `docs/design/security-model.md`
- **Deprecation policy** — what each surface promises to keep stable, how a break is announced, and
  what a dependency floor means for users on the older major. `docs/design/deprecation-policy.md`

## CLI / Tooling

- **Packaged example roster** — the fallback roster shipped inside the package,
  `tanglebrain/config/roster.yaml`. It is a starting point, not the live config: resolution is
  `$TANGLEBRAIN_ROSTER` → `~/.config/tanglebrain/roster.yaml` → this file, so a real roster lives
  outside the repo and survives `git pull`. Loaded by `tanglebrain/roster.py`.
