"""TangleBrain knob GUI (C5a) — a thin, localhost-only web panel over the §5 config.

Read-only this chunk: view the roster, pricing, and the local spend-avoided rollup, and run a
prompt through the router. Editable knobs (writing config back to YAML) are deferred to C5b.

Zero new runtime dependencies — the server is stdlib :mod:`http.server` and the page is a single
vanilla HTML/CSS/JS file. See :mod:`tanglebrain.gui.server` for the entry point and
:mod:`tanglebrain.gui.views` for the (testable, transport-free) view functions.
"""
