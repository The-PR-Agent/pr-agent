"""Skip automatic or CLI /review and /describe runs when the PR has not changed since they last succeeded.

Each command keeps its own record, written only after it succeeds, as hidden markers in one comment
PR-Agent authored. A run is skipped when the head commit and target branch name match the record, or when the
diff fingerprint does: a rebase that changes no content moves the head but keeps the diff, and a CI re-run
after the target branch moved keeps the head but can shift the diff context.
"""
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

from pr_agent.algo.comment_identity import PRCommandNoticeIdentity, comment_authorship_available
from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider, commit_patch_fingerprint
from pr_agent.log import get_logger

_IDENTITY = PRCommandNoticeIdentity.UNCHANGED_DIFF.value
_RECORD_RE = re.compile(r"<!-- pr-agent:unchanged-diff:([a-z_]+) ([0-9a-f]{64}) ([0-9a-f]{7,64}|-) (\S*) -->")
_SKIPPED_RE = re.compile(r"<!-- pr-agent:unchanged-diff-skipped:([a-z_]+) (commit|diff) -->")
_SKIP_REASONS = {
    "commit": "the head commit and target branch name have not changed since its last successful run",
    "diff": "the diff has not changed since its last successful run",
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


def parse_unchanged_diff_comment(body: str) -> tuple[dict[str, RunState], dict[str, str]]:
    """Return the per-command records and skip notices held in the comment body."""
    lines = body.splitlines()
    records, skipped = {}, {}
    if lines[:1] != [_IDENTITY]:
        return records, skipped
    for line in lines[1:]:
        if match := _RECORD_RE.fullmatch(line):
            head = "" if match[3] == "-" else match[3]
            records[match[1]] = RunState(diff_hash=match[2], head_sha=head, target_branch=unquote(match[4]))
        elif match := _SKIPPED_RE.fullmatch(line):
            skipped[match[1]] = match[2]
    return records, skipped


def _render(records: dict[str, RunState], skipped: dict[str, str]) -> str:
    lines = [
        _IDENTITY,
        "",
        ("`skip_unchanged_diff` is on: PR-Agent does not run an automatic `/review` or `/describe` again when "
         "the PR has not changed since that command last succeeded."),
        "",
        *(format_unchanged_diff_marker(tool, records[tool]) for tool in sorted(records)),
    ]
    for tool in sorted(skipped):
        lines += [
            "",
            f"<!-- pr-agent:unchanged-diff-skipped:{tool} {skipped[tool]} -->",
            f"**`/{tool}` skipped:** {_SKIP_REASONS[skipped[tool]]}, so its earlier output was kept. "
            f"Comment `/{tool}` to run it again.",
        ]
    return "\n".join(lines)


def _diff_fingerprint(files) -> str:
    # Hunk line numbers are left out: a rebase onto a target that gained lines above a change moves them.
    return commit_patch_fingerprint((f"{f.old_filename or ''}\0{f.filename or ''}", f.patch or "") for f in files)


def skip_unchanged_diff_configured() -> bool:
    value = get_settings().config.get("skip_unchanged_diff", False)
    return _as_bool_setting(value, setting_name="config.skip_unchanged_diff")


def _setting(section: str, key: str) -> bool:
    value = get_settings().get(section, {}).get(key, False)
    return _as_bool_setting(value, setting_name=f"{section}.{key}")


def _output_tied_to_head_commit(tool: str) -> bool:
    # A check run, verdict status or approval belongs to one head commit; a skip would leave it on the
    # previous head, or close the check run as a success without a verdict.
    if _setting("github", "publish_as_check_run"):
        return True
    return tool == "review" and (
        _setting("config", "enable_auto_approval") or _setting("pr_reviewer", "publish_merge_recommendation_status")
    )


def _record_usable(git_provider) -> bool:
    if not skip_unchanged_diff_configured() or not get_settings().config.get("publish_output", True):
        return False
    return comment_authorship_available(git_provider) and git_provider.supports_html_comment_markers() is not False


def _may_skip(tool: str) -> bool:
    config = get_settings().config
    # A command someone typed in a comment always runs.
    if not (config.get("is_auto_command", False) or config.get("cli_mode", False)):
        return False
    return not _output_tied_to_head_commit(tool)


class UnchangedDiffGuard:
    """Decide whether one command can be skipped, and record its state once it succeeds."""

    def __init__(self, tool: str, git_provider):
        self.tool = tool
        self.git_provider = git_provider
        self._state: RunState | None = None
        self._diff_comparable = False

    def should_skip(self) -> bool:
        # A run that may not skip still reads the record: it takes down its own notice and records on success.
        if not _record_usable(self.git_provider):
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
            reason = self._skip_reason(records.get(self.tool)) if _may_skip(self.tool) else None
            # A run takes its own old notice down first, so a run that then fails does not leave
            # the PR saying the last output still applies.
            had_notice = skipped.pop(self.tool, None) is not None
            if reason:
                skipped[self.tool] = reason
            if reason or had_notice:
                self._publish(records, skipped)
            if reason:
                get_logger().info(f"Skipping /{self.tool}: {_SKIP_REASONS[reason]}")
            return bool(reason)
        except Exception as e:
            get_logger().warning(f"Could not check whether /{self.tool} can be skipped, running it: {e}")
            return False

    def record(self, *, replaced_output: bool = True) -> None:
        if self._state is None:
            if replaced_output:
                self.invalidate()
            return
        try:
            records, skipped = self._read()
            records[self.tool] = self._state
            skipped.pop(self.tool, None)
            self._publish(records, skipped)
        except Exception as e:
            get_logger().warning(f"Could not record the /{self.tool} run, the next run will not be skipped: {e}")

    def invalidate(self) -> None:
        try:
            if not _record_usable(self.git_provider):
                return
            records, skipped = self._read()
            had_record = records.pop(self.tool, None) is not None
            had_notice = skipped.pop(self.tool, None) is not None
            if had_record or had_notice:
                self._publish(records, skipped)
        except Exception as e:
            get_logger().warning(f"Could not clear the /{self.tool} record: {e}")

    def _skip_reason(self, stored: RunState | None) -> str | None:
        if stored is None or self._state is None:
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
            return parse_unchanged_diff_comment(body)
        return {}, {}

    def _publish(self, records: dict[str, RunState], skipped: dict[str, str]) -> None:
        # Another command's notice says its last output still applies; drop it once that is no longer true.
        skipped = {
            tool: reason
            for tool, reason in skipped.items()
            if tool in records and (self._state is None or self._skip_reason(records.get(tool)))
        }
        self.git_provider.publish_persistent_comment_full(
            _render(records, skipped), initial_header=_IDENTITY, update_header=False,
            final_update_message=False, identity_marker=_IDENTITY, require_agent_authorship=True,
            fallback_on_error=False)
