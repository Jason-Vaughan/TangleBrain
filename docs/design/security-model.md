# Security Model

TangleBrain is a single-operator, local-first tool that handles third-party credentials and can
spend real money. This document names its weaknesses plainly rather than diplomatically — a security
document that only lists strengths is not useful to anyone deciding whether to trust it.

Every gap below has a tracking issue.

## Invariants

The two highest-consequence rules in the project. These bind.

- **Nothing binds off-loopback.** Both `tanglebrain-gui` and `tanglebrain-serve` bind `127.0.0.1`
  and only `127.0.0.1`. Not a default — a prohibition.

  *Why:* both surfaces are unauthenticated and both spend real backend quota, so the bind address
  **is** the entire authorization model. There is deliberately no auth story because there is
  deliberately no exposure; adding a token would imply the endpoint is safe to expose, which inverts
  the intent. Binding elsewhere does not weaken the posture, it **voids** it — the result is an
  unauthenticated endpoint spending money for whoever reaches it.

  > **Enforced.** `tests/test_bind_address.py` asserts the address handed to the server on both
  > surfaces, and that no flag can configure it — widening the literal, binding every interface, or
  > adding a `--host` flag each fail the suite.

- **A loose credential file warns; it never refuses.** `key_ref: file:PATH` resolution stats the
  file and warns on stderr when it is group- or world-readable. Warning rather than failing is the
  rule: refusing would break a working setup over a condition the operator may have accepted, and a
  credential check that stops the tool gets removed rather than heeded. The notice fires once per
  file per process — a per-call warning on a value resolved every request trains the operator to
  ignore it. POSIX only; Windows mode-bit semantics do not map onto these bits, so the check is an
  explicit no-op there rather than a guess. *Retroactive: yes — applies wherever a credential file
  is read.* Flipping this to a hard failure is a breaking change and needs its own ruling.

- **A paid backend is never reachable without two independent gates, and is never preferred.**
  `settings.api_billing_enabled` **and** the entry's own `enabled`, both defaulting false, both
  strictly bool-validated. `api` is never auto-selected by capability.

  *Why:* the threat here is accident far more than malice — a config edit that silently starts
  billing a real account. Two independent switches means one mistake is never sufficient, and strict
  bool validation means a stray `"yes"` or `1` cannot coincidentally enable spend. The
  never-preferred half is separate and equally load-bearing: a routing heuristic that decided a paid
  backend "looked like a good fit" would spend money on a judgment call nobody made.

  Enforced by `tests/test_selector.py` and `tests/test_settings.py`.

## What is actually being protected

Three things, in order of what a compromise would cost:

1. **Third-party credentials.** API keys and OAuth-authenticated CLI sessions. A leak means someone
   else's billed account, and TangleBrain would be the vector.
2. **Money.** A `tier: api` entry bills a real account. The threat is not only theft — it is
   *accident*: a config edit that silently starts spending.
3. **Backend quota.** The local surfaces can spend quota without authentication. Cheaper than the
   first two, easier to trigger than either.

Prompt and response content is explicitly **not** in this list. It is never persisted
(`measurement.py:351-370` builds the record field by field and carries no prompt or response body),
so there is no at-rest exposure to defend.

## Trust boundaries

| Boundary | Trusted side | Untrusted side | Control |
|---|---|---|---|
| Loopback bind | the local machine | the network | GUI and serve endpoint bind `127.0.0.1` only |
| Browser → local server | the operator's intent | any page in the operator's browser | `Content-Type: application/json` required on POSTs |
| Process → child CLI | TangleBrain | the CLI and whatever it reaches | `cmd` is a list, never a shell; `scrub_env` strips named vars |
| Config → credential | roster file | the secret itself | `key_ref` indirection; lazy resolution at call time |
| Router → backend | TangleBrain | every configured backend | responses are treated as text, never executed |

## Controls, and what each one actually buys

### Credentials by reference

`key_ref` holds `env:NAME` or `file:PATH` and is resolved lazily at call time. The value never
enters the roster, never enters the repo, and never reaches the browser — the GUI renders the
reference string.

**Buys:** a leaked roster file, a screenshot of the panel, or an accidental `git add` of a config
exposes no secret. Given the repo is public and the roster is the file operators are most likely to
paste into an issue, this is the highest-value control here.

**Does not buy:** anything once the process is running. The resolved secret is in memory and in the
child process environment. An attacker with local code execution as this user has already won — and
that is the correct scope for a local-first tool.

### Never inject a key into a CLI

A `cli` backend uses its own OAuth session. TangleBrain does not hand it a key, and `scrub_env`
actively removes named variables from the child environment so the call cannot silently fall back to
a credential path the operator did not intend.

