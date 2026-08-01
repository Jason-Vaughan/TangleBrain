# Project Map

<!--
A "where things live" map: the structural table-of-contents the agent consults
FIRST before grepping or filesystem search. The top-level-directory skeleton is
auto-generated (seeded on toggle-on, refreshed by the project-map wrap-step);
fill in the descriptions. Distinct from FEATURES.md (#207), which maps features
to file paths — this maps the layout itself.
-->

## Structure

- `tanglebrain/` — the package. Routing core at the top level (`router.py`, `selector.py`,
  `classifier.py`, `roster.py`), entry points beside it (`cli.py`, `mcp_server.py`), and one
  subpackage per surface: `adapters/` (backend drivers — `cli`, `openai_compat`, `api`, over a
  shared `base`), `serve/` (the OpenAI-compatible local endpoint), `gui/` (the knob panel),
  `config/` (the packaged example roster).
- `plugins/` — the Claude Code plugin this repo's own marketplace publishes
  (`tanglebrain-delegate`). Declarative manifests only; it wires the console script rather than
  vendoring any code.
- `tests/` — the unittest suite (`make test`), roughly one file per module, plus meta-tests
  guarding things no runtime path exercises: `test_plugin_manifest.py` (manifest drift) and
  `test_packaging.py` (dependency-constraint invariants).
- `.claude-plugin/` — `marketplace.json`, which is what makes this repo a Claude Code plugin
  marketplace.
- `.github/` — CI (`ci.yml`: the suite on Python 3.10/3.11/3.12) and release automation
  (`publish.yml`: PyPI upload via OIDC trusted publishing, fired by a published GitHub release),
  plus issue and PR templates.
- `tanglebrain.egg-info/` — build detritus from an editable install. Gitignored; ignore it.

## Shared directories / doc groups

- **AI Inference** → _(no shared directory)_
  - `LITELLM`
  - `TANGLEBRAIN`
  - `TANGLEBRAIN-C1-REPORT`
  - `TANGLEBRAIN-PLAN`
- **Tangle-Shared** → `/Users/jasonvaughan/Documents/Projects/Shared/Tangle-Shared`
  - _(no docs registered)_
