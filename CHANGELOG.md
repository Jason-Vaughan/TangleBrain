# Changelog

All notable changes to TangleBrain are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Backend response text no longer reaches `usage.jsonl`.** `docs/design/data-model.md` guarantees
  that prompt and response text is never written to disk, and grounds that in being *structural* —
  "there is nothing to redact". It was not. When a backend returned output an adapter could not
  parse, the adapter put that raw output in its error message, the router kept the message
  untruncated in the task's `failures`, and the record was written to the usage log. Reproduced
  end-to-end: non-JSON prose from a backend landed verbatim in a record whose own docstring says it
  carries derived counts only. Closes
  [#153](https://github.com/Jason-Vaughan/TangleBrain/issues/153).

  **Fixed where the message is built, not where it is written.** Filtering at the persistence
  boundary would be exactly the "redaction filter … bypassed by the next code path that forgets it"
  that the guarantee's own rationale rejects. Eight sites across the CLI and OpenAI-compatible
  adapters now describe the *shape* of what arrived instead of reproducing it.

  **The errors got more useful, not less.** `response JSON missing 'text' field: object with keys
  ['response', 'stats']` says immediately that the wrong parser is wired, which a dumped body never
  did. Object keys are named only when they look like schema — identifier-shaped and short — so a
  key that is really content is counted rather than shown. The failure signal #100 added is intact:
  the reason and the entry id still persist.

  **The guarantee now has a mechanism.** Its norm-registry entry listed enforcement as Critic
  review, which is invisible between reviews — which is how this survived. A test now drives a real
  unparseable response through the adapter, the router's failure shape, and `record_task`, and
  fails if any future site reintroduces a body; each adapter additionally pins its own sites, and
  the shape helper's key filter is tested directly. The registry entry now names those tests
  instead of naming a review.

  **What this does not cover, stated rather than implied.** Two error paths still pass third-party
  text through verbatim: an HTTP error body from an OpenAI-compatible endpoint, and a failed CLI's
  stderr. Both are provider diagnostics rather than completions — but a 400 can echo the offending
  input, and a CLI's argv carries the prompt, so either could in principle carry input text into
  the log. They are kept verbatim deliberately: replacing `invalid api key` or
  `claude: command not found` with a shape would make the commonest setup failures undiagnosable.
  Closing that properly means an error carrying a separately constructed summary for persistence,
  distinct from the message shown on stderr — recorded on
  [#153](https://github.com/Jason-Vaughan/TangleBrain/issues/153) rather than done here.

- **`--stats` now says the figure covers one machine, and that merging is a choice.** The rollup
  has always been per-machine — each install keeps its own `usage.jsonl` and `totals.json` under its
  own state root — and nothing said so, so a second laptop's small number read as a lost history
  rather than as the design working. The `--stats` help text and
  [`docs/design/operations.md`](docs/design/operations.md) now state the scope with its reasoning
  attached: a combined figure needs a stable machine identity, de-duplication of task ids across
  hosts, and a conflict rule for compaction running independently on each machine — a
  distributed-systems problem inside a router whose whole premise is local-first and
  single-operator.

  **Stated as a decision, not an omission**, and so not tracked as a gap: `docs/design/README.md`
  reserves its gap table for open work and leaves ratified non-goals in the documents that own them.

  **A combined view is still available, with a procedure.** The log is one JSON object per line so
  two of them concatenate into a file the rollup reads — read it under a throwaway
  `TANGLEBRAIN_STATE_DIR` rather than writing it back over a live log. It has to be that override
  specifically: every entry point migrates a pre-0.21 cache-tier state root forward before reading
  anything, `--stats` included, and `TANGLEBRAIN_STATE_DIR` is the one override the migration reads
  too, so source and destination collapse and it copies nothing. `XDG_DATA_HOME` does not, so a
  scratch root pointed at by it quietly acquires `~/.cache/tanglebrain`'s history. The honest limit
  on the view itself is that a machine which has already folded rows into its `totals.json` is short
  by whatever it folded; that file is one object and does not concatenate.

  Stated in `README.md` as well as the two places the plan named: the README's own `--stats`
  example carried the unscoped wording verbatim, above the command a first-time user actually runs,
  so the surface most likely to teach the wrong assumption was the one still teaching it.

  Nothing behaves differently. Three tests pin the help wording so the scope cannot drop out of
  `--help` silently.

- **`--stats` no longer asserts one pricing revision over a history that spans several.** The
  `Pricing ref:` line described the *current* `config/pricing.yaml` while the figure beside it
  summed tasks priced under whatever revision was in force when each one ran. It now describes the
  figure: the revision the rollup actually spans when there is one, or how many it spans when there
  are more, with a note saying that each task keeps the figure it was priced at. Current pricing
  labels the line only when the history carries no revision evidence at all — an empty log, or rows
  written before the per-record field existed — so an ordinary single-revision install renders
  exactly as before.

  **The revisions are counted, never listed.** Partitioning the headline by revision is correct and
  unreadable, and the width of the list is unbounded in the number of times the rates have been
  tuned. One number with an honest caveat on it is what the line is for.

  **The note is not a warning.** Spanning revisions is what any long-lived log does the first time
  its operator tunes the reference price; borrowing the glyph the PLACEHOLDER caveat owns would
  teach the reader to discount the caveat that does mean something.

  **A span witnesses an edit, not a rate change** — stated in `docs/design/data-model.md` rather
  than implied. A record carries the reference-model *label* and nothing else of a pricing revision,
  so a relabelling raises the caveat over unchanged rates, and rates edited under an unchanged label
  are a span this line cannot see. Widening the record to carry the rates themselves would catch
  both and is an accepted limit rather than open work: the caveat exists to stop one label being
  asserted over a mixed history, which it does.

  Nothing stored changes and no figure moves. `totals.json` has carried the folded revision set
  since it was introduced, so the span survives compaction destroying the rows behind it.

### Added

- **A lost measurement write now says so, once per run.** `record_task` swallows every exception
  because measurement must never break the answer, and the cost was that the usage log could stop
  recording with nobody told — leaving `--stats` understating a figure still labelled *lifetime*.
  The exception is still swallowed; the first lost append of a process now also prints a warning to
  stderr naming the error — which for the disk failures that dominate carries the errno and the
  path — and which direction the figure moves.

  **Once per process, not once per task.** The recording path runs on every routed request, so a
  per-task warning is a stream the operator learns to scroll past — the same reasoning that fires
  the loose-key-file warning once per file. The message says so itself, so one line is never read
  as one lost task.

  **The notice cannot become the failure it reports.** It runs inside the handler that guarantees
  the answer, so it swallows its own errors: an unusable stderr costs the notice, never the
  response. The honest consequence is that a broken stderr leaves the loss unannounced.

  It fires only when the row genuinely did not land. A failure *after* the append lost no row, and
  compaction — the one thing that runs after it — catches its own failures at their own site.

- **The usage log now prunes itself, capped by size** — recording a task checks the log, and
  crossing `MAX_LOG_BYTES` (5 MiB, roughly 15,000 records) folds the oldest rows into `totals.json`
  and drops them until what remains fits `KEEP_RECENT_BYTES` (~1 MiB). The lifetime spend-avoided
  figure does not move: the fold runs the read path's own summation, so rows crossing the seam
  change nothing a user sees. The log stops growing without limit, with nothing to schedule and no
  operator maintenance. Closes
  [#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101).

  **Size, not age.** An age cap is regressive — a light user loses a whole history to the calendar
  while a heavy user loses nothing — and disk footprint is the cost a cap exists to bound. The
  retention budget is strictly under the cap, so a fold cannot leave the log still over it and the
  next fold is a whole window away.

  Both numbers are named constants with their reasoning attached, read at the moment the check runs
  — changing one changes the behaviour, which is what the tests exercise.

  **The trigger never breaks a recorded task.** It runs outside the append lock (which is not
  reentrant, so triggering from inside would deadlock rather than raise), and swallows compaction's
  own failures at source, so the recording path's blanket catch keeps meaning "the append failed".
  A compaction that *refuses* — a `totals.json` present but unreadable — leaves every row on disk
  and stops the pruning until that file is repaired or moved aside.

  **The over-count a torn fold leaves is now an accepted limit, stated rather than implied.** A
  persisted watermark was considered and rejected: every form of one has to answer "are the rows in
  front of me already counted", and each way of answering fails toward *under*-counting — a
  second-resolution `ts` is shared by rows on both sides of a cut, `task_id` is optional and absent
  from most rows, and a digest of the folded prefix races the appends it would be compared against.
  That trades a power loss inside the microseconds between two fsynced writes for a permanent
  hazard on every read, in the one direction the lifetime figure cannot survive.

  **A fold whose log rewrite fails now puts the totals back**, so a failed compaction is a no-op
  rather than a half-applied one. Under a manual call that was a one-shot over-count; under an
  automatic trigger the log stays over its cap, so a repeating failure — a full disk fails the
  megabyte-scale log rewrite while the small totals write still succeeds — would otherwise re-fold
  the same rows on every recorded task and inflate the figure without limit.

  Two states still leave rows counted twice. A **crash** runs no code, so nothing rolls back, but
  the next run's fold completes and caps the damage at one batch. A **rollback that itself fails**
  leaves the totals inflated and the log over its cap — the unbounded case again. It asks far less
  of a failing disk to put a few hundred bytes back than to rewrite a megabyte of log, so it is
  much the less likely of the two, and it is stated rather than rounded away.

## [0.21.0] - 2026-09-06

### Added

- **Old rows fold into the lifetime totals, and the totals are written first** — a compaction that
  adds the oldest rows into `totals.json` and only then removes them from `usage.jsonl`. This is
  the write half of the measurement store; nothing triggers it automatically yet, so no install
  changes behaviour on upgrade.

  **The order of the two writes is the whole feature.** Interrupted between them, a compaction
  leaves rows counted in both halves and the figure reads too large. The opposite order would drop
  rows before anything recorded them — a smaller figure, no evidence, nothing left to recompute
  from. Over-counting is a bug; under-counting is the loss of the only claim this product makes
  about itself, so both directions are pinned by tests that force the interruption rather than
  waiting for one. What the ordering does *not* buy: nothing is persisted to mark which rows were
  folded, so an inflated figure is not attributable and does not correct itself — the automatic
  trigger has to settle whether a watermark is owed before an unattended crash can inflate it.

  Writes are **durable**, not merely atomic: the staging file is fsynced before the rename and the
  directory after, because `os.replace` orders the two writes against a killed process but not
  against a power loss. A totals file that is present but unparseable is **not folded onto** —
  reading it as zeros is right for a rollup, which renders, and wrong for a writer, which destroys,
  so compaction refuses and leaves both halves on disk.

  The fold runs the **same summation** the read path runs, so a figure cannot change merely because
  rows moved across the seam — a property test asserts exact equality across generated logs, and
  across six successive folds with appends between them. Rows that stay are copied
  through unparsed, so a field added by a newer TangleBrain, or a line left torn by an interrupted
  append, survives the rewrite that keeps it.

  Writing the totals also completes their forward-compatibility contract, which until now existed
  only on the read side. Ignoring an unknown key on read would have *deleted* it on write, so the
  first compaction run by an older TangleBrain would have permanently destroyed fields a newer one
  wrote. The writer now carries unrecognised fields through at any depth. Carrying is not
  maintaining — an older version cannot add the folded rows to a field it does not know, so such a
  value goes stale rather than being lost — and that limit is stated in `data-model.md` and
  `boundaries.md` rather than implied.

  A process-level lock covers the whole read-fold-truncate. It cannot cover a second TangleBrain
  process: an append during a compaction is written to the file being replaced and lost, and two
  overlapping compactions read the same stored totals so the later write discards the earlier fold.
  Accepted for a single-operator local tool and written down: an advisory file lock would hold on
  POSIX only, and a guarantee that silently does not hold on a supported platform is worse than a
  stated limitation.

- **Lifetime totals are a file of their own** — `totals.json`, written beside the usage log in the
  state root and read by `tanglebrain --stats` and the GUI panel. The spend-avoided headline is now
  the sum of stored lifetime aggregates and the per-task rows still on disk, rather than a re-read
  of every row ever written. This was the read half; the fold that writes it is the entry above.
  An absent file reads as zeros, so on a log that has never been compacted the figure is
  byte-for-byte the figure of the previous release — verified against the live 36-record log, where
  every shared figure is identical and only the two labels below differ.

  Splitting the two is what lets a later release bound the row window without the lifetime claim
  shrinking underneath it — and it is what makes a wrong figure attributable, since a discrepancy is
  then either in the stored totals or in the window, never diffused across both. The format carries
  every question `--stats` asks of it, including the **set of pricing revisions the figure spans**:
  that evidence lives per-row today and compaction destroys it, so it is captured at the format's
  birth rather than retrofitted once the rows are gone. The live log already spans two revisions.

  Forward compatibility is the same contract the usage record honours — an unknown key is ignored,
  a missing key reads as zero — and for the same reason: several versions of TangleBrain write this
  store over its life and an append-only store has no migration story. There is deliberately no
  schema-version field; a version number advertises that a breaking revision is possible, and the
  additive-only contract exists so that one never has to be. An absent, truncated, or corrupt file
  degrades to a smaller number, never to an error.

- **The install contract is published as data** — an `installReference` key in
  `.claude-plugin/marketplace.json` naming the marketplace entry (`ref: "main"`,
  `autoUpdate: true`), the plugin key to enable, and the console script that must be present.
  Until now the only statement of how TangleBrain should be installed was prose in `README.md`,
  and prose is not checkable: the entry `/plugin marketplace add` actually wrote carries no `ref`
  and no `autoUpdate`, a shape that reads as governed, resolves to nothing wherever the plugin is
  not already cached, and let a local checkout sit four releases behind in silence. An install
  monitor caught the incomplete entry but could not repair it, because there was nothing canonical
  to repair it *to*.

  `tests/test_plugin_manifest.py` derives every field from the thing it describes rather than
  restating it — the marketplace key from the manifest's own name, the enabled-plugin key from both
  manifests, the required command from `pyproject.toml`'s console scripts, the repo from the
  plugin's homepage. A confidently wrong install reference is worse than none, because a reader who
  trusts it stops looking for a better source.

- **A written deprecation policy** — [`docs/design/deprecation-policy.md`](docs/design/deprecation-policy.md).
  The policy existed in practice (usage-log fields additive-by-default, `--route` retained as an
  accepted no-op) but had never been stated, so it could not be relied on from outside the repo or
  applied consistently inside it. It covers the four consumer-facing surfaces — CLI flags, the MCP
  tool surface, the usage-log record shape, and the two HTTP surfaces — and states the announcement
  path and horizon (deprecated in *N*, not removed before *N+2*, never in a patch).

  Two things it settles that were genuinely open. **Pre-1.0 honesty:** semver says `0.y.z` owes
  users nothing, so the document separates what semver permits (everything) from what TangleBrain
  commits to anyway, and says plainly that those commitments are policy rather than semver until
  1.0. **Dependency floors:** raising a major floor is a breaking change even when no TangleBrain
  code changes, because the user's install resolves differently. Users on the older major are not
  stranded but *pinned* — every previous release stays on PyPI, and the CHANGELOG entry names the
  last release that allowed the old major rather than leaving it in an issue thread.

  It also draws a line it does not cross: whether a new major is *worth adopting* given its
  transitive dependency surface is weighed on the issue that makes the call, not pre-authorized by
  the policy. The policy governs how a floor is announced, not whether to raise it.

- **`key_ref: file:PATH` now warns when the credential file is group- or world-readable.**
  `ARCHITECTURE.md` describes the intended posture as a `0600` file and nothing verified it, so
  a world-readable key was read in silence. Resolution now stats the file first and writes a
  warning to stderr naming the path and its octal mode. It **warns rather than fails**,
  deliberately: refusing to run would break a working setup over a condition the operator may
  have accepted, while a warning still surfaces the misconfiguration at the moment it matters.
  The notice fires once per file per process — the credential is resolved on every routed
  request, and a per-call warning would train the operator to ignore it. POSIX only; Windows
  mode-bit semantics differ, so the check is a clean no-op there rather than a guess. The
  warning never echoes the credential.

- **Failed tasks and lost failover attempts are recorded in the usage log** (#100). A task that
  fails at every backend now writes a `kind: "failure"` record carrying the per-backend attempt
  list, and a task served only after failover carries the attempts it lost in a new optional
  `failures` field. Both are held out of the spend-avoided headline the way delegate records
  already are, and `--stats` shows a `Tasks failed` line (with the lost-attempt count) once there
  is anything to report. The new fields are optional and additive, so existing readers and
  pre-existing log lines are unaffected.

- **Published the design documents as [`docs/design/`](docs/design/README.md).** Eight documents
  covering runtime architecture, the four API contract surfaces, the data model and what survives a
  crash, the security model, contract boundaries, observability, nonfunctional requirements, and
  operations — plus an index. They state what the project promises and why, and they name their own
  gaps rather than only their strengths. Linked from `README.md` and `CONTRIBUTING.md`, which now
  asks that changes to routing, adapters, or either HTTP surface update the matching document in the
  same PR.

- **Filed every gap the design documents disclose as a tracking issue**, so each admission carries a
  fix path: the missing loopback-bind test (#98), unchecked `key_ref` file permissions (#99),
  unrecorded failures and lost failover attempts (#100), the `usage.jsonl` growth and cache-tier
  placement question (#101), and the incorrect `--roster` help text (#102). Three coordination-layer
  gaps found while designing a multi-backend setup are filed alongside them: no way to designate an
  orchestrator rather than round-robin (#95), `--model` silently stripping an orchestrator's
  delegate tool (#96), and capability routing being unable to route upward (#97).

### Changed

- **The `--stats` block says which figures are lifetime and which are not.** The heading now reads
  `spend avoided (cloud-equivalent, lifetime)`, and the delegate tree's `Linked to:` line is marked
  `(within the current row window)`; the GUI panel carries both labels too. That one line is the
  only figure in the block that cannot be folded into the permanent totals — it holds one key per
  parent task id, so its size grows without bound — and an unlabelled window-scoped split sitting
  under a lifetime headline reads as a lifetime count. No number changed.

- **State moved out of the cache tier** — the rotation cursor, the usage log and config backups now
  live under `~/.local/share/tanglebrain/` (honoring `XDG_DATA_HOME`; `TANGLEBRAIN_STATE_DIR` still
  overrides everything). `~/.cache` is *defined* as a directory any cleanup tool may delete at will,
  and nothing TangleBrain kept there was a cache: the usage log is the only record of accumulated
  spend-avoided and is never reconstructible, because prompt and response text is never persisted
  by design; a config backup is the only copy of something the operator hand-edited. Both were one
  `brew cleanup` from gone.

  **Existing installs migrate on first run, and lose nothing.** Every entry in
  `~/.cache/tanglebrain/` is copied forward before anything reads state, **the originals are left
  in place** so a downgrade still finds its history, and a single stderr notice names both
  directories. The copy is per-entry and skips what is already present, so an interrupted migration
  completes on the next run instead of skipping wholesale, and a completed one is a no-op. A
  migration that fails says so on stderr rather than raising — an incomplete log understates
  savings, and an operator has to be able to tell that from a genuinely small number.

  Two details are enforced rather than asserted. The migration copies *every* entry it finds rather
  than a named list of expected files, because a hard-coded list is a claim about a directory's
  contents that decays silently the moment anything new is written there. And the "every console
  script migrates before it reads state" claim is a test that derives its list from
  `pyproject.toml`'s `[project.scripts]`, so adding a fifth entry point without wiring the
  migration fails the build.

  This closes the placement half of
  [#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101); the log is still unbounded and
  that half stays open.

- **The delegate extra now requires `mcp >= 2, < 3`.** `tanglebrain/mcp_server.py` is migrated to
  the 2.x API: `mcp.server.fastmcp.FastMCP` became `mcp.server.mcpserver.MCPServer`. The tool
  surface is unchanged — `delegate`, `delegate_local`, `delegate_targets` and `delegate_many` keep
  their names, parameters and behavior, and the decorators and `run()` are identical, so an
  orchestrator sees no difference.

  **This is a breaking change for installs**, announced as one per
  [`docs/design/deprecation-policy.md`](docs/design/deprecation-policy.md): the floor is
  load-bearing, since `mcp.server.mcpserver` exists in no earlier major. **Users who need mcp 1.x
  should install TangleBrain 0.20.1**, the last release that allowed it — it remains on PyPI and
  keeps working. Only users of the optional `[delegate]` extra are affected; the core install is
  untouched.

  Two things worth knowing before upgrading. The extra now installs `httpx2` alongside
  TangleBrain's own `httpx` (and `httpcore2` alongside `httpcore`), because mcp 2.x moved HTTP
  stacks — wasteful rather than dangerous, and the alternative was holding the SDK at a superseded
  major indefinitely. Total installed distributions are effectively unchanged (30 → 29 in a
  measured comparison), because 2.x declares directly much of what 1.x pulled transitively;
  `opentelemetry-api` and `truststore` are genuinely new.

- **Every declared dependency now carries an upper bound.** `httpx >= 0.27, < 1` and
  `PyYAML >= 6.0, < 7` replace open-ended constraints. This changes what a fresh
  `pip install tanglebrain` resolves to: a future `httpx` 1.0 or `PyYAML` 7.0 will no longer be
  picked up silently. The bound sits at the major boundary deliberately — tighter caps buy nothing
  and create resolution conflicts for anyone installing TangleBrain alongside other packages, which
  is a real cost paid by users to prevent a break semver already announces. What made the `mcp`
  2.0.0 breakage expensive was not the release but the unbounded constraint that let it land in
  every fresh resolve with no announcement and no commit to blame.

### Fixed

- **A backup is never left short under a valid name.** The pricing and roster saves copied
  straight to their final backup path, so an interrupted copy left a truncated file that a later
  restore would read as whole — and a backup is consulted exactly when the original is already
  gone. Both now stage the copy beside the target and rename it into place, the same way the writes
  themselves have always worked. Staging names also carry a unique suffix, so two writers racing on
  one target can no longer interleave their bytes into a shared `.tmp` and rename the mixture in.

- **The GUI panel's colour layer now meets WCAG 2.1 AA, and is asserted rather than audited once
  (#115).** Nine of twenty-seven pairs failed, all of them muted text or control outlines. Two
  findings are worth repeating. `--text-muted` cleared 4.5:1 against the *page* background but not
  against the two surfaces it is actually used on (4.34:1 on cards, 3.89:1 on the elevated tiles) —
  measuring against the page alone would have declared it conformant. And form fields were outlined
  at 1.09:1 against their own fill, so the boundary of a text input carried essentially no contrast;
  that outline is now its own token, kept separate from the decorative border so the fix does not
  restyle cards and tables that never needed it. Raising the rest-state outline then dropped the
  *focus* outline to 1.40:1 against it — passing SC 1.4.11 while quietly failing SC 2.4.7 Focus
  Visible — so the focus colour moved too. `tests/test_gui_contrast.py` parses the palette out of
  the stylesheet and checks every pair, plus two structural assertions that stop the pair table
  falling behind the stylesheet: every `:root` token must be measured or explicitly exempt, and no
  colour literal may live outside `:root`. Decorative borders and disabled controls are exempt by
  the explicit carve-outs in SC 1.4.11 and 1.4.3; both exemptions are recorded in
  `docs/design/nonfunctional-requirements.md` rather than left silent.

- **Two adapters coerced caller-supplied options past their own error contract.** `opts` is a
  `Mapping[str, object]`, and both the openai-compat and CLI adapters ran a bare `int()` / `float()`
  over it — so a non-numeric `max_tokens` or `timeout` escaped as a raw `TypeError`, past the
  `AdapterError` each method documents and past `cli.main`'s handler that turns that contract into
  one clean line instead of a traceback. Both now raise the documented type. A numeric string still
  coerces: guarding the failure path is not licence to tighten the success path, and a test pins
  that half too. Surfaced by adopting mypy (#113).

- **`from_entry` checks the invariant it claimed in a comment.** Both adapters passed
  `entry.invoke.base_url` and `.model` through with the note "validated non-None by the roster
  loader". True of any entry that came through `load_roster` — but `Invoke` is a public dataclass
  that can be built directly, and a `None` reaching that path surfaced as a `TypeError` from inside
  the HTTP request rather than as the documented `AdapterError`. It is now a check, with a test.

- **A delegate failure again tells the orchestrator what went wrong.** The mcp 2.x migration
  silently dropped the reason: 1.x surfaced `Error executing tool X: endpoint down`, while 2.x
  reports a bare `Error executing tool X` for any exception that is not a `ToolError`. The tools
  now wrap `AdapterError` and `RouterError` so the message survives, and the result still carries
  `is_error`. Losing the reason matters more than it sounds — "endpoint down" and "key_ref file
  not found" call for completely different responses, and the operator reading the transcript is
  the one who has to tell them apart. A no-fit is deliberately *not* wrapped: it is a routing
  signal telling the orchestrator to do the work itself, and reporting it as an error would make a
  working delegation look broken.

- **An unreadable `key_ref` file now fails with a clean error instead of a traceback.** The
  permission check stats the file, but the read that followed was unguarded: a file the process
  could not open (wrong owner, restrictive mode, removed mid-run) raised a raw `PermissionError`,
  which is outside `resolve_key_ref`'s documented `Raises` contract and outside the `AdapterError`
  clause `cli.main` catches. The operator saw a stack trace where every other credential failure
  prints one line. It now raises `AdapterError` naming the path and the OS reason.

- **A paid last-resort backend reached by failover no longer receives the delegate tool.** The
  fix for #96 asserted the pin/delegate rule at the call sites that had the symptom; the router's
  failover loop still passed a blanket `inject_delegate=True`, so a `tier: api` CLI entry that is
  not `can_orchestrate` was built as an orchestrator whenever the rotation exhausted. The decision
  now lives in one place — `build_adapter` derives it from the entry's own `can_orchestrate` when
  the caller has no opinion — so every current and future call site is correct by default rather
  than by restating the rule. The delegate server still passes `False` explicitly, which is the
  no-recursion rule and not an absence of opinion.

- **`--model` on a `can_orchestrate` entry no longer strips its delegate tool.** Pinning a
  backend built its adapter without `inject_delegate`, so an orchestrator pinned with `--model`
  ran the whole task alone — no delegate tool registered, no warning, no error. The only visible
  symptom was a lower spend-avoided figure in `--stats`, which reads as a routing mystery rather
  than a bug. Pinning *which* backend serves a request is a different decision from *whether*
  that backend may delegate, and one must not silently imply the other. The same defect was
  present on the streaming path (`run_once_stream`) and is fixed alongside it; delegation now
  tracks the entry's own `can_orchestrate` flag on both.

- **`--roster` help text now states the real default.** It claimed the default was the packaged
  `tanglebrain/config/roster.yaml`; the actual resolution is `$TANGLEBRAIN_ROSTER`, then
  `~/.config/tanglebrain/roster.yaml` if it exists, then the packaged example. This matters more
  than a typo: the operational runbook's first diagnostic step for a mis-route is "check which
  roster is actually in play," and `--help` answered it incorrectly — sending an operator to read
  and edit the packaged example while their own config was live. The symptom of editing the wrong
  roster is that nothing changes, which reads as a routing bug rather than a documentation one.

### Internal

- **Filed [#131](https://github.com/Jason-Vaughan/TangleBrain/issues/131) for the accessibility
  surface #115 did not cover.** The new Accessibility section names keyboard traversal,
  screen-reader semantics, motion and reflow as unaudited — and `docs/design/README.md` promises,
  twice over, that every gap the design docs disclose has a tracking issue. Disclosing a new one in
  prose with nothing behind it would have quietly broken that promise on the same page that makes
  it. The gap ledger and the design-overview Artifact both carry the row now.

- **Closed the observations the verify pass demoted.** Two were defects rather than polish. Editing
  `project-state.yaml` to retire the answered contrast question had orphaned a `priority: low` line
  into the *next* entry, giving it a duplicate key that YAML silently resolves last-wins — the #92
  question's priority had flipped from `medium` without anything saying so. And the contrast suite's
  colour-property guard omitted `border-bottom` and `border-right` while `border-bottom` is used
  twice in the shipped stylesheet, so its docstring's "every colour-bearing declaration" was still
  not true. The pattern is now hoisted to module scope and shared with a new test that runs it
  against synthetic CSS for every syntax and every border side — the guard's coverage is asserted
  in-repo rather than claimed, and narrowing it now reds the suite. Also: the streaming type guard
  is a `raise` rather than an `assert`, because `python -O` strips asserts and a stripped guard
  would have handed the client a dict *as* the byte iterator.

- **Resolved the Critic findings for Chunk C.** The substantive ones were a stale-claim class and a
  real threshold bug. `ApiAdapter.from_entry` was a near-verbatim copy of its base differing only in
  one string, and this chunk had deepened it by copying a new guard into both; the kind is now a
  class attribute and the subclass has no override. `ignore_missing_imports` was global, which would
  have silenced a renamed or misspelled *first-party* import — exactly the defect the gate was
  adopted to catch — and is now scoped to `mcp.*`. The mypy pin was bounded at the next major by
  reflex, but mypy adds checks at a *minor*, which reds untouched PRs in a lockfile-free CI; both
  dev tools are now bounded at their real breaking-change unit. The contrast suite classified a
  20.8px normal-weight value as WCAG large text, asserting 3:1 where 1.4.3 requires 4.5:1, and
  missed two pairings that exist in the shipped stylesheet (`--danger` on the output pane, the
  button outline on hover). Its literal guard now checks colour-bearing *properties* rather than
  hunting hex, so `rgba()`, `hsl()` and bare CSS names cannot slip past — and the docstring now
  states plainly what the guards do **not** cover (a new pairing of two existing tokens) instead of
  implying the AA claim is fully mechanical.

- **De-anchored every line-numbered citation into `.py` files, and retired a claim that outlived its
  correction.** "The codebase's one deliberately broad exception handler (`measurement.py:376-378`)"
  appeared in six renderings; the previous commit fixed the one it happened to be editing and left
  the rest — including a normative Direction statement, which would have made the eight waivers this
  chunk legitimised read as departures. Every site now cites by symbol and points at the grep. The
  cited line ranges had already drifted: `376-378` is the linkage block today.

- **Adopted ruff and mypy as defect gates; declined the formatter and `--strict`, with reasons
  (#113).** `make lint` now runs both, `make test` depends on it, and CI runs `make test`, so a gate
  cannot be green locally and absent in CI. Both tools sit in a new `dev` extra — nothing there is
  imported at runtime, so the minimal-dependency posture, which is about what a *user* installs, is
  untouched. Each half of the ruling was measured against this codebase first: `ruff format` would
  have rewritten 41 of 45 files while catching no defects, and `mypy --strict` reported 62 errors
  against default mode's 11, 39 of them `type-arg` ceremony. The rule set selects for defects and
  deliberately omits the style families, which are the formatter question under another name. The
  full ruling, and what would justify revisiting either decline, is in
  `docs/design/nonfunctional-requirements.md`, "Code quality gates".

- **Twelve inert suppressions removed or made real.** The codebase was already written as though
  these tools were running: it carried `# noqa: BLE001`, `N802`, `F401` and `E731` directives, plus
  a `# type: ignore[arg-type]` in `measurement.py` that named the wrong error code. Every one
  suppressed a rule nothing had selected, so none of them did anything. That is the same defect
  #113 describes for annotations, one level up — a decision documented but not enforced. `RUF100` is
  now in the rule set so a waiver that stops being needed fails the build.

- **A latent closure bug and a truncating comparison in the suite.** A `lambda` in
  `test_openai_compat.py` closed over a loop variable rather than capturing it — harmless only
  because each iteration consumes its lambda before the next rebinds it — and `test_roster_edit.py`
  zipped two line lists without `strict`, so an edit that changed the line *count* would truncate
  to the shorter side and could still satisfy "exactly one line differs". Both found by ruff on
  adoption.

- **Packaging tests assert parsed constraints instead of the characters of a requirement string.**
  The upper-bound check looked for a literal `<` anywhere in the requirement, which an environment
  marker satisfies with no ceiling at all (`foo >= 1 ; python_version < "3.13"` passed) — and the
  `mcp` check was whitespace-sensitive, so the semantically identical `mcp>=2,<3` failed it and a
  formatter run would have redded the suite over nothing. Both now compare against the version
  specifier with the marker stripped and whitespace normalized. Verified both ways: the marker-only
  form now fails, the unspaced form now passes.

- **Retired the `#90` admissions the same bundle closed.** `deprecation-policy.md` and
  `api-contract.md` still described the mcp floor decision as pending — one of them in the sentence
  justifying why the policy was written when it was, so a reader assessing whether it was authored
  under pressure from a live decision got the wrong answer. The bundle applied the closed-gap rule
  to #92 and #114 and missed the issue it closed itself.

- **The design-doc gap table says what its completeness claim covers.** It read "Every gap these
  documents disclose has an issue" while `security-model.md` discloses two ratified non-goals — no
  roster integrity check, permissions warned rather than enforced — that deliberately have none.
  The claim now scopes itself to open work and says non-goals are decisions rather than backlog.

- **`installReference` verified against the consumer, not only against itself.** The five new tests
  prove the key is self-consistent; they cannot prove `marketplace.json` still loads with an
  unrecognized top-level key, which is the failure class that produced v0.20.1. `claude plugin
  validate` reports validation passing with `Unknown field 'installReference'. Claude Code ignores
  it at load time.` — ignored being the intended outcome. Recorded in `operations.md` with the
  command, so it is re-checkable rather than a remembered assurance.

- **The deprecation policy's own precedent is now a test.** `deprecation-policy.md` commits that a
  removed CLI flag becomes an accepted no-op rather than an error, and cites `--route` as the
  standing example — but `--route` was declared in the parser and read nowhere, with no test
  behind it. Deleting it would have passed the whole suite while breaking exactly the forgotten
  scripts the commitment exists to protect. `tests/test_cli.py` now pins both halves: the flag
  still parses, and it reaches `run_once` as no argument at all, so it is inert rather than merely
  tolerated. Verified by deleting the flag, which now fails.

- **The deprecation policy now says what "announced as breaking" means before 1.0.** The policy
  requires a dependency-floor bump to be announced as breaking; TangleBrain's release tooling maps
  the major-bump marker (the word, followed by a colon) to a *major* bump, which from `0.20.x` is
  `1.0.0` — a claim about the project's maturity that no single change earns. The policy now states
  that pre-1.0 a breaking change ships as a minor bump announced in prose, and reserves the marker
  for after 1.0. Announcement is a duty to the reader; the marker is a lever on the version number,
  and conflating them either understates the break or overstates the release.

  Note the marker is deliberately *not written literally* in this entry: the bump step matches it
  anywhere in the body, so it cannot distinguish a marker from a mention of one. Writing this entry
  the obvious way would have forced the 1.0.0 it exists to warn against.

- **The design-doc gap table pointed at the wrong issue for the deprecation policy.** The row
  reading "No written deprecation policy" linked #90 (the mcp 2.x migration) rather than #114,
  which is the issue that actually writes the policy. #90 is where the gap *becomes live* — an
  `mcp >= 2` floor is the first real test of an unwritten rule — but a reader following the link
  arrived at a migration rather than the policy work. A wrong link satisfies the table's
  completeness claim while defeating its purpose, which is worse than an absent row.

- **CI now runs on a weekly schedule, not only on push.** A dependency can break the build with no
  commit to blame, and a push-only CI never notices: after the `mcp` 2.0.0 release `main` read
  green for days because its last run predated the release, and the failure first surfaced on an
  unrelated markdown-only PR. The scheduled run resolves dependencies fresh — no lockfile, no cache
  — so it sees what a user's install would resolve to today. A lockfile was considered and rejected
  for CI: a green run against frozen versions says nothing about what a fresh install gets, which is
  the exact signal wanted here. `workflow_dispatch` is enabled alongside it so the canary can be
  exercised on demand rather than only by waiting a week.

- **`tests/test_packaging.py` asserts the rule, not one instance of it.** The upper-bound check
  covered only the `mcp` requirement; it now iterates every requirement in `project.dependencies`
  and in every `optional-dependencies` extra, so a dependency added to a new extra is held to the
  same rule without anyone remembering to extend the test. The `mcp`-specific test was not deleted
  in the consolidation but **sharpened** to assert `< 2` rather than merely *an* upper bound —
  `mcp >= 2, < 3` satisfies the general rule while shipping a delegate extra that cannot import,
  so the two tests now fail on different mutations rather than restating one another.

- **`observability.md`'s first gap now carries its issue.** "No `unlinked` visibility" was the
  one gap in the design set with no tracking issue behind it, which broke `README.md`'s claim that
  every disclosed gap has one. Filed as #123 and linked from both, with the actual defect stated:
  a lost `TANGLEBRAIN_TASK_ID` hop and a genuinely parentless sub-call land in the same bucket, so
  the count answers no question.

- **The design-doc gap table lists every gap the documents disclose.** `docs/design/README.md`
  claims "every gap these documents disclose has an issue" and then omitted #97 — capability
  routing ranking by cost only, named in `api-contract.md`. A completeness claim with a missing
  row is worse than no claim, because it stops the reader checking.

- **The loopback bind is now a tested contract, for both HTTP surfaces.** `tanglebrain-gui` and
  `tanglebrain-serve` are unauthenticated by design and spend real backend quota, so the
  `127.0.0.1` bind is not a default — it is the whole authorization model, and widening it does
  not weaken the posture but voids it. Nothing tested it: a one-character edit to `0.0.0.0`
  passed green. `tests/test_bind_address.py` asserts the address handed to the server rather
  than that a server starts, since a test connecting over localhost passes just as happily
  against `0.0.0.0`. It covers the three ways the invariant can be voided — widening the
  literal, binding every interface with an empty host, and adding a `--host` flag that leaves
  the literal untouched — the last of which the address assertions alone do not catch. No
  production code changed and no socket is opened.

- **`README.md` leads with the install command.** A visitor arriving from the PyPI listing had to
  scroll past the pitch to find out how to install; the `pip install tanglebrain` line now sits
  directly under the badges, alongside a MIT license badge that makes the licensing explicit
  without a click.

- **Reconciled `FEATURES.md` and `PROJECT-MAP.md` with the current generator.** Both landed in the
  repo carrying a superseded TangleClaw scaffold: `FEATURES.md` documented the abandoned
  `file.js:line` pointer format (the generator now mandates stable `file.js#symbolName` anchors,
  precisely because line pointers rot) and named its third section `Methodologies / Engines` rather
  than `Governance / Engines`, and `PROJECT-MAP.md` omitted the `Tangle-Shared` doc group. Both
  files now match `TangleClaw/lib/projects.js`, the three auto-stubbed `TBD` entries are resolved,
  and the `<!-- describe -->` placeholders are filled.

- **Issue templates now declare labels that exist.** `feature.md` and `add-backend.md` asked for a
  `feature` label the repo has never had, so `gh issue create` failed outright on it and
  UI-filed issues landed unlabeled. Both now use `enhancement`; the `backend` label
  `add-backend.md` also referenced has been created. Every label named across the three
  templates now resolves.

## [0.20.1] - 2026-08-01

### Fixed

- **The `delegate` extra no longer installs an SDK it cannot import (#87).** `mcp` 2.0.0
  (2026-07-28) renamed `FastMCP` to `MCPServer` and removed the `mcp.server.fastmcp` path
  `tanglebrain/mcp_server.py` imports, so the open-ended `mcp >= 1.0` constraint resolved to a
  broken install — `pip install "tanglebrain[delegate]"` produced a delegate server that failed at
  import, and CI went red on every branch. The constraint is now `mcp >= 1.0, < 2`; a new
  `tests/test_packaging.py` asserts the upper bound stays, since nothing at runtime exercises a
  dependency constraint and the gap went unnoticed until a resolve happened to pick up the new
  major. Migrating to the 2.x API is tracked separately — lifting the cap moves the floor to
  `mcp >= 2` and drops 1.x users, which is a decision rather than a bump.

## [0.20.0] - 2026-07-06

### Added

- **Antigravity CLI (`agy`) as the gemini replacement orchestrator (#61)** — the packaged
  example roster documents an `antigravity` sub entry (`agy -p {prompt}`, `parse: plain`;
  verified live on agy 1.0.10: bare response on stdout, ~5s, rides the old CLI's `~/.gemini`
  OAuth), restoring the third orchestrator slot the 2026-06-18 gemini sunset emptied. No
  delegate injection yet — agy exposes no per-invocation MCP flags; #81 tracks wiring the
  local-delegate tool when it does. The live suite exercises the entry when `agy` is installed.

- **Serve-origin marker + parent-task attribution (#74)** — usage records now carry an
  `origin` field (`cli` | `gui` | `serve`) so serve-mode traffic is distinguishable from CLI and
  panel runs, and `tanglebrain --stats` shows the per-origin split (records predating the field
  roll up as `untagged`, never guessed at). OpenAI-compat callers can additionally send an
  optional `X-TangleBrain-Parent-Task` header carrying their own task/session identity —
  trimmed, capped at 128 chars, recorded onto the usage record as `parent_task_id` for
  cross-system attribution, never routed on. Both are additive record fields; old records and
  readers are unaffected.

## [0.19.0] - 2026-07-04

### Added

- **Library streaming core (c13-S1 of #73)** — adapters gain an optional
  `StreamingAdapter` capability (`run_stream(prompt, opts) -> Iterator[str]`), implemented by
  the openai-compat adapter as httpx SSE pass-through (the paid `api` adapter inherits it; both
  billing gates are untouched — they act at selection time, before any adapter exists). New
  `run_once_stream()` beside `run_once`: identical path precedence, task-id minting, and gates;
  direct-adapter paths (pin / `--local` / gate-local) stream incrementally when the backend can,
  everything else — including the router path, per the ratified c13 v2 scope — delivers the
  completed text as a single-item stream. Metering parity: streamed responses are recorded on
  stream completion, with partial text recorded when a stream dies or is abandoned mid-way
  (real backend spend), and nothing recorded for a stream that failed before its first token.
- **True incremental streaming in `tanglebrain-serve` (c13-S2, closes #73)** — `stream: true`
  now delivers real `chat.completion.chunk` deltas as the backend produces them, for backends
  that can stream (pinned `openai-compat`/`api` entries and the classifier-gate local path);
  cli-kind backends and the full-router `auto` path keep the single-chunk delivery (ratified v2
  scope). The view primes the pump — the first delta is pulled before headers commit, so every
  failure up to the backend connection returns a plain JSON error with the right status, never
  broken SSE — and the handler writes one flushed, close-delimited SSE event per delta. A stream
  that dies mid-way ends with one in-stream `{"error": ...}` event and no `[DONE]`. The finish
  chunk always carries the estimated `usage` block and the `tanglebrain` extension. Verified
  live with the real `openai` client: multiple incremental chunks from the pinned local backend,
  and an `auto` round-trip.

### Security

- **Knob GUI POST hardening** (#72), mirroring the serve endpoint's guards: `POST /api/*` now
  requires `Content-Type: application/json` (415 otherwise) — the panel's own requests already
  send it, and the check keeps no-preflight cross-origin browser requests (e.g. a malicious page
  POSTing `text/plain` to `/api/run`, which spends real backend quota) from ever reaching a view.
  A malformed `Content-Length` header now returns a clean JSON 400 instead of a traceback and a
  dropped connection, and negative values are clamped.

## [0.18.0] - 2026-07-03

### Added

- **Server mode (`tanglebrain-serve`) — the router as a local OpenAI-compatible endpoint**
  (issue #70, S1). `POST /v1/chat/completions` fronts the same routing path the CLI uses: the
  `model` param is a routing directive (`auto` = full router, a roster id = explicit pin, unknown
  ids → a clear `model_not_found` error), chat `messages` arrays are flattened to a role-tagged
  transcript (non-text content parts rejected loudly), and `GET /v1/models` lists `auto` + the
  roster ids. `stream: true` is emulated in v1 — the completed response is framed as a single SSE
  chunk (true incremental streaming is a follow-up). The endpoint binds `127.0.0.1` only and
  ignores the `Authorization` header (local callers need no key); POSTs must send
  `Content-Type: application/json` (415 otherwise — keeps no-preflight cross-origin browser
  requests from reaching routing); the paid-API tier stays behind both existing billing gates,
  and served requests are metered exactly like CLI runs. Zero new
  runtime dependencies (stdlib `http.server`, mirroring the knob panel's pure-`dispatch` split).
  `run_once(return_served=True)`'s served summary now also carries the minted `task_id` (the
  endpoint reuses it as the completion id, linking a response to its usage record).

## [0.17.0] - 2026-07-03

### Internal

- **Automated PyPI publishing via trusted publishing (`.github/workflows/publish.yml`).** Publishing
  a GitHub release now builds, checks, and uploads to PyPI through OIDC — no API token stored
  anywhere. Guards: the release tag must match the `pyproject.toml` version, and `twine check` must
  pass before upload. One-time PyPI-side setup: add the repo/workflow/environment as a trusted
  publisher under the project's Publishing settings.
- **PyPI-listing polish.** Trove classifiers + full `[project.urls]` set (Repository, Changelog,
  Issues, Releases) in `pyproject.toml`; README relative links converted to absolute GitHub URLs so
  the PyPI project page renders them correctly; PyPI version + Python-versions badges added. Takes
  effect on the PyPI page with the next uploaded release.

### Changed

- **TangleBrain is now on PyPI** — `pip install tanglebrain` (add `[delegate]` for the MCP server)
  replaces the from-GitHub / from-clone install as the primary path. v0.16.0 published; install docs
  in the README and the plugin README updated accordingly. Publishing also removes the
  dependency-confusion window the 0.16.0 review flagged (the name can no longer be squatted).

## [0.16.0] - 2026-07-03

### Added

- **Claude Code plugin for the delegate MCP server (one-click mode-4, closes #63).** The repo is now
  its own Claude Code plugin marketplace (`.claude-plugin/marketplace.json`), listing a
  `tanglebrain-delegate` plugin (`plugins/tanglebrain-delegate/`) that registers the existing
  `tanglebrain-delegate` stdio MCP server declaratively. Install becomes two commands —
  `/plugin marketplace add Jason-Vaughan/TangleBrain` + `/plugin install tanglebrain-delegate@tanglebrain` —
  instead of manual `claude mcp add`. The plugin wires the pip-installed console script (documented
  prerequisite: install the `[delegate]` extra from GitHub or a clone — TangleBrain is not on
  PyPI); it does not vendor the Python code. Manifest
  drift (renamed console script, broken source path, name mismatch) is CI-guarded by
  `tests/test_plugin_manifest.py`.

### Internal

- **Refreshed README + ARCHITECTURE for the shipped feature set.** The README status line is now
  version-agnostic (links the latest release + CHANGELOG instead of a fixed version that drifts), and
  the delegation bullet describes the full **scatter-gather** capability (route by id/capability, fan
  out concurrently, metered + linked to the parent task) instead of the old local-only phrasing.
  `ARCHITECTURE.md` is stamped v0.15.0 and its per-parent-task-tree passages — which still called the
  tree "deferred" / "the remaining stretch" — now describe it as **shipped** (the `TANGLEBRAIN_TASK_ID`
  propagation mechanism). Docs-only; no code change.
- **Skip the `gemini` live CLI test — the CLI sunset (#61).** The `gemini` CLI sunset for individuals
  on 2026-06-18 (migrated to Antigravity) and now exits with an `IneligibleTierError`, so
  `LiveCliTest.test_gemini_returns_text` can no longer pass. It now skips with a pointer to #61; the
  full live suite is green again against the supported backends (claude + codex + local). The `gemini`
  orchestrator entry is disabled in the operator roster (out of rotation). Test-only.

## [0.15.0] - 2026-06-18

### Added

- **Per-parent-task delegation tree (cross-process linkage, scatter-gather roadmap #39 stretch /
  closes #52).** Each delegated sub-call is now linked back to the specific top-level task that
  spawned it, across the process boundary. The CLI mints a task id per routed task; the
  orchestrator-CLI adapter injects it as `TANGLEBRAIN_TASK_ID` into the orchestrator's environment
  (only when the delegate tool is injected), the orchestrator forwards it to the MCP delegate child
  it spawns, and `run_delegate` reads it back to stamp each delegate record's `parent_task_id`. Task
  records gain a `task_id`, delegate records gain a `parent_task_id` (both written only when present,
  so existing records and readers are unaffected). `tanglebrain --stats` and the rollup gain a
  `by_parent` grouping — "Linked to: N parent task(s)" — with sub-calls run outside a propagated task
  grouped as `unlinked`. The linkage was **manually verified live through the real claude→MCP-delegate
  boundary** (the env survives the orchestrator's subprocess hop; the parent and delegate records
  shared the same id) — the orchestrator-forwards-env hop is a load-bearing assumption, not a
  TangleBrain-enforced guarantee, so a delegate that loses the env degrades safely to `unlinked` (never
  an error). This was the deferred half of the scatter-gather epic whose entry criterion was a
  live-verification spike — now done.
- **Knob panel surfaces the delegation tree.** The panel's "Delegated sub-tasks" card now shows a
  **Linked to** stat (`N parent task(s)`, with any `unlinked` sub-calls noted) — GUI parity with the
  `tanglebrain --stats` rollup, so the per-parent-task linkage is visible in the panel, not just the
  CLI. Read-only; no new endpoint (the data already rides `view_stats`'s rollup payload).

### Fixed

- **`pyproject.toml` package metadata carried the purged "cost-tiered / flat-rate subscriptions"
  framing** that the public-rollout neutralization scrubbed everywhere else (#42). Both the `description`
  and the `cost-tiered` entry in `keywords` (→ `local-llm`) — the metadata rendered on the repo and any
  package index — now match the neutral positioning used in the README and `ARCHITECTURE.md`:
  *"A local-first, config-driven LLM router across OpenAI-compatible backends you own."*

### Internal

- **Gated live smoke check for delegate parent-task linkage (closes #55).** A `TANGLEBRAIN_LIVE`-gated
  test routes a delegation-inducing prompt through the real router → orchestrator → `delegate_local`
  and asserts each delegate record's `parent_task_id` matches the parent task's `task_id` — a standing
  guard for the load-bearing "orchestrator forwards env to the MCP child" assumption (it skips, never
  fails, if the orchestrator doesn't delegate that run, since delegation is emergent). Test-only; gated
  off in CI.

## [0.14.0] - 2026-06-18

### Added

- **Delegate observability + metering (scatter-gather roadmap #39, slice 6).** Delegated sub-calls
  are now metered: every `run_delegate` execution (including each `delegate_many` item — metered at one
  seam) is logged as a `kind: delegate` usage record with its served backend + estimated tokens.
  `tanglebrain --stats` and the knob panel gain a **"Delegated sub-tasks" breakdown by backend** (count,
  est tokens, informational cloud-equiv). Delegate records are kept **out of the "spend avoided"
  headline** so a sub-call's saving is never double-counted against its parent task, and concurrent
  fan-out appends are serialized by a process-level lock. Records carry a new `kind` field
  (`task`/`delegate`; older records read as `task`). The per-parent-task tree (cross-process linkage)
  is deferred. Closes the deferred metering noted since the measurement layer landed.

### Internal

- **Documented the synthesis/reduce pattern (scatter-gather roadmap #39, slice 4).** README +
  ARCHITECTURE now spell out that the orchestrator synthesises `delegate_many` results itself (it
  holds the original task context), and offloads a *mechanical* stitch with an ordinary
  `delegate(task=…)` call — so no dedicated reducer tool ships. Documentation of existing behaviour;
  no code change. The reduce step stays the orchestrator's by design until observability data (a later
  slice) shows a TB-side reducer would earn its keep.

## [0.13.0] - 2026-06-18

### Added

- **Parallel fan-out (`delegate_many`).** A new MCP tool lets an orchestrator fan **several sub-tasks
  out concurrently** in one call and collect them, instead of delegating one at a time. Each item
  (`{prompt, target?, task?, max_tokens?}`) routes independently — a batch can mix backends — and runs
  on a `ThreadPoolExecutor` over the existing sync `run_delegate` (plain Python, no new deps). Results
  come back **in input order** with a per-item `status` (`ok` / `no_fit` / `error`); one failing
  sub-task never sinks the batch. Concurrency is bounded by a **system-derived default**
  (`os.cpu_count()`), an **operator override** (new `delegate_max_concurrency` in `settings.yaml` —
  pin it to your backend's real parallelism, e.g. `OLLAMA_NUM_PARALLEL`), and an optional per-call
  `max_concurrency` that may lower it. Dispatch + collect only — synthesis stays the orchestrator's
  job. Third slice of the scatter-gather roadmap (#39).

## [0.12.0] - 2026-06-18

### Added

- **Capability-routed delegation.** The `delegate` MCP tool gains a **`task`** parameter: instead of
  naming a backend id, an orchestrator can ask for a *capability* (a `good_at` tag, e.g. `code`) and
  TangleBrain selects the **cheapest `can_delegate` backend** good_at it (`local` before `sub`, ties
  by declared order) — sub-task-level task-fit mirroring the request-level router. Precedence is
  `target` (explicit id) > `task` (capability) > free local default. **Paid `api` backends are never
  auto-selected by `task`** (the ratified paid-is-last-resort invariant; reach one only via an
  explicit `target`). When no backend fits a `task`, the tool **hands the sub-task back to the
  orchestrator to do itself** — a returned instruction, not an error (a new `NoDelegateFit` signal
  caught at the MCP boundary). Second slice of the scatter-gather roadmap (#39).

## [0.11.0] - 2026-06-18

### Added

- **Generalized / tiered delegate.** An orchestrator can now offload a sub-task to a *configured*
  backend, not just the free local model. The `tanglebrain-delegate` MCP server gains two tools
  alongside the unchanged `delegate_local`: **`delegate(prompt, target?, max_tokens?)`** routes to
  any roster entry flagged the new **`can_delegate: true`** (mirrors `can_orchestrate`), and
  **`delegate_targets()`** lists the configured menu (`id`, `tier`, `good_at`, `cost`, `kind`) so the
  orchestrator can pick by fit; the `delegate` tool's description also enumerates the menu, built at
  server startup. Targets are invoked as leaves (no recursive delegation); `api` targets stay behind
  the billing gate. Secret-safe (the menu never emits a `key_ref`). The shipped roster flags its
  local tier `can_delegate: true` and carries a commented non-local target example. Non-local
  delegate spend is not metered in this version (orchestration-tree observability is tracked on the
  scatter-gather roadmap, #39). First slice of #39. Closes #38.
- **Project logo.** A snake-and-circuit-brain mark now brands the README (hosted in the
  `project-assets` repo) and the knob panel — `tanglebrain-gui` ships a packaged copy, serves it at
  `/logo.png`, and uses it as the page header + favicon.

### Changed

- **README restructured around Problem → Solution.** A "Cloud-by-Default Routing / routing debt"
  problem statement and a "Local-First Router You Own" solution lead the page, plus a "Standalone, or
  part of the Tangle family" section (welcomes forks/PRs; notes optional integration with
  [TangleClaw](https://github.com/Jason-Vaughan/TangleClaw)). Status line corrected to
  *v0.10.0 — first public release*.
- **README surfaces the OAuth-/local-first credential model and prompt-aware routing.** Clarifies
  that TangleBrain prefers your local models and authenticated (OAuth) tool sessions — never injecting
  an API key into a CLI — with the raw-API-key tier a deliberate, gated opt-in; and that an optional
  classifier reads each request and routes grunt work to the free local backend. The measurement
  bullet is reframed as cost measurement (spent vs avoided). Doc-only; no feature change.
- **Knob-panel header copy.** The panel subtitle now reads "roster & pricing config · local
  spend-avoided rollup" (was a stale "read-only — cost-tiered router config …"; the panel has been
  editable since the pricing/roster knobs landed).

## [0.10.0] - 2026-06-17

First public release.

### Changed

- **Neutral positioning + local-only default roster (public-OSS rollout, R2a).** Reframed the project
  as a *local-first, config-driven router across OpenAI-compatible backends you own*. The packaged
  `config/roster.yaml` now ships **one active entry — the free local tier**; the subscription /
  authenticated-CLI tier (claude/codex/gemini) ships **commented out** as an opt-in example like the
  paid tier, so a fresh clone routes to local out of the box. README rewritten for newcomers (neutral
  headline, capability list, `--local`-first quickstart); new `ARCHITECTURE.md` (clean-room, neutral)
  and `DISCLAIMER.md` (subscription/CLI adapters are opt-in and your responsibility under each
  provider's ToS; paid tier is bring-your-own-key, off by default). `PackagedRosterTest` updated to
  the one-active-entry reality.
- **Generic shipped roster + external roster discovery.** The bundled `config/roster.yaml` is now a
  **generic example** (free local tier points at Ollama on `localhost:11434`, opt-in subscription-CLI
  entries, no maintainer infra). Your real roster lives **outside the repo** and is auto-discovered:
  `TANGLEBRAIN_ROSTER` env → `~/.config/tanglebrain/roster.yaml` (XDG) → the packaged example. So a
  `git pull` never clobbers your config, and the package ships nothing deployment-specific. The
  `--roster` flag still takes precedence. Part of the public-OSS rollout.

### Added

- **`tanglebrain --version`** prints the package version (from `tanglebrain.__version__`) and exits.
  Closes #29.
- **Contributor mechanics (public-OSS rollout, R2b).** `CONTRIBUTING.md` (dev setup via
  `make venv` / `make test`, branch & PR conventions, What/Why/Test-plan, and "adding a backend is a
  config edit" first-contribution framing), `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1), GitHub
  issue templates (`bug`, `feature`, `add a backend/adapter`), and a pull-request template with a
  What/Why/Test-plan body and a docs-updated checklist. README gained a Contributing section.
- `roster.packaged_roster_path()` (the bundled example) and `roster.default_roster_path()` discovery,
  mirroring the existing state-dir resolution pattern.

### Internal

- **Dropped local-tooling references from product files.** Neutralized cosmetic mentions of the
  local development tooling in `CHANGELOG.md`, `tanglebrain/gui/views.py`, `tanglebrain/gui/server.py`
  (the `--port` help text), and the `.gitignore` comment — they described the maintainer's local
  workflow, not the product. No behavior change.
- **Aligned code docstrings, comments, the CLI `--help` text, and shipped config comments with the
  project's documentation.** A consistency pass so the in-code descriptions match the
  README/ARCHITECTURE framing — the router is described as orchestrator rotation + failover for
  resilience — and a generic example hostname replaces a deployment-specific one in the tests.
  Docstrings/comments/strings only — no behavior change (verified by an AST-token structural diff).
  Closes #30. A GitHub Actions workflow
  (`.github/workflows/ci.yml`) runs `make test` (the hermetic suite) on every push to `main` and on
  pull requests, across Python 3.10/3.11/3.12. The `TANGLEBRAIN_LIVE`-gated tests stay skipped (CI has
  no backend). README gained a CI status badge. CI immediately surfaced a test-isolation gap — three
  `--model "claude"` CLI tests relied on the dev machine's ambient `~/.config/tanglebrain/roster.yaml`
  (the packaged example is local-only since R2a) — now fixed to pin a self-contained roster.
- Live e2e test (`tests/test_live.py`) pins the **direct-local** path (`run_once(..., local=True)`)
  and asserts it was served by the active roster's own local entry (roster-agnostic). Bare `run_once`
  has routed through the frontier-first router since the default flip, so the acceptance assertion had
  quietly stopped exercising the local path (#24). Test-only.

## [0.9.0] - 2026-06-17

### Added

- **Local classifier gate (plan §6 evolution path), off by default.** An optional cheap local
  classify can now run in front of the router: it rates each request's complexity using free local
  gpt-oss and sends **trivial** work straight to free local (skipping the rate-limited subs), while
  **frontier** work falls through to the normal frontier-first router. This preserves sub rate-limit
  runway when rotation alone isn't enough.
  - **Off by default** — new `classifier_gate_enabled` setting (`config/settings.yaml`, default
    `false`); per-run `--gate` / `--no-gate` override the setting. Built ahead of the §8 data trigger,
    so existing routing behaviour is unchanged until it's turned on.
  - **Fail-safe by design** — the classifier rates *task complexity* (not "can the local model do
    it?"), and any ambiguity, parse miss, or classifier error resolves to **frontier**, so the gate
    can never trap a hard task on the local tier. New `tanglebrain/classifier.py`; gated work is
    metered with `path=gate-local`.

## [0.8.0] - 2026-06-17

### Added

- **Editable roster in the knob panel (plan §5/§9.2).** The `tanglebrain-gui` roster card is now
  editable for a focused set of per-entry scalar fields — `enabled`, `can_orchestrate`,
  `budget_usd_month`, and `good_at` — each row with its own Save. Completes the deferred half of the
  C5 knob GUI (pricing became editable in C5b).
  - **Comment-preserving, zero new deps**: a new `tanglebrain/roster_edit.py` edits the targeted
    value on the targeted line *in place*, so every inline comment, blank line, the nested `invoke`
    block, and the commented paid-API example survive byte-for-byte — no YAML round-trip library.
    Adding/removing/reordering entries and editing the `invoke` block stay hand-edits (out of scope).
  - **Write-safety** mirrors C5b: edits are validated (and the candidate is **re-parsed with the real
    loader before any write**, so a surgical slip can never land a malformed roster), the prior file
    is backed up to `<state_dir>/backups/roster-<ts>.yaml`, and the write is atomic. The panel sends
    only changed fields and confirms before overwriting the tracked `config/roster.yaml`.
  - New `views.save_roster_view()` + `POST /api/roster`.

## [0.7.1] - 2026-06-16

### Fixed

- `tanglebrain.__version__` now derives from the installed package metadata
  (`importlib.metadata.version`) instead of a hardcoded literal, so it always tracks
  `pyproject.toml` and can no longer drift from the released version — it had been frozen at
  `0.1.0` since C1 while releases moved on to 0.7.0 (#17). Falls back to `0.0.0+unknown` when
  imported from an uninstalled source checkout.

## [0.7.0] - 2026-06-16

### Added

- **C6b — last-resort paid-API routing.** The frontier-first router can now fall through to a paid
  `tier: api` entry as a genuine last resort (plan §6): **only** after *every* `can_orchestrate` sub
  has failed/exhausted, and **only** when the `api_billing_enabled` gate is on. With the gate off
  (the default) the router never reaches a paid tier — behavior is unchanged. Part of #2.
  - Enabled `api` entries are tried in roster order; a paid success is surfaced on
    `Router.last_served` (so the run is metered `tier=api`, `spend_avoided=0`) but does **not**
    advance the orchestrator rotation cursor. Paid failures fail over to the next `api` entry and
    are listed in the `RouterError` with the same `[rate-limit]` annotation as orchestrators.
  - The router requires at least one orchestrator to be present — it never paid-routes a roster with
    no subs to exhaust (use `--model <id>` for an explicit paid call). `Router(... settings=)` is
    injectable; it defaults to the packaged `config/settings.yaml`.

- **C6c — paid-API visibility in the knob panel + runbook.** Closes #2. The `tanglebrain-gui` roster
  card now surfaces each entry's `enabled` kill-switch (a `disabled` pill) and `budget_usd_month`
  (a display-only `budget: $N/mo` note), and shows a **Paid-API billing: ON/OFF** banner from the
  global gate — so an operator never misreads a paid entry's own `enabled` flag as "live" when the
  global gate is off. New `view_settings()` view + `GET /api/settings` route (reads only
  `config/settings.yaml`; no key file touched). All read-only — per the v1 decision, TangleBrain
  does **not** meter or enforce spend; the hard budget cap stays LiteLLM-side on the virtual key.
  - README gains a step-by-step **runbook** for minting a budget-scoped LiteLLM virtual key on your
    LiteLLM gateway and wiring it via `key_ref`, plus how to pause spend (`enabled: false` or the gate).

## [0.6.0] - 2026-06-16

### Added

- **C6a — paid-API tier scaffolding (off by default).** A new `api` adapter and the global billing
  gate that guards it. A `tier: api` roster entry now parses fully but is **never routable** until
  it is explicitly enabled — preserving today's safe, zero-paid-spend default (issue #2).
  - **The gate**: new `tanglebrain/settings.py` + `config/settings.yaml` with `api_billing_enabled`
    (**default `false`**). A missing settings file defaults the gate *off*; a malformed one is a hard
    error (never a coincidental enable). `selector.build_adapter` builds an `api` entry only when the
    global gate **and** the entry's own `enabled` flag are both on, else raises clearly.
  - **The adapter**: `tanglebrain/adapters/api.py` `ApiAdapter` — paid APIs are LiteLLM-fronted, so
    it reuses the OpenAI-compat transport and references a scoped LiteLLM **virtual key** via
    `key_ref` (never a raw provider key, resolved lazily at call time).
  - **Roster fields**: `api` invoke now requires `base_url` + `model` + `key_ref`; new per-entry
    `enabled` (kill-switch, default `true`) and `budget_usd_month` (display-only in v1 — the hard cap
    is enforced LiteLLM-side on the virtual key). A commented example entry ships in `roster.yaml`.
  - Last-resort routing (wiring `api` into the router) is **not** in this change — that is C6b.

## [0.5.0] - 2026-06-16

### Added

- **C5b — editable pricing in the knob panel.** The panel's pricing card is now editable: change the
  input/output $/MTok, the reference-model label, and the placeholder flag, then **Save** to persist
  to `tanglebrain/config/pricing.yaml`. Closes #13.
  - **Write-safety**: strict validation before any write (rejects non-numeric/negative rates and an
    empty reference model — nothing is persisted on a bad value); the file is written **atomically**
    (temp + `os.replace`) and the prior version is **backed up** to `<state_dir>/backups/` first.
  - **Comment-preserving**: the canonical methodology header is re-emitted on every save, so GUI/
    programmatic edits never strip it — no new dependency. (Roster editing stays out — its dense
    inline comments need a comment-preserving mechanism, deferred to a later chunk.)
  - New `measurement.validate_pricing()` / `save_pricing()`; the panel writes the tracked repo config
    so an edit is git-visible and committed by the operator.

### Changed

- `cli.run_once()` gained an optional `return_served=True` that also returns the served
  `{path, tier, model}`. The knob panel uses it to report which tier handled a run **without
  re-reading the usage log** — removing the C5a best-effort race. Default behavior (returns a bare
  string) is unchanged.

## [0.4.0] - 2026-06-16

### Added

- **C5a — knob GUI (read-only panel), a simple dark-themed panel.** A new `tanglebrain-gui` console
  script serves a thin, **localhost-only** web panel (stdlib `http.server` + a single vanilla
  HTML/CSS/JS page — zero new runtime dependencies) on port 3250. The panel:
  views the live roster (§5), the pricing reference, and the local C4 spend-avoided rollup, and runs
  a prompt through the router (prompt in → final out), showing which tier/model served it (read from
  the C4 usage log; panel runs are metered automatically). First slice of plan §10's "C5 — Knob GUI".
  - **Read-only this chunk** — config editing (write-back to YAML) is deferred to C5b (#13).
  - **Secret-safety**: the roster view emits `key_ref` as the stored reference string only; it is
    never resolved and no key file is read, so no secret material reaches the browser.
  - Binds `127.0.0.1` only — the panel spends real sub rate-limit quota when it runs prompts and
    reads the roster, so it must not be network-exposed. New `tanglebrain/gui/` package; HTTP routing
    is a pure `dispatch()` over testable view functions (`tanglebrain/gui/views.py`).

## [0.3.0] - 2026-06-16

### Added

- **C4 — measurement / "spend avoided" rollup (plan §8).** Every routed task is now logged as one
  JSON line in an append-only usage log (`~/.cache/tanglebrain/usage.jsonl`, honoring
  `TANGLEBRAIN_STATE_DIR`): the execution path, tier, model, estimated tokens, and the
  cloud-equivalent cost it avoided. `tanglebrain --stats` rolls those records up into a
  "spend avoided" figure — what the work would have cost on a paid frontier API. Closes #10.
  - **Uniform token estimation**: CLI subs expose no usable token counts, so tokens are estimated
    with a single `chars/4` heuristic over the visible prompt + response, applied identically to
    every tier — one consistent (if approximate) methodology. No adapter or routing behavior change.
  - **Config-driven pricing** (`tanglebrain/config/pricing.yaml`) carrying a local pricing source's
    `costSaved` anchor — Claude Sonnet at $3/$15 per MTok (methodology ratified 2026-06-13) — so
    avoided spend is valued consistently. A `placeholder` flag (false by default) makes the
    rollup render a PLACEHOLDER caveat if the anchor is ever forked before re-ratifying.
  - **New module** `tanglebrain/measurement.py`; the router now exposes `Router.last_served` so the
    CLI metering seam can record which tier handled each task. All measurement I/O is
    fault-tolerant — a logging failure never affects the returned answer, and a corrupt log line
    never breaks the rollup.
  - **Scope**: meters top-level routed tasks only (the three `run_once` paths). The gpt-oss MCP
    delegate's sub-calls are intentionally not metered (they run inside an already-counted sub task).

## [0.2.0] - 2026-06-16

### Changed

- **C3b — frontier-first is now the default, and orchestrators offload grunt to free local
  (BEHAVIOR CHANGE).** `tanglebrain "prompt"` (no flags) now routes through the frontier-first
  router instead of going straight to the local tier; pass `--local` for the old direct-to-gpt-oss
  behavior, or `--model <id>` to pin an entry. Each orchestrator is now invoked with the C2b
  `delegate_local` tool available, so it decomposes the task and offloads sub-tasks to the free
  local backend — the offload behind frontier-first decompose (plan §6). Closes #7.
  - **Config-driven injection**: a new `invoke.delegate_args` roster field carries the per-CLI
    flags that register + allow the delegate, with `{delegate_mcp_json}` / `{delegate_mcp_command}`
    tokens substituted at runtime (the delegate runs as `python -m tanglebrain.mcp_server`, so it
    resolves without PATH assumptions). Adding/adjusting a CLI is a config edit (§5).
  - **Verified live, all three orchestrators delegate to gpt-oss**: claude (`--mcp-config` +
    `--allowedTools`, API key scrubbed), codex (`-c mcp_servers…` + approval bypass), gemini
    (after a one-time `gemini mcp add` + `--approval-mode yolo`). See the README for gemini's setup.

### Added

- **C3 — frontier-first router (control plane).** `tanglebrain/router.py` routes a task to a
  frontier sub acting as orchestrator, rotating the role across the `can_orchestrate` subs with
  automatic failover for resilience (plan §6).
  - **Task-fit selection**: a `--task <good_at-tag>` hint prefers orchestrators good at it (falling
    back to all when none match — a preference, not a gate). Auto-classification stays deferred
    (§6: "only if volume demands").
  - **Rotation**: round-robin across orchestrators, with the cursor **persisted across processes**
    (`~/.cache/tanglebrain/router-state.json`, override via `TANGLEBRAIN_STATE_DIR`) so successive
    `tanglebrain` invocations actually spread load. Missing/corrupt state resets to 0, never crashes.
  - **Failover**: on an `AdapterError` from one orchestrator, advance to the next; if all fail,
    raise `RouterError` naming each failure (rate-limit-looking ones are annotated `[rate-limit]`).
  - Exposed via `tanglebrain --route [--task <kind>]`. **The CLI default stays local-first** — the
    router becomes the default in C3b (#7), once the local-delegate is wired into orchestrator runs
    (routing whole tasks to subs without local offload would burn rate limits for no cost benefit).
  - Lives in its own module; the C1 selector stays minimal. Rotation/failover are proven by the
    hermetic suite (round-robin, wraparound, failover, persisted cursor); a gated live test
    confirms a real route returns text end-to-end.

- **C2b — gpt-oss MCP local-delegate.** `tanglebrain-delegate`, an MCP server exposing a single
  `delegate_local(prompt, max_tokens?)` tool, lets a frontier orchestrator (claude / codex /
  gemini) offload grunt work to the free local tier (gpt-oss-120b) at $0 — the mechanism behind
  frontier-first decompose (plan §6). Closes #4.
  - `tanglebrain/delegate.py`: `run_local_delegate(...)` — the routing logic, **reusing** C1's
    roster + `select_local` + `OpenAICompatAdapter` (no duplicated LiteLLM/endpoint/key logic).
    MCP-free so it stays hermetically testable; failures surface to the orchestrator (no retry).
  - `tanglebrain/mcp_server.py`: a thin `FastMCP` wrapper exposing the sync `delegate_local` tool
    (its docstring is the orchestrator-facing contract). Console entry `tanglebrain-delegate`
    serves over stdio. Verified end-to-end: a real MCP stdio client calls the tool and gets
    gpt-oss text back.
  - The `mcp` SDK is an **optional dependency** — `pip install "tanglebrain[delegate]"`; the core
    install stays lean (httpx + PyYAML). README documents per-CLI registration.

- **C2 — CLI adapters for the three subscription tools (claude / codex / gemini), with
  env-scrub.** The subscription tier is now invocable end-to-end through the uniform
  `run(prompt, opts) -> text` interface.
  - `CliAdapter` (`tanglebrain/adapters/cli.py`): runs a sub CLI as a subprocess (never via a
    shell) and returns its final text. Prompt injection is config-driven — a `{prompt}` token
    in the roster `cmd` is substituted (gemini's `-p {prompt}`), otherwise the prompt is
    appended as the final argument (claude, codex).
  - **Env-scrub (§7), the safety-critical piece:** `invoke.scrub_env` strips named vars from a
    *copy* of the environment handed to the subprocess (the parent `os.environ` is never
    mutated), so `claude -p` uses its own authenticated session rather than the injected
    `ANTHROPIC_API_KEY`. Proven by a live test: claude reports the key as `UNSET`.
  - Output parsers selected per entry via a new `invoke.parse` roster field: `claude-json`
    (single `{"result": ...}` object), `gemini-json` (`{"response": ...}`), and `plain`
    (stripped stdout, for codex `exec`). Parsers were written against real captured CLI output.
  - `AdapterError` promoted to `tanglebrain/adapters/base.py` so the openai-compat and CLI
    adapters and the routing layer share one error type (re-exported from `openai_compat` for
    backwards-compatible imports).
  - `selector.build_adapter` now builds the `cli` adapter; `selector.select_by_id` plus a new
    `tanglebrain --model <id>` flag let a named sub be driven end-to-end. This is an explicit
    override, **not** the §6 frontier-first router (still C3).
  - Roster `cmd` for claude switched from `stream-json` to `--output-format json` (a single
    parseable object). The gpt-oss MCP local-delegate (the other half of plan §10's C2 line)
    was split out to issue #4 (C2b), to land near C3 where it has a consumer.

## [0.1.0] - 2026-06-16

### Added

- **C1 — repo skeleton + roster loader + openai-compat adapter.** One request now routes to
  the free local tier (a local gpt-oss model via LiteLLM) end-to-end.
  - Python package skeleton (`tanglebrain/`), `pyproject.toml`, `Makefile`, and `tests/`
    following the project's conventions (stdlib `unittest`, venv-based test target, `make lint/test`).
  - Roster config loader (`tanglebrain/roster.py`): parses the YAML roster into typed
    objects. The roster is config-driven and open-ended — adding a model is an entry edit,
    not a code change. The starting roster is `gpt-oss-120b` + the three subscription CLIs.
  - `openai-compat` adapter (`tanglebrain/adapters/openai_compat.py`) with the uniform
    `run(prompt, opts) -> text` interface, calling the local LiteLLM endpoint directly.
    Returns only the final `content` (drops `reasoning_content`); defaults `max_tokens` to
    2048 per the C0 budget lesson. Resolves the scoped key via the contract's `key_ref`.
  - Local-first selector (`tanglebrain/selector.py`) and CLI entry point
    (`tanglebrain/cli.py`) wiring roster → local entry → adapter → text.
  - Brought the project's planning and design docs into this repo (the current architecture is
    documented in `ARCHITECTURE.md`).
  - Baseline hygiene files: `README`, `LICENSE`, `CHANGELOG`, `.gitignore`.

### Internal

- Resolved the two parked design decisions (PM, 2026-06-16; see issue #2 and plan §9.6–9.7):
  paid-API billing will be gated by an explicit `api_billing_enabled` flag (**default off**),
  with each paid key a `tier: api` roster entry carrying a per-key enable toggle + budget cap,
  **fronted through LiteLLM** (TangleBrain references a scoped virtual key — preferred over a raw
  provider key, which is not foreclosed but stays behind the toggle). Reconciled contract invariant
  #3 accordingly — it now *softens, not reverses* (the durable rule is *no paid billing without the
  explicit toggle*). No code behavior change yet; the paid-API tier itself is a later chunk (#2).
