# C1 Build Report — verification sweep

**Prepared:** 2026-06-17 (builder session) · **Repo:** `Jason-Vaughan/TangleBrain` (PRIVATE)

---

## ⚠️ Read first — temporal reconciliation (one source of truth)

**C1 is not pending. It shipped at the very start of the project and has been merged and released.**
This report is a **retrospective verification** that the C1 foundation is intact in current `main`,
not a sign-off on freshly-built, unmerged work.

- **C1 merged:** PR **#1** ("Add C1: skeleton, roster loader, and openai-compat adapter to local
  gpt-oss"), **MERGED 2026-06-16T15:47Z**, released **v0.1.0**.
- **Current state:** **v0.9.0 — feature-complete.** Every later chunk (C2/C2b CLI adapters + delegate,
  C3/C3b router, C4 measurement, C5a/b GUI, C6a–c paid-API tier, C7 roster editing, the §6 classifier
  gate, the #17 version fix) is also built, merged, and released.

**Consequences for this prompt's instructions:**
- *"Do NOT start C2"* — C2 (and everything through C7) is already merged. Nothing to start or hold back.
- *"PR/merge status — branch / PR# / HOLDING for PM test"* — there is **no open C1 branch or PR to
  hold**; C1 is PR #1, long merged. (See §6.)
- *"CHANGELOG [Unreleased] updated"* — C1's entry lives in the dated **[0.1.0]** section; `[Unreleased]`
  is currently **empty** (everything is released through v0.9.0). There is no pending C1 work to log.

Everything below verifies the C1 deliverables **as they exist in current `main`**.

---

## 1. Completeness — §10 C1 deliverables (verified against current `main`)

| Deliverable | Status | Evidence |
|---|---|---|
| Python package skeleton (pyproject, tests/, Makefile; Monad-1 conventions) | **DONE** | `pyproject.toml`, `Makefile` (`venv`/`lint`/`test`/`test-live`/`clean`), `tests/` (stdlib `unittest`); console script `tanglebrain = tanglebrain.cli:main` |
| Roster config loader (§5): YAML → typed objects (gpt-oss + 3 subs) | **DONE** | `tanglebrain/roster.py` (`load_roster`→`Roster`/`RosterEntry`/`Invoke`); `config/roster.yaml` has `gpt-oss-120b` (local) + `claude`/`codex`/`gemini` (sub); `tests/test_roster.py` |
| openai-compat adapter: `run(prompt, opts) -> text`, calls LiteLLM | **DONE** | `tanglebrain/adapters/openai_compat.py` `OpenAICompatAdapter.run` → `httpx` POST to `{base_url}/chat/completions`; `tests/test_openai_compat.py` |
| END-TO-END: one request → roster → local entry → adapter → gpt-oss → text | **DONE** *(see §4 caveat)* | `select_local` → `build_adapter` → `OpenAICompatAdapter.run`; live test `tests/test_live.py::LiveEndToEndTest` (gated `TANGLEBRAIN_LIVE=1`). **Exact path command is now `tanglebrain --local "…"`** — see §4 |
| Contract + plan migrated in-repo; `TANGLEBRAIN.md` SUPERSEDED banner | **DONE** | `TANGLEBRAIN.md` top: "⚠️ SUPERSEDED — FROZEN PENDING RECONCILIATION" with §2/§6 supersession; the two formerly-open decisions are now **resolved** (paid-API gate + LiteLLM-fronted custody, invariant #3 softened — ratified 2026-06-16, the whole paid tier shipped since). Plan at `.claude/plans/tanglebrain.md` |
| Shared-doc registrations TANGLEBRAIN / TANGLEBRAIN-PLAN re-pointed in-repo | **DONE (per record)** | Recorded done at C1 in session memory; the in-repo `TANGLEBRAIN.md` + plan are the canonical copies. **Not re-verified via the TangleClaw API this session** — PM can confirm the registration targets from the TC side |
| GitHub repo created (private), visibility verified | **DONE** | `gh repo view` → `"private": true, "visibility": "PRIVATE"` |
| Hygiene files: README, LICENSE, CHANGELOG, .gitignore | **DONE** | All present; `LICENSE` = MIT; `CHANGELOG.md` Keep-a-Changelog (dated [0.1.0]…[0.9.0]); `.gitignore` present (ignores `*.key`, caches) |

**Completeness verdict: all C1 deliverables DONE.** One nuance on the end-to-end acceptance command
(§4) — not a gap, a post-C3b default-routing change.

---

## 2. Janitor

- **Test command:** `make test` (= lint via `py_compile` + `python -m unittest discover -s tests`).
- **Result:** **`Ran 316 tests … OK (skipped=9)`** — 307 passed, **0 failed**, 9 skipped (the
  `TANGLEBRAIN_LIVE=1`-gated live tests in `tests/test_live.py`).
- **Dead code / debug / TODOs:** none. `grep -rE 'TODO|FIXME|XXX|pdb|breakpoint'` over `tanglebrain/`
  → no hits. The only `print()` calls are legitimate user-facing output (`cli.py` result/error/stats,
  `gui/server.py` startup banner) — not debug logging.
- **CHANGELOG `[Unreleased]`:** empty (correct — all work is released through v0.9.0). No pending C1
  items to record.

---

## 3. Critic (reasoning set aside — code + tests + plan only)

- **Scope creep beyond C1?** **At the C1 boundary (PR #1): none** — C1 shipped only skeleton + loader +
  the one openai-compat adapter + the local-first selector, explicitly *not* the router/measurement/GUI
  (the C1 selector docstring even warns "do not grow this into the §6 router"). **In current `main`:**
  the repo now contains C2–C7, but those are **separately reviewed, separately merged later chunks**,
  not C1 creep. So: C1 itself stayed in scope; the repo has since legitimately grown past it.
- **Doc parity (docstring on every function)?** **Pass** for the C1 modules: `roster.py`,
  `adapters/openai_compat.py`, `adapters/base.py`, `selector.py`, `cli.py` — module + every
  public function/class/method carries a docstring (Args/Returns/Raises style). Spot-checked; no
  undocumented public surface in the C1 set.
- **Test gaps (loader / adapter / e2e)?**
  - *Loader* — strong: `tests/test_roster.py` covers the packaged roster shape, every validation error
    (missing id/tier, bad kind, missing fields, duplicate ids), plus later api-tier additions.
  - *Adapter* — strong: `tests/test_openai_compat.py` covers `key_ref` resolution (file/env/none/bad),
    header/Bearer behavior, max_tokens default + override, HTTP/transport/shape errors, null content.
  - *E2E* — **one real finding (low severity):** the live test
    `LiveEndToEndTest.test_one_request_routes_to_local_and_returns_text` calls **bare** `run_once(...)`.
    Since **C3b flipped the default path from local-first to the frontier-first router**, this test now
    exercises the **router**, not the direct local tier — its name/intent are **stale**. It still passes
    (returns non-empty text) but no longer asserts the C1 "direct → local" path. The direct local path
    *is* covered hermetically (`test_openai_compat`, `test_cli::test_local_flag_forces_local_tier`). See
    §4 for the correct command. *(Recommend, when convenient: rename/repoint that live test to
    `run_once(..., local=True)`, or add a sibling live test that pins the direct-local path. Not a C1
    regression — a measurement-of-the-right-thing nit introduced by the C3b default flip.)*

---

## 4. Test hook — PM independent end-to-end verification

**Exact command (direct local path — the C1 acceptance bar), from a fresh shell, no hidden state:**

```sh
cd ~/Documents/Projects/TangleBrain
make venv                                   # one-time: create .venv, install -e .
.venv/bin/tanglebrain --local "Reply with exactly the word: pong"
```

**Why `--local` (not bare `tanglebrain "…"`):** post-C3b the default `tanglebrain "…"` goes through the
frontier-first **router** (orchestrator sub + delegate). To exercise the **C1 direct path**
(roster → local entry → adapter → gpt-oss → text), pass `--local`, which forces `select_local` →
`OpenAICompatAdapter`.

**Confirmed by code (this session):**
- **Reads the scoped key at `~/.config/monad/tanglebrain-spike.key`** — the packaged
  `config/roster.yaml` local entry sets `key_ref: "file:~/.config/monad/tanglebrain-spike.key"`;
  `resolve_key_ref` reads that file (`~` expanded) and sends it as `Authorization: Bearer …`.
- **Hits LiteLLM at `http://monad-1.tail123678.ts.net:4000/v1`** — same entry's
  `base_url`; the adapter POSTs to `{base_url}/chat/completions`.
- **No other setup** — no env vars required; only prerequisites are the key file (0600) present and
  tailnet reachability to Monad.
- **Does NOT depend on the Monad-1 MCP server (the C0 vehicle)** — `openai_compat.py` imports only
  `httpx` (+ stdlib) and calls LiteLLM's HTTP endpoint **directly**; there is no `mcp` import anywhere
  in the C1 path. (The MCP server `tanglebrain-delegate` is a *separate* later component, C2b, used only
  for orchestrator grunt-offload — not on this path.)

**Gated test-suite equivalent** (asserts non-empty text from gpt-oss; currently routes via the router
per the §3 finding):

```sh
make test-live        # = TANGLEBRAIN_LIVE=1 python -m unittest tests.test_live -v
```

**Note:** I did **not** execute the live call this session (no assumed tailnet/key access from the
builder session; `make test` skips the 9 live tests). The live verification is the PM's to run — these
commands are runnable as-is.

---

## 5. Report location

This file: `~/Documents/Projects/TangleBrain/C1-BUILD-REPORT.md` (tracked-repo root). Ready for the PM
to register as a shared doc in the **AI Inference** group.

---

## 6. PR / merge status

- **C1 = PR #1, MERGED 2026-06-16T15:47Z, released v0.1.0.** There is **no open C1 branch or PR** and
  nothing to hold — the "hold for PM test, then `gh pr merge`" step is **already historically complete**
  for C1.
- **Working tree:** clean (only untracked `.tangleclaw/project-version.txt`, TangleClaw-managed, not
  build output). `main` is in sync with `origin/main`; latest tag **v0.9.0**.
- **The PM can still independently run the §4 test hook** against current `main` to satisfy the
  acceptance bar retrospectively — that's the one action that remains meaningful from this prompt.

---

### Deferred / follow-ups (non-blocking)
- Rename/repoint the live e2e test to assert the **direct-local** path (§3) — cosmetic accuracy.
- Re-verify the TangleClaw shared-doc registration targets from the PM/TC side (§1) — out of builder reach.
- (Unrelated, already tracked) **#23** — paid-API tier is hermetically tested but live-unverified by design (anti-key stance).
