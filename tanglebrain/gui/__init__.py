"""TangleBrain knob GUI — a thin, localhost-only web panel over the roster/pricing config.

View the roster, pricing, and the local spend-avoided rollup, and run a prompt through the router.
A focused set of knobs (pricing, and per-entry roster fields) is editable, writing config back to
YAML with validation, an atomic write, a backup, and comment-preserving edits.

Zero new runtime dependencies — the server is stdlib :mod:`http.server` and the page is a single
vanilla HTML/CSS/JS file. See :mod:`tanglebrain.gui.server` for the entry point and
:mod:`tanglebrain.gui.views` for the (testable, transport-free) view functions.
"""
