from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    PERSISTENT_MODE_UPDATE,
    VALID_PERSISTENT_MODES,
    append_marker,
    build_marker_index,
    extract_marker,
    generate_marker,
    normalize_persistent_mode,
)


def _suggestion(file="src/app.py", label="possible issue",
                content="Nullable pointer may crash on line 42 when user_id is None",
                start=10, end=12):
    return {
        "relevant_file": file,
        "label": label,
        "suggestion_content": content,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
    }


class TestGenerateMarker:
    def test_shape(self):
        marker = generate_marker(_suggestion())
        assert marker.startswith(MARKER_PREFIX)
        assert marker.endswith(MARKER_SUFFIX)
        hash_part = marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
        assert len(hash_part) == 12
        assert all(c in "0123456789abcdef" for c in hash_part)

    def test_deterministic(self):
        assert generate_marker(_suggestion()) == generate_marker(_suggestion())

    def test_stable_across_line_shifts(self):
        a = generate_marker(_suggestion(start=10, end=12))
        b = generate_marker(_suggestion(start=200, end=202))
        assert a == b

    def test_changes_with_file(self):
        a = generate_marker(_suggestion(file="src/app.py"))
        b = generate_marker(_suggestion(file="src/other.py"))
        assert a != b

    def test_changes_with_label(self):
        a = generate_marker(_suggestion(label="possible issue"))
        b = generate_marker(_suggestion(label="security"))
        assert a != b

    def test_changes_with_content_prefix(self):
        a = generate_marker(_suggestion(content="A totally different suggestion about X"))
        b = generate_marker(_suggestion(content="Another totally different suggestion about Y"))
        assert a != b

    def test_tolerates_trailing_content_variation(self):
        long_base = "Same opening 128-chars " + "x" * 200
        a = generate_marker(_suggestion(content=long_base + "tail-A"))
        b = generate_marker(_suggestion(content=long_base + "tail-B"))
        assert a == b

    def test_whitespace_normalized(self):
        a = generate_marker(_suggestion(content="Same   content  here"))
        b = generate_marker(_suggestion(content="Same content here"))
        assert a == b

    def test_missing_fields_returns_none(self):
        assert generate_marker({"relevant_file": "a.py"}) is None
        assert generate_marker({}) is None


class TestExtractMarker:
    def test_present(self):
        body = "some text\n<!-- pr-agent-inline-id:abc123def456 -->"
        assert extract_marker(body) == "abc123def456"

    def test_missing(self):
        assert extract_marker("no marker here") is None

    def test_empty(self):
        assert extract_marker("") is None

    def test_multiple_returns_last(self):
        body = "<!-- pr-agent-inline-id:000000000001 -->\nmore\n<!-- pr-agent-inline-id:000000000002 -->"
        assert extract_marker(body) == "000000000002"

    def test_roundtrip_with_append(self):
        marker = generate_marker(_suggestion())
        body_plus = append_marker("suggestion body", marker)
        assert extract_marker(body_plus) == marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]


class TestAppendMarker:
    def test_adds_separator(self):
        body = append_marker("hello", "<!-- pr-agent-inline-id:abcabcabcabc -->")
        assert body.endswith("<!-- pr-agent-inline-id:abcabcabcabc -->")
        assert "hello\n\n<!--" in body

    def test_idempotent_when_already_marked(self):
        marker = "<!-- pr-agent-inline-id:abcabcabcabc -->"
        once = append_marker("hello", marker)
        twice = append_marker(once, marker)
        assert once == twice


class TestBuildMarkerIndex:
    def test_indexes_marked_comments(self):
        comments = [
            {"id": 1, "body": "body A <!-- pr-agent-inline-id:aaaaaaaaaaaa -->"},
            {"id": 2, "body": "body B <!-- pr-agent-inline-id:bbbbbbbbbbbb -->"},
        ]
        index = build_marker_index(comments)
        assert [comment["id"] for comment in index["aaaaaaaaaaaa"]] == [1]
        assert [comment["id"] for comment in index["bbbbbbbbbbbb"]] == [2]

    def test_ignores_unmarked(self):
        comments = [{"id": 1, "body": "no marker"}]
        assert build_marker_index(comments) == {}

    def test_duplicate_hashes_are_preserved(self):
        comments = [
            {"id": 1, "body": "A <!-- pr-agent-inline-id:aaaaaaaaaaaa -->"},
            {"id": 2, "body": "B <!-- pr-agent-inline-id:aaaaaaaaaaaa -->"},
        ]
        index = build_marker_index(comments)
        assert [comment["id"] for comment in index["aaaaaaaaaaaa"]] == [1, 2]


class TestNormalizePersistentMode:
    def test_valid_values(self):
        assert normalize_persistent_mode("off") == PERSISTENT_MODE_OFF
        assert normalize_persistent_mode("update") == PERSISTENT_MODE_UPDATE
        assert normalize_persistent_mode("skip") == PERSISTENT_MODE_SKIP

    def test_case_and_whitespace(self):
        assert normalize_persistent_mode("  UPDATE  ") == PERSISTENT_MODE_UPDATE

    def test_invalid_falls_back_to_off(self):
        assert normalize_persistent_mode("garbage") == PERSISTENT_MODE_OFF
        assert normalize_persistent_mode(None) == PERSISTENT_MODE_OFF
        assert normalize_persistent_mode("") == PERSISTENT_MODE_OFF

    def test_valid_set_exposed(self):
        assert VALID_PERSISTENT_MODES == {PERSISTENT_MODE_OFF, PERSISTENT_MODE_UPDATE, PERSISTENT_MODE_SKIP}


