from pr_agent.commands.pr_help_docs import format_markdown_q_and_a_response, map_documentation_files_to_contents


def test_nested_doc_keys_and_source_links_use_forward_slashes(tmp_path):
    document = tmp_path / "docs" / "usage" / "guide.md"
    document.parent.mkdir(parents=True)
    document.write_text("# Setup\n\nRead the instructions.\n", encoding="utf-8")

    contents = map_documentation_files_to_contents(str(tmp_path), [str(document)])

    assert list(contents) == ["/docs/usage/guide.md"]
    # The model answers with the file names it was shown, so the key is what ends up in the source link.
    answer = format_markdown_q_and_a_response(
        "How do I set up?",
        "Read the guide.",
        [{"file_name": next(iter(contents)), "relevant_section_header_string": "Setup"}],
        [".md"],
        "https://example.com/repo/blob/main",
    )
    assert "https://example.com/repo/blob/main/docs/usage/guide.md#setup" in answer
    assert "\\" not in answer