**Buys:** the blast radius of a compromised or malicious CLI tool stops at that tool's own session.
It never gains an API key it was not already entitled to.

### No shell, ever

`Invoke.cmd` is `list[str]` and the subprocess is spawned without a shell.

**Buys:** shell injection is structurally impossible, not merely filtered. Roster files are
hand-edited and shared; a `cmd` accepting a string would make a pasted config an execution vector.

### The two-gate paid model

A `tier: api` entry is routable only when `settings.api_billing_enabled` **and** that entry's own
`enabled` are both true. Both default false, both are strictly bool-validated so a stray `"yes"` or
`1` cannot coincidentally enable billing, and `api` is never auto-selected by capability — a paid
target must be named explicitly and still passes the gate.

**Buys:** billing cannot start by accident, by typo, or by a routing heuristic deciding a paid
backend looked like a good fit. Two independent switches means one mistake is never sufficient.

### Loopback bind as the authorization model

The GUI and the serve endpoint bind `127.0.0.1` only. The serve endpoint is deliberately keyless —
the `Authorization` header is never read.

**Buys:** exactly one thing, and it is worth being blunt about it: **the bind address is the entire
access control story.** There is no authentication, authorization, rate limiting, or audit trail on
either surface. This is a defensible choice for a single-operator local tool — adding a token would
imply the endpoint is safe to expose, which is the opposite of the intent — but it means the
security posture is one config line deep.

**Explicit consequence:** if either surface is ever bound to a non-loopback address, port-forwarded,
tunnelled, or placed behind a reverse proxy, it becomes an unauthenticated endpoint that spends
money on behalf of whoever reaches it. That is not a hardening TODO; it is a **prohibition**.

### Content-Type as CSRF defense

POSTs to the GUI and serve endpoints require `Content-Type: application/json`.

**Buys:** a malicious page in the operator's browser cannot reach a quota-spending view with a
simple no-preflight cross-origin form POST, because a form cannot set that header. It forces a
preflight, which same-origin policy then blocks.

**Does not buy:** protection from anything that can make a real HTTP request from the machine —
another local process, a browser extension with host permissions, or any locally-running code. The
threat model here is "a web page the operator visited", and only that.

## Threats accepted, with reasons

- **Local code execution as the operator.** Out of scope. It defeats every control here, and no
  local-first tool can defend against it.
- **A malicious or compromised backend.** A backend returns text; TangleBrain never executes it. The
  blast radius is a wrong answer or a prompt-injection payload handed to whatever consumes the
  output. The orchestrator's problem, not the router's — but worth stating, because `delegate` means
  a model chooses which backend sees a sub-task, and a compromised backend therefore sees content
  the operator never explicitly routed to it.
- **Multi-user or shared installs.** Not supported. Every path assumes one operator and one home
  directory.
- **Supply chain.** Inherited from PyPI and from each configured backend. Partially mitigated by
  keeping the dependency surface deliberately small (stdlib GUI, no agent framework) — every
  dependency avoided is one that cannot be compromised. **Not** currently mitigated by pinning:
  `httpx >= 0.27` and `PyYAML >= 6.0` remain unbounded, and there is no scheduled CI that would
  notice an upstream break without a push
  ([#92](https://github.com/Jason-Vaughan/TangleBrain/issues/92)). The `mcp < 2` cap exists because
  this exact shape already broke the published package once (v0.20.1).

## Known gaps

Recorded, not fixed. Each is a decision someone should make deliberately.

1. **No audit trail for security-relevant events.** The usage log records routing and cost. It does
   not record gate state at time of call, which credential path was used, or that a paid backend was
   engaged. After an unexpected bill there is no way to reconstruct *why* a paid entry was
   reachable. Related: [#100](https://github.com/Jason-Vaughan/TangleBrain/issues/100).
2. **No enforcement of key-file permissions — only a warning.** The mode is now checked (see
   Direction), but a loose key file still runs. Refusing is a deliberate non-goal; if that ever
   changes it is a breaking change for existing setups, not a tightening.
3. **No integrity check on the roster.** A modified roster silently changes where prompts go,
   including to a paid or attacker-controlled backend. Consistent with the local-trust model — noted
   because "a config edit changes where your data goes" deserves to be stated out loud rather than
   assumed.
4. **Unbounded core dependency constraints.**
   [#92](https://github.com/Jason-Vaughan/TangleBrain/issues/92), above.

## Reporting a vulnerability

Please do not open a public issue for a security report. See the contact in
[`CODE_OF_CONDUCT.md`](../../CODE_OF_CONDUCT.md).
