"""Skip automatic /review and /describe runs when the PR has not changed since they last succeeded.

Each command keeps its own record, written only after it succeeds, as hidden markers in one comment
PR-Agent authored. A run is skipped when the head commit and target branch match the record, or when the
diff fingerprint does: a rebase that changes no content moves the head but keeps the diff, and a CI re-run
after the target branch moved keeps the head but can shift the diff context.
"""
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

from pr_agent.algo.comment_identity import PRCommandNoticeIdentity, comment_authorship_available
from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger

_IDENTITY = PRCommandNoticeIdentity.UNCHANGED_DIFF.value
_RECORD_RE = re.compile(r"<!-- pr-agent:unchanged-diff:([a-z_]+) ([0-9a-f]{64}) ([0-9a-f]{7,64}|-) (\S*) -->")
_SKIPPED_RE = re.compile(r"<!-- pr-agent:unchanged-diff-skipped:([a-z_]+) (commit|diff) -->")
_SKIP_REASONS = {
    "commit": "the head commit and target branch have not changed since its last run",
    "diff": "the diff has not changed since its last run",
}


@dataclass(frozen=True)
class RunState:
    diff_hash: str
    head_sha: str
    target_branch: str


def format_unchanged_diff_marker(tool: str, state: RunState) -> str:
    # Quoting keeps a branch name such as "a-->b" from closing the HTML comment early.
    target = quote(state.target_branch, safe="")
    return f"<!-- pr-agent:unchanged-diff:{tool} {state.diff_hash} {state.head_sha or '-'} {target} -->"


def parse_unchanged_diff_records(body) -> dict[str, RunState]:
    lines = body.splitlines() if isinstance(body, str) else []
    if lines[:1] != [_IDENTITY]:
        return {}
    records = {}
    for line in lines[1:]:
        if match := _RECORD_RE.fullmatch(line):
            head = "" if match[3] == "-" else match[3]
            records[match[1]] = RunState(diff_hash=match[2], head_sha=head, target_branch=unquote(match[4]))
    return records


def _parse_skipped(body: str) -> dict[str, str]:
    return {match[1]: match[2] for line in body.splitlines() if (match := _SKIPPED_RE.fullmatch(line))}


def _render(records: dict[str, RunState], skipped: dict[str, str]) -> str:
    lines = [
        _IDENTITY,
        "",
        ("PR-Agent skips an automatic `/review` or `/describe` run when the PR has not changed since that "
         "command last ran."),
        "",
        *(format_unchanged_diff_marker(tool, records[tool]) for tool in sorted(records)),
    ]
    for tool in sorted(skipped):
        lines += [
            "",
            f"<!-- pr-agent:unchanged-diff-skipped:{tool} {skipped[tool]} -->",
            f"**`/{tool}` skipped:** {_SKIP_REASONS[skipped[tool]]}, so its last output still applies. "
            f"Comment `/{tool}` to run it again.",
        ]
    return "\n".join(lines)


def _diff_fingerprint(files) -> str:
    hasher = hashlib.sha256()
    for file in sorted(files, key=lambda f: f.filename or ""):
        for field in (file.old_filename, file.filename, file.patch):
            hasher.update((field or "").encode("utf-8"))
            hasher.update(b"\x00")
    return hasher.hexdigest()


def skip_unchanged_diff_configured() -> bool:
    value = get_settings().config.get("skip_unchanged_diff", False)
    return _as_bool_setting(value, setting_name="config.skip_unchanged_diff")


def _enabled(git_provider) -> bool:
    config = get_settings().config
    if not skip_unchanged_diff_configured():
        return False
    # A command someone typed in a comment always runs.
    if not (config.get("is_auto_command", False) or config.get("cli_mode", False)):
        return False
    if not config.get("publish_output", True) or not comment_authorship_available(git_provider):
        return False
    return getattr(git_provider, "supports_html_comment_markers", lambda: True)() is not False


class UnchangedDiffGuard:
    """Decide whether one command can be skipped, and record its state once it succeeds."""

    def __init__(self, tool: str, git_provider):
        self.tool = tool
        self.git_provider = git_provider
        self._state: RunState | None = None
        self._diff_comparable = False

    def should_skip(self) -> bool:
        if not _enabled(self.git_provider):
            return False
        try:
            files = self.git_provider.get_diff_files()
            if not files:
                return False
            target = self.git_provider.get_request_policy_metadata(set()).get("target_branch") or ""
            self._state = RunState(_diff_fingerprint(files), self.git_provider.get_pr_head_sha() or "", target)
            # A file without a patch (binary, or content the provider could not read) can change without
            # changing the fingerprint.
            self._diff_comparable = all(file.patch for file in files)
            records, skipped = self._read()
            reason = self._skip_reason(records.get(self.tool))
            if reason or self.tool in skipped:
                # A run takes its own old notice down first, so a run that then fails does not leave
                # the PR saying the last output still applies.
                skipped.pop(self.tool, None)
                if reason:
                    skipped[self.tool] = reason
                self._publish(records, skipped)
        except Exception as e:
            get_logger().warning(f"Could not check whether /{self.tool} can be skipped, running it: {e}")
            return False
        if reason:
            get_logger().info(f"Skipping /{self.tool}: {_SKIP_REASONS[reason]}")
        return bool(reason)

    def record(self) -> None:
        if self._state is None or not _enabled(self.git_provider):
            return
        try:
            records, skipped = self._read()
            records[self.tool] = self._state
            skipped.pop(self.tool, None)
            self._publish(records, skipped)
        except Exception as e:
            get_logger().warning(f"Could not record the /{self.tool} run, the next run will not be skipped: {e}")

    def _skip_reason(self, stored: RunState | None) -> str | None:
        if stored is None:
            return None
        current = self._state
        if (current.head_sha and current.target_branch
                and current.head_sha == stored.head_sha and current.target_branch == stored.target_branch):
            return "commit"
        if self._diff_comparable and current.diff_hash == stored.diff_hash:
            return "diff"
        return None

    def _read(self) -> tuple[dict[str, RunState], dict[str, str]]:
        for _comment, body in GitProvider._iter_persistent_comments(
            self.git_provider, [_IDENTITY], identity_marker=_IDENTITY, require_agent_authorship=True
        ):
            return parse_unchanged_diff_records(body), _parse_skipped(body)
        return {}, {}

    def _publish(self, records: dict[str, RunState], skipped: dict[str, str]) -> None:
        # Another command's notice says its last output still applies; drop it once that is no longer true.
        skipped = {tool: reason for tool, reason in skipped.items()
                   if tool == self.tool or self._skip_reason(records.get(tool))}
        self.git_provider.publish_persistent_comment_full(
            _render(records, skipped), initial_header=_IDENTITY, update_header=False,
            final_update_message=False, identity_marker=_IDENTITY, require_agent_authorship=True,
            fallback_on_error=False)
