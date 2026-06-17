---
name: Bug report
about: Report something that isn't working as documented
title: "[bug] "
labels: bug
---

## What happened

A clear description of the bug.

## Expected behavior

What you expected to happen instead.

## Steps to reproduce

1. …
2. …
3. …

## Environment

- TangleBrain version (`python -c "import tanglebrain; print(tanglebrain.__version__)"`):
- Python version (`python --version`):
- OS:
- Which path was involved (e.g. `--local`, the router, `tanglebrain-gui`, the delegate):

## Roster / config (redact secrets)

The relevant roster entry or settings, with any `key_ref` values and secrets **removed**. Never
paste a real key — `key_ref` is a reference (`env:NAME` / `file:PATH`), so share only that reference.

## Logs / output

Relevant error output or the failing `make test` snippet. Redact anything sensitive.
