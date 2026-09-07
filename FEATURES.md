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

## CLI / Tooling

- **Packaged example roster** — the fallback roster shipped inside the package,
  `tanglebrain/config/roster.yaml`. It is a starting point, not the live config: resolution is
  `$TANGLEBRAIN_ROSTER` → `~/.config/tanglebrain/roster.yaml` → this file, so a real roster lives
  outside the repo and survives `git pull`. Loaded by `tanglebrain/roster.py`.

## TODO (auto-stubbed 2026-08-01)

- **TBD** — touched in this session: `PROJECT-MAP.md`. <!-- describe -->

## TODO (auto-stubbed 2026-08-21)

- **TBD** — touched in this session: `CONTRIBUTING.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/api-contract.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/architecture.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/boundaries.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/data-model.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/nonfunctional-requirements.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/observability.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/operations.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/security-model.md`. <!-- describe -->

## TODO (auto-stubbed 2026-09-06)

- **TBD** — touched in this session: `ARCHITECTURE.md`. <!-- describe -->
- **TBD** — touched in this session: `docs/design/deprecation-policy.md`. <!-- describe -->
