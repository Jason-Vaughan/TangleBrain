# Roster Entry Guide

A roster entry should make the backend's ownership, endpoint, model, and failure behavior clear without exposing credentials. The local-first default works best when entries are explicit and users can see which backend will receive a request.

## Documenting an entry

For each entry, record whether the endpoint is local or remote, whether authentication is required, the expected model identifier, and any compatibility limitations. Use an environment-variable reference for secrets and keep the example value obviously fake. If the backend supports streaming, structured output, or tool calls differently from the common interface, note that next to the entry.

## Validation before use

Test a roster entry with a harmless prompt and verify that the selected backend appears in the measurement output. Then test an unavailable endpoint so the documented fallback behavior is confirmed. Do not enable a paid or authenticated backend merely to validate a configuration example.

## Sharing examples

A shareable roster snippet should omit personal hostnames, account identifiers, and API keys. Prefer a local endpoint or a placeholder domain, and link to the relevant provider documentation when a setting is provider-specific.
