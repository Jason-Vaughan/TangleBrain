"""OpenAI-compatible server mode — expose the router as a local LLM endpoint.

``tanglebrain-serve`` fronts the same routing path the CLI uses (``cli.run_once``) with a
``POST /v1/chat/completions`` endpoint, so any OpenAI-compatible consumer can point its
``base_url`` at TangleBrain and pick a *routing strategy* instead of a model: ``model: "auto"``
engages the full router, a roster id pins that entry. The routing core is untouched — this
package is a translation layer only.
"""
