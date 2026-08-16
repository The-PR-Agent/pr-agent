"""AI handler that delegates completions to a locally installed coding-agent CLI
(Claude Code's `claude`, or Codex's `codex exec`) instead of an HTTP endpoint.

Why a subprocess handler rather than an `api_base` override: neither CLI exposes an
HTTP chat-completions endpoint, so LiteLLM has nothing to talk to. Both do have a
non-interactive mode that reads a prompt and prints one final answer, which is exactly
the shape `BaseAiHandler.chat_completion` needs.

Selection is by model string: "cli/claude/<model>" or "cli/codex/<model>", e.g.
`config.model = "cli/claude/opus"`. The trailing segment is passed to the CLI's
--model flag verbatim; omit it ("cli/claude") to let the CLI pick its own default.
Because these names are absent from MAX_TOKENS in pr_agent/algo/__init__.py, a
positive `config.custom_model_max_tokens` is REQUIRED or get_max_tokens() raises.

The prompt goes to the child's stdin, never argv: PR diffs routinely exceed the
platform argv limit (~256 KB on macOS) and would fail with E2BIG.

Both backends are invoked with their agentic capabilities restricted as far as each CLI
allows, and with the working directory set to an empty temp dir. PR-Agent puts the whole
diff in the prompt and wants one block of text back; letting the agent explore the
filesystem or run commands would add latency and non-determinism, not context.
"""
import asyncio
import json
import os
import re
import shutil
import tempfile
from typing import Optional, Tuple

from tenacity import (retry, retry_if_exception_type,
                      retry_if_not_exception_type, stop_after_attempt,
                      wait_exponential)

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.run_details import record_ai_call
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

MODEL_PREFIX = "cli/"
SUPPORTED_BACKENDS = ("claude", "codex")

DEFAULT_TIMEOUT_SECONDS = 600
CLI_AGENT_RETRIES = 2

# Tools are disabled rather than merely unpermitted: an agent that decides to grep the
# filesystem mid-review burns wall-clock and can only find context the prompt already has.
_CLAUDE_DISALLOWED_TOOLS = (
    "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite"
)

# PR-Agent's prompts already specify their output format exactly (usually strict YAML).
# The CLIs are tuned for conversational replies, so restate the contract; without this
# they tend to open with "Here's the review:" and load_yaml() then fails on the preamble.
_OUTPUT_CONTRACT = (
    "You are running as a non-interactive text generator inside an automated pipeline. "
    "Return only the content the instructions above ask for. Do not add any preamble, "
    "explanation, summary, or commentary around it. Do not wrap the response in a "
    "markdown code fence unless the instructions explicitly ask for one. Do not use "
    "tools; everything you need is already in the prompt."
)

# Used to recover from a model that fenced its structured answer anyway. Deliberately
# limited to structured languages so free-prose answers (/ask) are never rewritten.
_STRUCTURED_FENCE_RE = re.compile(
    r"```[ \t]*(?:yaml|yml|json|toml)[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE
)


