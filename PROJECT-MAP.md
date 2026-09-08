# Project Map

<!--
A "where things live" map: the structural table-of-contents the agent consults
FIRST before grepping or filesystem search. The top-level-directory skeleton is
auto-generated (seeded on toggle-on, refreshed by the project-map wrap-step);
fill in the descriptions. Distinct from FEATURES.md (#207), which maps features
to file paths — this maps the layout itself.

Keep every bullet on ONE physical line, however long. The wrap-step reads this
list line-by-line and drops the continuation lines of a wrapped bullet, which
severs the sentence mid-clause — it did exactly that to the two entries below
between #155 and #159.
-->

## Structure

- `docs/` — only `design/`: nine public design documents plus a README indexing them and tracking known gaps
- `plugins/` — the Claude Code plugin this repo's own marketplace publishes
- `tanglebrain/` — the package. `cli.py` is the primary surface; routing core beside it (`router.py`, `selector.py`, `classifier.py`), roster in `roster.py`/`roster_edit.py`, measurement in `measurement.py`/`totals.py`/`integrity.py`, gates in `settings.py`, shared write-and-rename in `atomic.py`, MCP delegate in `mcp_server.py`/`delegate.py`; subpackages `adapters/`, `gui/`, `serve/`, `config/`
- `tanglebrain.egg-info/` — build detritus from an editable install. Gitignored; ignore it.
- `tests/` — the unittest suite (`make test`), roughly one file per module, plus meta-tests

## Shared directories / doc groups

_This project belongs to 2 shared-doc groups. Membership is machine-local state, not project structure, so it is not published here — see the TangleClaw UI for this install's groups._
