# tanglebrain-delegate (Claude Code plugin)

Wires [TangleBrain](https://github.com/Jason-Vaughan/TangleBrain)'s `tanglebrain-delegate` stdio
MCP server into Claude Code, so the orchestrator can offload self-contained sub-tasks — code
generation, drafting, extraction, summarization, bulk transforms — to a **free local model** (or
any configured `can_delegate` backend) and keep its own budget for decomposition and review.

Exposes four tools: `delegate_local`, `delegate`, `delegate_many`, `delegate_targets`. See the
[project README](https://github.com/Jason-Vaughan/TangleBrain#delegate-mcp--let-an-orchestrator-offload-sub-tasks-to-a-configured-backend)
for the full tool semantics and routing rules.

## Prerequisite

The plugin registers the server declaratively — it does **not** vendor the Python code. Install
TangleBrain with the delegate extra so the `tanglebrain-delegate` command is on your `PATH`
(TangleBrain is not on PyPI; install straight from GitHub, or from a clone with
`pip install -e ".[delegate]"`):

```sh
pip install "tanglebrain[delegate] @ git+https://github.com/Jason-Vaughan/TangleBrain"
```

The server reads the roster from `$TANGLEBRAIN_ROSTER` → `~/.config/tanglebrain/roster.yaml` → the
packaged example, in that order; copy the example to the user-config path and point it at your
backends.

## Install

```
/plugin marketplace add Jason-Vaughan/TangleBrain
/plugin install tanglebrain-delegate@tanglebrain
```

Equivalent manual registration (no plugin): `claude mcp add tanglebrain-delegate -- tanglebrain-delegate`.

## Troubleshooting

If the prerequisite is missing, the server shows as **failed** in `/mcp` (Claude Code spawns
`tanglebrain-delegate` and gets command-not-found) — the plugin itself installs fine either way.
Check `which tanglebrain-delegate` in the same environment Claude Code runs from; if it's absent,
run the pip install above. If the command exists but tools error at call time, the roster is the
next suspect: run `tanglebrain-delegate` by hand and call `delegate_targets` to see the live error.
