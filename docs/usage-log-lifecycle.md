# Usage Log Lifecycle

Usage logs are useful for understanding routing, fallback, and cost-avoidance behavior, but they should not grow without bound or become an accidental cache of sensitive prompts.

A practical lifecycle policy separates operational measurements from request content, documents the retention window, and provides a bounded rotation or archival mechanism. When a write fails, the user-visible result should distinguish a failed measurement from a successfully routed task. If logs are copied for debugging, redact prompts, keys, hostnames, and account identifiers first.

The right retention period depends on the operator's needs and privacy obligations. The important invariant is that growth, deletion, and recovery behavior are explicit and testable rather than emergent from filesystem pressure.
