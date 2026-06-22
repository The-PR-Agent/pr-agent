from pr_agent.cli import set_parser


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
