from pr_agent.tools.pr_help_docs import PRHelpDocs


def test_parse_args_empty():
    assert PRHelpDocs._parse_args(None) is None
    assert PRHelpDocs._parse_args([]) is None


def test_parse_args_single():
    assert PRHelpDocs._parse_args(["/help"]) == "/help"


def test_parse_args_multiword():
    assert PRHelpDocs._parse_args(["how", "to", "run", "tests?"]) == "how to run tests?"
