# Project Map

<!--
A "where things live" map: the structural table-of-contents the agent consults
FIRST before grepping or filesystem search. The top-level-directory skeleton is
auto-generated (seeded on toggle-on, refreshed by the project-map wrap-step);
fill in the descriptions. Distinct from FEATURES.md (#207), which maps features
to file paths — this maps the layout itself.
-->

## Structure

- `docs/` — only `design/`: nine public design documents (architecture, api-contract, data-model,
- `plugins/` — the Claude Code plugin this repo's own marketplace publishes
- `tanglebrain/` — the package. Routing core at the top level (`router.py`, `selector.py`,
- `tanglebrain.egg-info/` — build detritus from an editable install. Gitignored; ignore it.
- `tests/` — the unittest suite (`make test`), roughly one file per module, plus meta-tests

## Shared directories / doc groups

_This project belongs to 2 shared-doc groups. Membership is machine-local state, not project structure, so it is not published here — see the TangleClaw UI for this install's groups._
