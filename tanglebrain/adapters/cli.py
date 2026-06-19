"""CLI adapter — the authenticated-CLI tier.

Runs a configured command-line tool (e.g. ``claude`` / ``codex`` / ``gemini``) as a subprocess and
returns its final text, behind the uniform :class:`~tanglebrain.adapters.base.Adapter` interface. The
routing layer never sees the per-CLI differences below — it hands over a prompt and gets text.

Two things vary per CLI and are config-driven from the roster, never hardcoded here:

- **How the prompt is passed.** A literal ``{prompt}`` token anywhere in the roster ``cmd`` is
  replaced with the prompt (e.g. gemini's ``-p {prompt}``); with no token the prompt is appended
  as the final argument (claude's ``-p ... <prompt>``, codex's ``exec <prompt>``). The prompt is
  passed through ``argv`` with **no shell** (``shell=True`` is never used), so it cannot be
  interpreted as shell syntax.
- **How the final text is extracted.** ``invoke.parse`` names a parser (:data:`PARSERS`):
  ``claude-json`` (single ``{"result": ...}`` object), ``gemini-json`` (``{"response": ...}``),
  or ``plain`` (stripped stdout — codex prints the answer to stdout, metadata to stderr).

The safety-critical piece is **env-scrub**: ``invoke.scrub_env`` names env vars stripped from the
subprocess environment, so a CLI runs against its own authenticated session rather than an injected
API key (e.g. ``claude -p`` without ``ANTHROPIC_API_KEY``). Scrubbing operates on a **copy** of the
environment — the parent process's ``os.environ`` is never mutated.

Like the openai-compat adapter, failures (non-zero exit, timeout, missing binary, unparseable
output) surface as :class:`~tanglebrain.adapters.base.AdapterError`. This layer never retries or
falls back — the routing layer decides what to do next.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Callable, Mapping

from tanglebrain.adapters.base import AdapterError
from tanglebrain.roster import RosterEntry

PROMPT_TOKEN = "{prompt}"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_PARSER = "plain"


def _parse_plain(stdout: str) -> str:
    """Return stripped stdout as the final text (codex ``exec`` and any plain-text CLI).

    Args:
        stdout: The subprocess's captured stdout.

    Returns:
        The stripped text.

    Raises:
        AdapterError: If stdout is empty/whitespace-only (no answer produced).
    """
    text = stdout.strip()
    if not text:
        raise AdapterError("CLI produced no stdout to parse as text")
    return text


def _parse_json_field(stdout: str, field: str, *, label: str) -> str:
    """Parse stdout as a single JSON object and return one string field.

    Args:
        stdout: The subprocess's captured stdout (expected to be one JSON object).
        field: The key whose value is the final text.
        label: Human label for the source CLI, used in error messages.

    Returns:
        The value at ``field``.

    Raises:
        AdapterError: If stdout is not valid JSON, is not an object, the field is missing, or
            the field's value is not a (non-empty) string.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{label}: stdout is not valid JSON: {exc}; got {stdout!r}") from exc
    if not isinstance(data, dict):
        raise AdapterError(f"{label}: expected a JSON object, got {type(data).__name__}")
    if field not in data:
        raise AdapterError(f"{label}: response JSON missing {field!r} field: {data!r}")
    value = data[field]
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"{label}: {field!r} is not non-empty text: {value!r}")
    return value


