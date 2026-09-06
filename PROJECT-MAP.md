# Project Map

<!--
A "where things live" map: the structural table-of-contents the agent consults
FIRST before grepping or filesystem search. The top-level-directory skeleton is
auto-generated (seeded on toggle-on, refreshed by the project-map wrap-step);
fill in the descriptions. Distinct from FEATURES.md (#207), which maps features
to file paths — this maps the layout itself.
-->

## Structure

- `docs/` — the published design documents (`docs/design/`), covering the architecture, data
  model, API contract, security model, boundaries, observability, operations, non-functional
  requirements and deprecation policy, with a README indexing them.
- `plugins/` — the Claude Code plugin this repo's own marketplace publishes
- `tanglebrain/` — the package. `cli.py` is the primary surface; the routing core sits beside
  it (`router.py`, `selector.py`, `classifier.py`) with the roster in `roster.py` /
  `roster_edit.py` and measurement in `measurement.py` + `totals.py`. `settings.py` holds the
  gates, including the paid-backend one. Subpackages: `adapters/` (backends), `gui/` (knob
  panel), `serve/` (HTTP), `config/` (packaged defaults). `mcp_server.py` and `delegate.py`
  are the MCP delegate surface, and `atomic.py` is the shared write-and-rename helper.
- `tanglebrain.egg-info/` — build detritus from an editable install. Gitignored; ignore it.
- `tests/` — the unittest suite (`make test`), roughly one file per module, plus meta-tests

## Shared directories / doc groups

_This project belongs to 2 shared-doc groups. Membership is machine-local state, not project structure, so it is not published here — see the TangleClaw UI for this install's groups._
