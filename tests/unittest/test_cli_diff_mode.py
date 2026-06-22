import pytest

from pr_agent.cli import run, set_parser


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