def _diagnostic_tail(stderr: str, limit: int = 500) -> str:
    """Best line(s) from stderr for an error message.

    Codex echoes the whole prompt to stderr, so a plain tail slice reports diff fragments
    instead of the failure. Prefer lines that announce an error and fall back to the tail.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    flagged = [ln for ln in lines if re.search(r"\b(error|fatal|panic)\b", ln, re.IGNORECASE)]
    chosen = "\n".join(dict.fromkeys(flagged)) if flagged else "\n".join(lines)
    return chosen[-limit:]


class CliAgentError(Exception):
    """A CLI invocation failed in a way that may succeed on retry."""


class CliAgentConfigError(CliAgentError):
    """Misconfiguration (unknown backend, binary not on PATH). Retrying cannot help."""


def parse_cli_model(model: str) -> Tuple[str, Optional[str]]:
    """Split "cli/<backend>[/<model>]" into (backend, model_or_None).

    Raises CliAgentConfigError for anything this handler should not have received, so a
    typo surfaces as a clear message instead of a silent fallback to a different backend.
    """
    raw = (model or "").strip()
    if not raw.startswith(MODEL_PREFIX):
        raise CliAgentConfigError(
            f"Model '{model}' is not a CLI-agent model; expected 'cli/<backend>[/<model>]'"
        )
    remainder = raw[len(MODEL_PREFIX):]
    backend, _, sub_model = remainder.partition("/")
    backend = backend.strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        raise CliAgentConfigError(
            f"Unsupported CLI backend '{backend}' in model '{model}'. "
            f"Supported: {', '.join(SUPPORTED_BACKENDS)}"
        )
    return backend, (sub_model.strip() or None)


def _settings_value(key: str, default):
    return get_settings().get(f"CLI_AGENT.{key}", default)


def _resolve_binary(backend: str) -> str:
    """Absolute path to the backend's executable.

    Resolved up front so a missing CLI reports itself here rather than as a bare
    FileNotFoundError from create_subprocess_exec.
    """
    configured = _settings_value(f"{backend.upper()}_BINARY", None) or backend
    resolved = shutil.which(configured)
    if not resolved:
        raise CliAgentConfigError(
            f"CLI backend '{backend}' requested but '{configured}' was not found on PATH. "
            f"Install it, or set cli_agent.{backend}_binary to its absolute path."
        )
    return resolved


def _extra_args() -> list:
    """Caller-supplied passthrough flags, appended after ours so they can override."""
    extra = _settings_value("EXTRA_ARGS", []) or []
    if isinstance(extra, str):
        extra = extra.split()
    return [str(a) for a in extra]


def _claude_argv(binary: str, model: Optional[str], system: str) -> list:
    """`claude --print` with a replaced system prompt and its agent surface switched off.

    --system-prompt REPLACES the default Claude Code system prompt, which is what we want:
    the tool's own prompt is the whole instruction set. The output contract is concatenated
    in rather than passed via --append-system-prompt, because that flag appends to the
    *default* prompt and its interaction with --system-prompt is unspecified.

    --safe-mode disables CLAUDE.md, skills, plugins, hooks and MCP servers while leaving
    auth untouched (unlike --bare, which forces ANTHROPIC_API_KEY and would break the
    OAuth/subscription login this handler exists to reuse).
    """
    argv = [
        binary,
        "--print",
        "--output-format", "json",
        "--system-prompt", f"{system}\n\n{_OUTPUT_CONTRACT}",
        "--safe-mode",
        "--no-session-persistence",
        "--disallowed-tools", _CLAUDE_DISALLOWED_TOOLS,
    ]
    if model:
        argv += ["--model", model]
    return argv + _extra_args()


def _codex_argv(binary: str, model: Optional[str], out_file: str) -> list:
    """`codex exec` reading the prompt from stdin ("-") and writing its final message to a file.

    Codex has no system-prompt flag, so the caller folds `system` into the prompt body.
    --output-last-message is used instead of parsing --json JSONL: it yields exactly the
    final assistant message, with no event-schema coupling.

    --ignore-user-config is the counterpart to claude's --safe-mode: it keeps runs hermetic
    rather than inheriting whatever ~/.codex/config.toml pins for interactive use (a model
    the account cannot serve there fails every PR-Agent call here). Auth still resolves via
    CODEX_HOME, so the CLI's own login is unaffected. Set
    cli_agent.codex_ignore_user_config=false to keep that config, e.g. when it defines a
    custom provider endpoint.
    """
    argv = [
        binary,
        "exec",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "--output-last-message", out_file,
    ]
    if _settings_value("CODEX_IGNORE_USER_CONFIG", True):
        argv.append("--ignore-user-config")
    if model:
        argv += ["--model", model]
    return argv + _extra_args() + ["-"]


def _unwrap_structured_output(text: str) -> str:
    """Return the body of a lone yaml/json/toml fence when the model added prose around it.

    Conservative by construction: it acts only when exactly one structured fence is present
    AND there is other non-whitespace text, so a clean response and a multi-block prose
    answer are both returned untouched.
    """
    if not _settings_value("UNWRAP_STRUCTURED_OUTPUT", True):
        return text
    matches = _STRUCTURED_FENCE_RE.findall(text)
    if len(matches) != 1:
        return text
    outside = _STRUCTURED_FENCE_RE.sub("", text).strip()
    if not outside:
        return text
    get_logger().debug("cli_agent: unwrapped fenced structured output from surrounding prose")
    return matches[0]


def _parse_claude_output(stdout: str) -> Tuple[str, str, dict]:
    """Extract (text, finish_reason, usage) from `claude --print --output-format json`.

    Accepts both the single result object and a list of messages, since the shape has
    varied across CLI versions; anything else is treated as a retryable failure.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise CliAgentError(f"claude returned non-JSON output: {e}") from e

    if isinstance(data, list):
        results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"]
        if not results:
            raise CliAgentError("claude JSON output contained no result message")
        data = results[-1]
    if not isinstance(data, dict):
        raise CliAgentError(f"Unexpected claude JSON output of type {type(data).__name__}")

    if data.get("is_error"):
        raise CliAgentError(f"claude reported an error: {data.get('result') or data.get('subtype')}")

    text = data.get("result") or ""
    # error_max_turns is reported as a subtype rather than is_error; surface it as a
    # truncation so callers see the same signal an API length-stop would give.
    finish_reason = "length" if data.get("subtype") == "error_max_turns" else "stop"

    raw_usage = data.get("usage") or {}
    usage = {
        "prompt_tokens": (
            raw_usage.get("input_tokens", 0)
            + raw_usage.get("cache_creation_input_tokens", 0)
            + raw_usage.get("cache_read_input_tokens", 0)
        ),
        "completion_tokens": raw_usage.get("output_tokens", 0),
    }
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return text, finish_reason, usage