def _parse_claude_json(stdout: str) -> str:
    """Parse ``claude -p --output-format json`` output and return the result text.

    Claude emits a single JSON object with an ``is_error`` flag and the answer in ``result``.

    Args:
        stdout: The subprocess's captured stdout.

    Returns:
        The ``result`` text.

    Raises:
        AdapterError: If the JSON is malformed/unexpected, ``is_error`` is true, or ``result``
            is missing or not non-empty text.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"claude: stdout is not valid JSON: {exc}; got {stdout!r}") from exc
    if not isinstance(data, dict):
        raise AdapterError(f"claude: expected a JSON object, got {type(data).__name__}")
    if data.get("is_error"):
        raise AdapterError(
            f"claude reported an error (subtype={data.get('subtype')!r}): {data.get('result')!r}"
        )
    return _parse_json_field(stdout, "result", label="claude")


def _parse_gemini_json(stdout: str) -> str:
    """Parse ``gemini -p ... --output-format json`` output and return the response text.

    Gemini emits a single JSON object with the answer in ``response`` (alongside a ``stats``
    block that is intentionally ignored).

    Args:
        stdout: The subprocess's captured stdout.

    Returns:
        The ``response`` text.

    Raises:
        AdapterError: If the JSON is malformed/unexpected or ``response`` is missing/not text.
    """
    return _parse_json_field(stdout, "response", label="gemini")


#: Output parsers keyed by ``invoke.parse`` name. Adding a CLI with a new output shape = a new
#: entry here plus the name in the roster — the routing layer is unaffected.
PARSERS: dict[str, Callable[[str], str]] = {
    "plain": _parse_plain,
    "claude-json": _parse_claude_json,
    "gemini-json": _parse_gemini_json,
}


def scrubbed_env(scrub_env: list[str]) -> dict[str, str]:
    """Return a copy of the current environment with ``scrub_env`` names removed.

    This is the session-vs-key safety boundary: removing ``ANTHROPIC_API_KEY`` makes ``claude -p``
    run against its own authenticated session rather than an injected API key. The parent's
    ``os.environ`` is **not** mutated — only the returned copy (handed to the subprocess) is.

    Args:
        scrub_env: Env var names to remove. Names absent from the environment are ignored.

    Returns:
        A fresh ``dict`` of the environment minus the scrubbed names.
    """
    env = dict(os.environ)
    for name in scrub_env:
        env.pop(name, None)
    return env


def build_argv(cmd: list[str], prompt: str) -> list[str]:
    """Build the subprocess argv, injecting ``prompt`` into ``cmd``.

    If any ``cmd`` element contains the literal ``{prompt}`` token, the token is replaced (in
    place, in every element that contains it). Otherwise the prompt is appended as the final
    argument. No shell is involved, so the prompt is never interpreted as shell syntax.

    Args:
        cmd: The roster ``invoke.cmd`` argv template.
        prompt: The prompt to inject.

    Returns:
        The concrete argv to execute.
    """
    if any(PROMPT_TOKEN in part for part in cmd):
        return [part.replace(PROMPT_TOKEN, prompt) for part in cmd]
    return [*cmd, prompt]


class CliAdapter:
    """Adapter that runs prompts through a subscription CLI subprocess.

    Implements the uniform :class:`~tanglebrain.adapters.base.Adapter` interface
    (``run(prompt, opts) -> text``).
    """

    def __init__(
        self,
        cmd: list[str],
        parse: str | None = None,
        scrub_env: list[str] | None = None,
        delegate_args: list[str] | None = None,
        inject_delegate: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Configure the adapter.

        Args:
            cmd: The argv template (see :func:`build_argv` for ``{prompt}`` handling).
            parse: Name of the output parser (a key of :data:`PARSERS`). ``None`` uses
                :data:`DEFAULT_PARSER` (``plain``).
            scrub_env: Env var names to strip from the subprocess environment.
            delegate_args: Per-CLI flags that make the local-delegate tool available to this CLI
                as an orchestrator. A ``{delegate_mcp_json}`` token is substituted with the
                delegate's MCP-server JSON. Only applied when ``inject_delegate`` is true.
            inject_delegate: When true, append the (substituted) ``delegate_args`` to the command
                so the orchestrator can offload sub-tasks to the free local backend.
            timeout: Per-call subprocess timeout in seconds.

        Raises:
            AdapterError: If ``cmd`` is empty, or ``parse`` names an unknown parser.
        """
        if not cmd:
            raise AdapterError("CliAdapter requires a non-empty cmd")
        parser_name = parse or DEFAULT_PARSER
        if parser_name not in PARSERS:
            raise AdapterError(
                f"unknown parser {parser_name!r}; expected one of {sorted(PARSERS)}"
            )
        self.cmd = list(cmd)
        self.parser_name = parser_name
        self.scrub_env = list(scrub_env or [])
        self.delegate_args = list(delegate_args or [])
        self.inject_delegate = inject_delegate
        self.timeout = timeout

    @classmethod
    def from_entry(
        cls, entry: RosterEntry, inject_delegate: bool = False, **overrides: object
    ) -> "CliAdapter":
        """Build an adapter from a ``cli`` roster entry.

        Args:
            entry: A roster entry whose ``invoke.kind`` is ``cli``.
            inject_delegate: Make the local-delegate tool available to this CLI; honors the
                entry's ``invoke.delegate_args``.
            **overrides: Optional constructor overrides (``timeout``).

        Returns:
            A configured :class:`CliAdapter`.

        Raises:
            AdapterError: If the entry's invoke kind is not ``cli``, or ``cmd`` is missing.
        """
        if entry.invoke.kind != "cli":
            raise AdapterError(
                f"entry {entry.id!r} has invoke.kind {entry.invoke.kind!r}, not 'cli'"
            )
        if not entry.invoke.cmd:
            raise AdapterError(f"entry {entry.id!r}: cli invoke requires a non-empty cmd")
        return cls(
            cmd=entry.invoke.cmd,
            parse=entry.invoke.parse,
            scrub_env=entry.invoke.scrub_env,
            delegate_args=entry.invoke.delegate_args,
            inject_delegate=inject_delegate,
            **overrides,  # type: ignore[arg-type]
        )

    def _effective_cmd(self) -> list[str]:
        """Return the base ``cmd``, with substituted ``delegate_args`` appended when injecting.

        The delegate tokens (``{delegate_mcp_json}``, ``{delegate_mcp_command}``) in
        ``delegate_args`` are replaced via ``delegate_substitutions()`` (imported lazily so the
        mcp-free import graph is unaffected). Delegate flags land after the base command and before
        the prompt (added by :func:`build_argv`).

        Returns:
            The command with delegate flags applied, or just ``self.cmd`` when not injecting.
        """
        if not self.inject_delegate or not self.delegate_args:
            return self.cmd
        from tanglebrain.delegate import delegate_substitutions

        subs = delegate_substitutions()
        injected = []
        for arg in self.delegate_args:
            for token, value in subs.items():
                arg = arg.replace(token, value)
            injected.append(arg)
        return [*self.cmd, *injected]

    def run(self, prompt: str, opts: Mapping[str, object] | None = None) -> str:
        """Run the prompt through the CLI subprocess and return the final text.

        Args:
            prompt: The prompt to send.
            opts: Optional per-call options. Recognized key: ``timeout`` (float, seconds).
                Other keys are ignored (per the adapter contract).

        Returns:
            The CLI's final response text, per this adapter's parser.

        Raises:
            AdapterError: On a missing binary, non-zero exit, timeout, or unparseable output.
        """
        opts = opts or {}
        timeout = float(opts.get("timeout", self.timeout))
        argv = build_argv(self._effective_cmd(), prompt)
        env = scrubbed_env(self.scrub_env)

        # When this CLI is an orchestrator carrying the delegate tool, propagate the top-level task
        # id into its environment so the MCP delegate child it spawns can stamp each sub-call's
        # parent_task_id (linking the delegation tree across the process boundary). Gated on
        # inject_delegate: a leaf CLI call spawns no delegate child, so there is nothing to link.
        # Lazy import keeps the constant's home module (measurement) out of this adapter's import
        # graph, mirroring the lazy delegate_substitutions import in _effective_cmd.
        task_id = opts.get("task_id")
        if self.inject_delegate and task_id is not None:
            from tanglebrain.measurement import PARENT_TASK_ID_ENV

            env[PARENT_TASK_ID_ENV] = str(task_id)

        try:
            completed = subprocess.run(
                argv,
                # The prompt travels via argv (see build_argv); close stdin with EOF so a CLI
                # that probes stdin for "additional input" (e.g. codex) does not block.
                input="",
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdapterError(f"CLI binary not found: {argv[0]!r} ({exc})") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(
                f"CLI {argv[0]!r} timed out after {timeout}s"
            ) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise AdapterError(
                f"CLI {argv[0]!r} exited {completed.returncode}: {stderr or '(no stderr)'}"
            )

        return PARSERS[self.parser_name](completed.stdout)
