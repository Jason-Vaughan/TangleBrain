---
name: Add a backend / adapter
about: Propose or document support for a new routable backend
title: "[backend] "
labels: feature, backend
---

Adding a backend is usually a **config edit, not a code change** — a new roster entry, not new
Python. Use this template to propose a backend recipe, or to flag one that needs a new adapter.

## The backend

- Name / what it is:
- Tier: `local` / `sub` (authenticated CLI) / `api` (paid)
- Is it OpenAI-compatible (HTTP `/chat/completions`), a command-line tool, or something else?

## Can it be expressed as a roster entry?

The existing `invoke.kind` options are `openai-compat`, `cli`, and `api`. If your backend fits one
of these, please share a **worked roster entry** (redact secrets — `key_ref` is a reference, never a
raw key):

```yaml
# - id: your-backend
#   tier: local
#   invoke:
#     kind: openai-compat
#     base_url: "http://…/v1"
#     model: "…"
#   good_at: [...]
```

## If it needs a new adapter

If the backend can't be expressed with an existing `invoke.kind` (e.g. a non-OpenAI HTTP shape, a
non-CLI auth flow), describe the integration shape so we can agree on it before code is written. See
[ARCHITECTURE.md](../../ARCHITECTURE.md) for the adapter interface (`run(prompt, opts) -> text`).

## ToS / cost notes

For an authenticated CLI or paid backend, note any Terms-of-Service or cost considerations — see
[DISCLAIMER.md](../../DISCLAIMER.md).