class CliAgentAIHandler(BaseAiHandler):
    """Runs completions through a local `claude` or `codex` CLI process."""

    def __init__(self):
        super().__init__()
        self.timeout_seconds = int(_settings_value("TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))

    @property
    def deployment_id(self):
        """No deployment concept for a local CLI; present to satisfy BaseAiHandler."""
        return None

    @retry(
        retry=retry_if_exception_type(CliAgentError)
        & retry_if_not_exception_type(CliAgentConfigError),
        stop=stop_after_attempt(CLI_AGENT_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def chat_completion(self, model: str, system: str, user: str,
                              temperature: float = 0.2, img_path: str = None):
        if img_path:
            get_logger().warning(f"cli_agent does not support images; ignoring {img_path}")

        backend, sub_model = parse_cli_model(model)
        binary = _resolve_binary(backend)
        get_logger().info(
            f"cli_agent: invoking {backend}",
            model=sub_model or "(cli default)", system_chars=len(system), user_chars=len(user),
        )

        # temperature has no equivalent flag on either CLI. Logged rather than silently
        # dropped so a config that relies on it is visible in the run log.
        if temperature is not None:
            get_logger().debug(f"cli_agent: temperature={temperature} is not supported by {backend}")

        if backend == "claude":
            text, finish_reason, usage = await self._run_claude(binary, sub_model, system, user)
        else:
            text, finish_reason, usage = await self._run_codex(binary, sub_model, system, user)

        text = _unwrap_structured_output((text or "").strip())
        if not text:
            raise CliAgentError(f"{backend} returned an empty response")

        get_logger().info("AI response", response=text, finish_reason=finish_reason,
                          model=model, usage=usage)
        record_ai_call(usage)
        return text, finish_reason

    async def _run_claude(self, binary, sub_model, system, user):
        argv = _claude_argv(binary, sub_model, system)
        stdout, _ = await self._exec(argv, stdin_text=user)
        return _parse_claude_output(stdout)

    async def _run_codex(self, binary, sub_model, system, user):
        # Codex has no system-prompt channel, so the roles are flattened into one prompt.
        prompt = f"{system}\n\n{_OUTPUT_CONTRACT}\n\n{user}"
        with tempfile.TemporaryDirectory(prefix="pr-agent-codex-") as tmpdir:
            out_file = os.path.join(tmpdir, "last_message.txt")
            argv = _codex_argv(binary, sub_model, out_file)
            stdout, _ = await self._exec(argv, stdin_text=prompt, cwd=tmpdir)
            try:
                with open(out_file, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                # The file is only written on a completed turn, so a missing file means
                # the run ended without producing an answer. stdout carries the transcript.
                raise CliAgentError(
                    f"codex produced no final message ({e}); "
                    f"transcript tail: {_diagnostic_tail(stdout)}"
                ) from e
        # Codex reports no usage totals on this path; the run-details renderer omits
        # token lines when they stay at zero, so an empty dict is the honest value.
        return text, "stop", {}

    async def _exec(self, argv: list, stdin_text: str, cwd: Optional[str] = None):
        """Run argv to completion with stdin_text on stdin. Returns (stdout, stderr) as str.

        An empty temp cwd is the default so the agent never has the user's repo (or its
        AGENTS.md/CLAUDE.md) as ambient context.
        """
        owns_tmpdir = cwd is None
        tmpdir = tempfile.TemporaryDirectory(prefix="pr-agent-cli-") if owns_tmpdir else None
        workdir = tmpdir.name if owns_tmpdir else cwd
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env=os.environ.copy(),
                )
            except OSError as e:
                raise CliAgentConfigError(f"Failed to start {argv[0]}: {e}") from e

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(stdin_text.encode("utf-8")), timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError as e:
                proc.kill()
                await proc.wait()
                raise CliAgentError(
                    f"{argv[0]} timed out after {self.timeout_seconds}s "
                    f"(raise cli_agent.timeout_seconds if the model needs longer)"
                ) from e

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                raise CliAgentError(
                    f"{argv[0]} exited with code {proc.returncode}: {_diagnostic_tail(stderr)}"
                )
            return stdout, stderr
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()