from pr_agent.algo.inline_comments_dedup import normalize_code


class TestNormalizeCode:
    def test_empty_inputs(self):
        assert normalize_code("") == ""
        assert normalize_code(None) == ""
        assert normalize_code("   \n  \n") == ""

    def test_reindent_produces_same_output(self):
        a = (
            "        cleanup_mode=cleanup_mode,\n"
        )
        b = (
            "    cleanup_mode=cleanup_mode,\n"
        )
        assert normalize_code(a) == normalize_code(b)

    def test_multiline_reindent_produces_same_output(self):
        a = (
            "        bump_version(\n"
            "            github=self.github,\n"
            "            cleanup_mode=None if dry_run else cleanup_mode,\n"
            "        )\n"
        )
        b = (
            "    bump_version(\n"
            "        github=self.github,\n"
            "        cleanup_mode=None if dry_run else cleanup_mode,\n"
            "    )\n"
        )
        assert normalize_code(a) == normalize_code(b)

    def test_trailing_whitespace_stripped(self):
        assert normalize_code("foo = 1   \n") == normalize_code("foo = 1\n")

    def test_leading_and_trailing_blank_lines_dropped(self):
        assert normalize_code("\n\nfoo = 1\n\n\n") == normalize_code("foo = 1")

    def test_tabs_expand_consistently(self):
        tabbed = "\tfoo = 1\n\tbar = 2\n"
        spaced = "        foo = 1\n        bar = 2\n"
        assert normalize_code(tabbed) == normalize_code(spaced)

    def test_token_difference_preserved(self):
        assert normalize_code("cleanup_mode=None if dry_run else cleanup_mode") != \
               normalize_code("cleanup_mode=cleanup_mode if not dry_run else None")

    def test_is_idempotent(self):
        sample = "    x = f(1, 2)\n    y = g(3)\n"
        once = normalize_code(sample)
        twice = normalize_code(once)
        assert once == twice

    def test_internal_whitespace_in_string_literal_is_preserved(self):
        assert normalize_code('message = "a  b"') != normalize_code('message = "a b"')


def _structured_suggestion(
    file="src/app.py",
    content="some prose",
    improved_code="cleanup_mode=None if dry_run else cleanup_mode,",
    label="possible issue",
    start=10,
    end=12,
):
    return {
        "relevant_file": file,
        "label": label,
        "suggestion_content": content,
        "improved_code": improved_code,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
    }


class TestGenerateMarkerStructured:
    def test_paraphrased_prose_same_edit_collides(self):
        a = _structured_suggestion(
            content="When dry_run=True, cleanup_mode is still passed through unchanged to bump_version.",
        )
        b = _structured_suggestion(
            content="When dry_run=True, the cleanup_mode is still forwarded unchanged to bump_version.",
        )
        assert generate_marker(a) == generate_marker(b)

    def test_same_prose_different_edit_splits(self):
        a = _structured_suggestion(
            improved_code="cleanup_mode=None if dry_run else cleanup_mode,",
        )
        b = _structured_suggestion(
            improved_code="cleanup_mode=cleanup_mode if not dry_run else None,",
        )
        assert generate_marker(a) != generate_marker(b)

    def test_reindented_edit_collides(self):
        a = _structured_suggestion(
            improved_code="        cleanup_mode=None if dry_run else cleanup_mode,\n",
        )
        b = _structured_suggestion(
            improved_code="    cleanup_mode=None if dry_run else cleanup_mode,\n",
        )
        assert generate_marker(a) == generate_marker(b)

    def test_label_change_does_not_split_when_structured(self):
        a = _structured_suggestion(label="possible issue")
        b = _structured_suggestion(label="best practice")
        assert generate_marker(a) == generate_marker(b)

    def test_missing_file_returns_none(self):
        s = _structured_suggestion()
        s["relevant_file"] = ""
        assert generate_marker(s) is None

    def test_empty_improved_code_falls_back_to_prose(self):
        s = _structured_suggestion(improved_code="")
        # With prose + label present, fallback produces a marker.
        assert generate_marker(s) is not None

    def test_fallback_missing_label_returns_none(self):
        s = _structured_suggestion(improved_code="", label="")
        assert generate_marker(s) is None

    def test_fallback_missing_content_returns_none(self):
        s = _structured_suggestion(improved_code="", content="")
        s["suggestion_content"] = ""
        assert generate_marker(s) is None

    def test_structured_and_prose_differ_on_same_inputs(self):
        # Same file/label/content; structured extra input shouldn't alias to prose hash.
        structured = _structured_suggestion(
            improved_code="x = 1",
            content="x = 1",
            label="possible issue",
        )
        prose_only = _structured_suggestion(
            improved_code="",
            content="x = 1",
            label="possible issue",
        )
        assert generate_marker(structured) != generate_marker(prose_only)

    def test_same_hash_can_be_reused_for_multiple_locations(self):
        first = _structured_suggestion(start=10, end=12)
        second = _structured_suggestion(start=40, end=42)
        assert generate_marker(first) == generate_marker(second)
