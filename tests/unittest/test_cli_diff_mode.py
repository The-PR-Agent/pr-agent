import pytest

from pr_agent.cli import run, set_parser
from pr_agent.config_loader import get_settings

# Keys the run()-based test mutates on the process-wide settings singleton;
# saved and restored around every test so global state never leaks.
_SETTINGS_KEYS = ["diff.content", "diff.output_path",
                  "config.git_provider", "config.publish_output"]


@pytest.fixture(autouse=True)
def _restore_settings():
    s = get_settings()
    saved = {k: s.get(k, None) for k in _SETTINGS_KEYS}
    yield
    for k, v in saved.items():
        s.set(k, v)


def test_parser_has_diff_flags():
    parser = set_parser()
    args = parser.parse_args(["--diff-file", "x.diff", "--output", "out.md", "review"])
    assert args.diff_file == "x.diff"
    assert args.output == "out.md"
    assert args.command == "review"


def test_parser_stdin_flag():
    parser = set_parser()
    args = parser.parse_args(["--stdin", "review"])
    assert args.stdin is True


def test_missing_diff_file_fails_fast(tmp_path, capsys):
    """A non-existent --diff-file must exit cleanly via parser.error (SystemExit)
    with a clear message, not crash with an uncaught OSError traceback."""
    missing = tmp_path / "does-not-exist.diff"
    with pytest.raises(SystemExit):
        run(inargs=["--diff-file", str(missing), "review"])
    err = capsys.readouterr().err
    assert "Could not read --diff-file" in err


_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n-line2\n+line2-changed\n line3\n"
)


def test_diff_mode_forces_publish_output(monkeypatch):
    """Diff mode must force config.publish_output=True so stdout/--output is
    never suppressed by a config/env that disabled publishing."""
    import io

    import pr_agent.cli as cli

    get_settings().set("config.publish_output", False)
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["publish_output"] = get_settings().config.publish_output
            return True

    monkeypatch.setattr(cli, "PRAgent", FakeAgent)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    run(inargs=["--stdin", "review"])
    assert captured["publish_output"] is True
