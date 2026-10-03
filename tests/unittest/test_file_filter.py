import fnmatch

import pytest

from pr_agent.algo import file_filter
from pr_agent.algo.file_filter import filter_ignored, translate_globs_to_regexes
from pr_agent.config_loader import global_settings
from pr_agent.log import get_logger


def _capture_logs(call):
    import io

    buffer = io.StringIO()
    handler_id = get_logger().add(buffer, level='DEBUG', format='{message}', colorize=False)
    try:
        call()
    finally:
        get_logger().remove(handler_id)
    return buffer.getvalue()


def _capture_errors(call):
    return [line for line in _capture_logs(call).splitlines() if 'Could not filter file list' in line]


def _capture_warnings(call):
    return [
        line for line in _capture_logs(call).splitlines()
        if 'No ignore filtering is implemented for platform' in line
    ]


class _BitbucketSide:
    def __init__(self, path):
        self.path = path


class _BitbucketDiffstat:
    def __init__(self, new_path, old_path):
        self.new = _BitbucketSide(new_path)
        self.old = _BitbucketSide(old_path)


def _gitlab_change(new_path, old_path):
    return {'new_path': new_path, 'old_path': old_path, 'diff': 'diff --git a/x b/x'}


class TestIgnoreFilter:
    def test_no_ignores(self):
        """
        Test no files are ignored when no patterns are specified.
        """
        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        assert filter_ignored(files) == files, "Expected all files to be returned when no ignore patterns are given."

    def test_glob_ignores(self, monkeypatch):
        """
        Test files are ignored when glob patterns are specified.
        """
        monkeypatch.setattr(global_settings.ignore, 'glob', ['*.py'])

        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        expected = [
            files[1],
            files[2]
        ]

        filtered_files = filter_ignored(files)
        assert filtered_files == expected, (
            f"Expected {[file.filename for file in expected]}, "
            f"but got {[file.filename for file in filtered_files]}."
        )

    def test_glob_ignores_dict_values(self, monkeypatch):
        """Verify ignore filtering for GitHub incremental dict_values views."""
        monkeypatch.setattr(global_settings.ignore, 'glob', ['*.py'])

        files = [
            type('', (object,), {'filename': 'ignored.py'})(),
            type('', (object,), {'filename': 'kept.java'})(),
        ]
        incremental_files = {file.filename: file for file in files}.values()

        assert filter_ignored(incremental_files) == [files[1]]

    @pytest.mark.parametrize(
        ("pattern", "filename", "ignored"),
        [
            # '**' also matches zero directories, so the flattened form has to be matched too
            ("src/**/generated_*.py", "src/generated_pb.py", True),
            ("src/**/generated_*.py", "src/api/generated_pb.py", True),
            ("src/**/generated_*.py", "src/api/deep/generated_pb.py", True),
            ("src/**/generated_*.py", "src/handwritten.py", False),
            ("src/**/generated_*.py", "other/generated_pb.py", False),
            # a leading globstar keeps matching files at the repository root
            ("**/vendor/**", "vendor/lib.py", True),
            ("**/vendor/**", "third_party/vendor/lib.py", True),
            ("**/vendor/**", "third_party/vendored/lib.py", False),
            # every globstar collapses, so the fully flattened form is matched as well
            ("**/a/**/b.py", "a/b.py", True),
            ("**/a/**/b.py", "x/a/y/b.py", True),
            # each globstar collapses on its own, so partial combinations are matched too
            ("a/**/x/**/b.py", "a/x/y/b.py", True),
            ("a/**/x/**/b.py", "a/y/x/b.py", True),
            ("a/**/x/**/b.py", "a/x/y/z/b.py", True),
            ("a/**/x/**/b.py", "a/y/x/z/b.py", True),
            ("a/**/x/**/b.py", "a/x/b.py", True),
            ("a/**/x/**/b.py", "b/x/a/b.py", False),
            # a '**' that is not a whole path segment stays an ordinary '*'
            ("generated**/schema.py", "generatedXschema.py", False),
            # the collapsed form a wrong scan would emit, which ignores this file
            ("generated**/schema.py", "generatedschema.py", False),
            ("generated**/schema.py", "generated_proto/schema.py", True),
            # likewise a '**' inside a bracket expression, which is a literal
            ("a[**/]/**/b.py", "a/b.py", False),
        ],
    )
    def test_globstar_patterns_also_match_zero_directories(self, monkeypatch, pattern, filename, ignored):
        monkeypatch.setattr(global_settings.ignore, 'glob', [pattern])
        monkeypatch.setattr(global_settings.ignore, 'regex', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

        files = [type('', (object,), {'filename': filename})()]

        assert filter_ignored(files) == ([] if ignored else files)

    @pytest.mark.parametrize(
        "pattern",
        ['*.py', 'vendor/**', 'src/generated_*.py', 'a[b/c]*', 'generated**/schema.py'],
    )
    def test_pattern_without_a_globstar_segment_keeps_fnmatch_semantics(self, pattern):
        """Without a `**/` segment the translation stays exactly what fnmatch builds."""
        assert translate_globs_to_regexes([pattern]) == [fnmatch.translate(pattern)]

    @pytest.mark.parametrize(
        ("pattern", "expected_variants"),
        [
            ('src/**/generated_*.py', ['src/generated_*.py']),
            ('**/vendor/**', ['vendor/**']),
            ('**/a/**/b.py', ['**/a/b.py', 'a/**/b.py', 'a/b.py']),
            ('a/**/x/**/b.py', ['a/**/x/b.py', 'a/x/**/b.py', 'a/x/b.py']),
            ('**/**/a.py', ['**/a.py', 'a.py']),
        ],
    )
    def test_globstar_combinations_become_separate_patterns(self, pattern, expected_variants):
        """Each way the globstars can match zero directories is translated on its own."""
        regexes = translate_globs_to_regexes([pattern])

        assert regexes[0] == fnmatch.translate(pattern)
        assert sorted(regexes[1:]) == sorted(fnmatch.translate(v) for v in expected_variants)

    @pytest.mark.parametrize(
        ("pattern", "expected_offsets"),
        [
            ('src/**/generated_*.py', [4]),
            ('**/a/**/b.py', [0, 5]),
            # an embedded '**' is an ordinary '*', so the separator after it is not dropped
            ('generated**/schema.py', []),
            # a '**' inside a bracket expression is a literal, the one after it is a globstar
            ('a[**/]/**/b.py', [7]),
            ('[**/]x.py', []),
            # fnmatch reads a ']' in first position as a member, so the class closes later
            ('[]a/**/]src/*.py', []),
            ('[!]a/**/]src/*.py', []),
            # a '!' anywhere but first position is an ordinary member
            ('[a]!**/**/x.py', [7]),
            # a '!' first position skips the member, so ']' closes the expression
            ('[!]]/**/x.py', [5]),
            # fnmatch reads an unterminated '[' as a literal and keeps parsing after it, and every
            # '[' of a pattern with no closing bracket at all is unterminated
            ('a[b/**/c.py', [4]),
            ('[' * 40 + '/**/x.py', [41]),
            # ...while a later ']' closes the expression, so the '**/' inside it stays literal
            ('a[b/**/c.py]', []),
        ],
    )
    def test_globstar_offsets_ignore_embedded_and_bracketed_globstars(self, pattern, expected_offsets):
        assert file_filter._globstar_offsets(pattern) == expected_offsets

    def test_too_many_globstars_keep_fnmatch_translation_and_are_reported(self):
        pattern = 'a/**/b/**/c/**/d/**/e/**/f/**/g/**/h/x.py'
        assert len(file_filter._globstar_offsets(pattern)) > file_filter._MAX_ENUMERATED_GLOBSTARS

        logs = _capture_logs(lambda: translate_globs_to_regexes([pattern]))

        assert translate_globs_to_regexes([pattern]) == [fnmatch.translate(pattern)]
        assert 'zero-directory globstar variants' in logs

    def test_too_many_globstars_keep_the_root_level_form(self):
        """Over the limit, a glob still matches at the repository root as it did before."""
        pattern = '**/a/**/b/**/c/**/d/**/e/**/f/**/g/**/h/x.py'
        assert len(file_filter._globstar_offsets(pattern)) > file_filter._MAX_ENUMERATED_GLOBSTARS

        logs = _capture_logs(lambda: translate_globs_to_regexes([pattern]))

        assert translate_globs_to_regexes([pattern]) == [
            fnmatch.translate(pattern),
            fnmatch.translate(pattern[len('**/'):]),
        ]
        assert 'plus its root-level form' in logs

    def test_a_glob_that_collapses_to_matching_everything_is_reported(self):
        """`**/**/**` flattens to `**`, so the operator is told nothing will be analyzed."""
        pattern = '**/**/**'
        expected = sorted({fnmatch.translate(variant) for variant in [pattern, '**', '**/**']})

        logs = _capture_logs(lambda: translate_globs_to_regexes([pattern]))

        assert sorted(translate_globs_to_regexes([pattern])) == expected
        assert 'matches every file' in logs

    @pytest.mark.parametrize(
        ('glob', 'filename'),
        [
            # a glob at the limit still matches the shallowest file and a deeper one
            ('**/**/**/**/**/**/x.py', 'x.py'),
            ('**/**/**/**/**/**/x.py', 'a/b/c/x.py'),
            ('**/a/**/b/**/c/**/d/**/e/x.py', 'a/b/c/d/e/x.py'),
            ('**/a/**/b/**/c/**/d/**/e/x.py', 'z/a/b/c/d/e/x.py'),
            # six globstars is the last count that gets enumerated, so the fully collapsed form of
            # a glob with no leading '**/' exists at the limit and not one globstar above it
            ('a/**/b/**/c/**/d/**/e/**/f/**/x.py', 'a/b/c/d/e/f/x.py'),
        ],
    )
    def test_a_glob_at_the_globstar_limit_still_matches(self, monkeypatch, glob, filename):
        monkeypatch.setattr(global_settings.ignore, 'glob', [glob])
        monkeypatch.setattr(global_settings.ignore, 'regex', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

        files = [type('', (object,), {'filename': filename})()]

        assert filter_ignored(files) == []

    @pytest.mark.parametrize('glob', ['[]a/**/]src/*.py', '[!]a/**/]src/*.py', 'a[**/]/b.py'])
    def test_a_globstar_inside_a_bracket_expression_adds_no_regex(self, glob):
        """A `**/` inside a bracket expression is literal, so it must not produce a variant."""
        assert translate_globs_to_regexes([glob]) == [fnmatch.translate(glob)]

    def test_regex_ignores(self, monkeypatch):
        """
        Test files are ignored when regex patterns are specified.
        """
        monkeypatch.setattr(global_settings.ignore, 'regex', ['^file[2-4]\..*$'])

        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        expected = [
            files[0],
            files[4]
        ]

        filtered_files = filter_ignored(files)
        assert filtered_files == expected, (
            f"Expected {[file.filename for file in expected]}, "
            f"but got {[file.filename for file in filtered_files]}."
        )

    def test_invalid_regex(self, monkeypatch):
        """
        Test invalid patterns are quietly ignored.
        """
        monkeypatch.setattr(global_settings.ignore, 'regex', ['(((||', '^file[2-4]\..*$'])

        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        expected = [
            files[0],
            files[4]
        ]

        filtered_files = filter_ignored(files)
        assert filtered_files == expected, (
            f"Expected {[file.filename for file in expected]}, "
            f"but got {[file.filename for file in filtered_files]}."
        )

    def test_language_framework_ignores(self, monkeypatch):
        """
        Test files are ignored based on language/framework mapping (e.g., protobuf).
        """
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', ['protobuf', 'go_gen'])

        files = [
            type('', (object,), {'filename': 'main.go'})(),
            type('', (object,), {'filename': 'dir1/service.pb.go'})(),
            type('', (object,), {'filename': 'dir1/dir/data_pb2.py'})(),
            type('', (object,), {'filename': 'file.py'})(),
            type('', (object,), {'filename': 'dir2/file_gen.go'})(),
            type('', (object,), {'filename': 'file.generated.go'})()
        ]
        expected = [
            files[0],
            files[3]
        ]

        filtered = filter_ignored(files)
        assert filtered == expected, (
            f"Expected {[f.filename for f in expected]}, "
            f"but got {[f.filename for f in filtered]}"
        )

    def test_skip_invalid_ignore_language_framework(self, monkeypatch):
        """
        Test skipping of generated code filtering when ignore_language_framework is not a list
        """
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', 'protobuf')

        files = [
            type('', (object,), {'filename': 'main.go'})(),
            type('', (object,), {'filename': 'file.py'})(),
            type('', (object,), {'filename': 'dir1/service.pb.go'})(),
            type('', (object,), {'filename': 'file_pb2.py'})()
        ]
        expected = [
            files[0],
            files[1],
            files[2],
            files[3]
        ]

        filtered = filter_ignored(files)
        assert filtered == expected, (
            f"Expected {[f.filename for f in expected]}, "
            f"but got {[f.filename for f in filtered]}"
        )

    def test_repeated_filtering_does_not_mutate_regex_settings(self, monkeypatch):
        """Ensure repeated filtering does not append translated glob patterns to shared settings."""
        configured_regex = ['^docs/']
        monkeypatch.setattr(global_settings.ignore, 'regex', configured_regex)
        monkeypatch.setattr(global_settings.ignore, 'glob', ['vendor/**'])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

        files = [
            type('', (object,), {'filename': 'src/app.py'})(),
            type('', (object,), {'filename': 'vendor/generated.py'})(),
        ]

        for _ in range(3):
            filtered = filter_ignored(files)
            assert filtered == [files[0]]

        assert configured_regex == ['^docs/']


class TestRenameFiltering:
    """A rename names one file by a destination and a source path.

    The destination decides, the way providers label the file. Keeping the entry
    because its other path does not match lets a rename into an ignored path
    reach the model, and matching only the first available path with no
    destination to fall back on drops nothing it should keep.
    """

    @staticmethod
    def _ignore(monkeypatch, regex):
        monkeypatch.setattr(global_settings.ignore, 'regex', regex)
        monkeypatch.setattr(global_settings.ignore, 'glob', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

    def test_gitlab_rename_into_ignored_path_is_ignored(self, monkeypatch):
        self._ignore(monkeypatch, [r'.*\.lock$'])

        renamed_in = _gitlab_change('poetry.lock', 'notes.txt')
        untouched = _gitlab_change('src/app.py', 'src/app.py')
        ignored = _gitlab_change('yarn.lock', 'yarn.lock')

        assert filter_ignored([renamed_in, untouched, ignored], platform='gitlab') == [untouched]

    def test_gitlab_rename_out_of_ignored_path_is_kept(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/'])

        renamed_out = _gitlab_change('docs/app.yaml', 'secrets/app.yaml')
        untouched = _gitlab_change('docs/readme.md', 'docs/readme.md')

        assert filter_ignored([renamed_out, untouched], platform='gitlab') == [renamed_out, untouched]

    def test_gitlab_rename_falls_back_to_source_path(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/'])

        no_destination = _gitlab_change('', 'secrets/app.yaml')
        no_destination_kept = _gitlab_change(None, 'src/app.py')

        kept = filter_ignored([no_destination, no_destination_kept], platform='gitlab')

        assert kept == [no_destination_kept]

    def test_gitlab_added_file_is_ignored_by_destination_path(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/'])

        added = _gitlab_change('secrets/app.yaml', '')
        added_outside = _gitlab_change('src/app.py', '')

        assert filter_ignored([added, added_outside], platform='gitlab') == [added_outside]

    def test_gitlab_entry_without_any_path_is_ignored(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/'])

        pathless = {'diff': 'diff --git a/x b/x'}
        untouched = _gitlab_change('src/app.py', 'src/app.py')

        assert filter_ignored([pathless, untouched], platform='gitlab') == [untouched]

    def test_bitbucket_rename_into_ignored_path_is_ignored(self, monkeypatch):
        self._ignore(monkeypatch, [r'.*\.pem$'])

        renamed_in = _BitbucketDiffstat('id_rsa.pem', 'notes.txt')
        untouched = _BitbucketDiffstat('src/app.py', 'src/app.py')
        ignored = _BitbucketDiffstat('id_rsa.pem', 'id_rsa.pem')

        assert filter_ignored([renamed_in, untouched, ignored], platform='bitbucket') == [untouched]

    def test_bitbucket_rename_out_of_ignored_path_is_kept(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/'])

        renamed_out = _BitbucketDiffstat('docs/app.yaml', 'secrets/app.yaml')
        untouched = _BitbucketDiffstat('docs/readme.md', 'docs/readme.md')

        assert filter_ignored([renamed_out, untouched], platform='bitbucket') == [renamed_out, untouched]

    def test_bitbucket_rename_falls_back_to_source_path(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/'])

        no_destination = _BitbucketDiffstat(None, 'secrets/app.yaml')
        no_destination_kept = _BitbucketDiffstat(None, 'src/app.py')

        kept = filter_ignored([no_destination, no_destination_kept], platform='bitbucket')

        assert kept == [no_destination_kept]

    def test_bitbucket_entry_without_any_path_is_ignored(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/'])

        pathless = _BitbucketDiffstat(None, None)
        untouched = _BitbucketDiffstat('src/app.py', 'src/app.py')

        assert filter_ignored([pathless, untouched], platform='bitbucket') == [untouched]

    def test_rename_between_unignored_paths_is_kept(self, monkeypatch):
        self._ignore(monkeypatch, [r'^secrets/', r'.*\.lock$'])

        renamed = _gitlab_change('src/renamed.py', 'src/original.py')
        other_renamed = _BitbucketDiffstat('src/renamed.py', 'src/original.py')

        assert filter_ignored([renamed], platform='gitlab') == [renamed]
        assert filter_ignored([other_renamed], platform='bitbucket') == [other_renamed]

    def test_rename_is_filtered_against_every_pattern(self, monkeypatch):
        """Each pattern tests the chosen path, so a later pattern can still match."""
        self._ignore(monkeypatch, [r'^vendor/', r'.*_generated\.py$'])

        renamed = _gitlab_change('src/api_generated.py', 'src/api.py')
        untouched = _gitlab_change('src/app.py', 'src/app.py')

        assert filter_ignored([renamed, untouched], platform='gitlab') == [untouched]


class TestMultiplePatterns:
    """Every pattern has to run, including after an earlier one drops entries.

    A file is matched against one path per entry, and each pass removes entries,
    so the file and its path must stay paired across passes. When a pass shortened
    the file list without shortening the path list, the next pass raised and the
    remaining patterns never ran, leaving later matches in the result.
    """

    @staticmethod
    def _ignore(monkeypatch, regex):
        monkeypatch.setattr(global_settings.ignore, 'regex', regex)
        monkeypatch.setattr(global_settings.ignore, 'glob', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

    def test_gitlab_later_pattern_still_applies_after_an_earlier_one_drops_entries(self, monkeypatch):
        self._ignore(monkeypatch, [r'^vendor/', r'.*_generated\.py$', r'^secrets/'])

        dropped_first = _gitlab_change('vendor/lib.py', 'vendor/lib.py')
        dropped_second = _gitlab_change('src/api_generated.py', 'src/api.py')
        dropped_third = _gitlab_change('secrets/app.yaml', 'secrets/app.yaml')
        untouched = _gitlab_change('src/app.py', 'src/app.py')

        files = [dropped_first, dropped_second, dropped_third, untouched]

        assert filter_ignored(list(files), platform='gitlab') == [untouched]

    def test_bitbucket_later_pattern_still_applies_after_an_earlier_one_drops_entries(self, monkeypatch):
        self._ignore(monkeypatch, [r'^vendor/', r'.*_generated\.py$', r'^secrets/'])

        dropped_first = _BitbucketDiffstat('vendor/lib.py', 'vendor/lib.py')
        dropped_second = _BitbucketDiffstat('src/api_generated.py', 'src/api.py')
        dropped_third = _BitbucketDiffstat('secrets/app.yaml', 'secrets/app.yaml')
        untouched = _BitbucketDiffstat('src/app.py', 'src/app.py')

        files = [dropped_first, dropped_second, dropped_third, untouched]

        assert filter_ignored(list(files), platform='bitbucket') == [untouched]

    def test_renamed_entry_survives_earlier_pattern_passes(self, monkeypatch):
        """A rename that an earlier pattern drops must not shift later matches.

        The entry before the rename in the list is what an earlier pattern removes;
        if its removal shifts the path list relative to the file list, the rename
        that a later pattern should drop is left in the result.
        """
        self._ignore(monkeypatch, [r'^vendor/', r'.*\.pem$'])

        dropped_first = _gitlab_change('vendor/lib.py', 'vendor/lib.py')
        dropped_later = _gitlab_change('id_rsa.pem', 'notes.txt')
        untouched = _gitlab_change('src/app.py', 'src/app.py')

        assert filter_ignored([dropped_first, dropped_later, untouched], platform='gitlab') == [untouched]

    def test_no_filter_error_is_logged_across_pattern_passes(self, monkeypatch):
        """A mismatch between the file and path lists must surface, not be swallowed."""
        self._ignore(monkeypatch, [r'^vendor/', r'.*_generated\.py$'])

        files = [
            _gitlab_change('vendor/lib.py', 'vendor/lib.py'),
            _gitlab_change('src/api_generated.py', 'src/api.py'),
        ]

        errors = _capture_errors(lambda: filter_ignored(list(files), platform='gitlab'))

        assert errors == []

    def test_every_pattern_can_drop_an_entry_one_at_a_time(self, monkeypatch):
        """Narrow down to a single survivor so each pass has something to drop."""
        self._ignore(monkeypatch, [r'^a/', r'^b/', r'^c/', r'^d/', r'^e/'])

        files = [
            _gitlab_change('a/1.py', 'a/1.py'),
            _gitlab_change('b/2.py', 'b/2.py'),
            _gitlab_change('c/3.py', 'c/3.py'),
            _gitlab_change('d/4.py', 'd/4.py'),
            _gitlab_change('e/5.py', 'e/5.py'),
            _gitlab_change('src/keep.py', 'src/keep.py'),
        ]

        assert filter_ignored(list(files), platform='gitlab') == [files[-1]]


class TestNoPatternsConfigured:
    """No compiled pattern means there is nothing to match, so nothing is filtered.

    This holds for every platform, including one whose entry names no path: the
    filter is a no-op, not an opportunity to drop entries the user never asked to
    exclude. It also means a pathless entry is only ever dropped by a pattern pass.
    """

    @staticmethod
    def _no_patterns(monkeypatch, regex=None):
        monkeypatch.setattr(global_settings.ignore, 'regex', regex if regex is not None else [])
        monkeypatch.setattr(global_settings.ignore, 'glob', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

    def test_gitlab_pathless_entry_survives_when_no_pattern_is_configured(self, monkeypatch):
        self._no_patterns(monkeypatch)

        pathless = {'diff': 'diff --git a/x b/x'}
        files = [pathless, _gitlab_change('src/app.py', 'src/app.py')]

        assert filter_ignored(list(files), platform='gitlab') == files

    def test_bitbucket_pathless_entry_survives_when_no_pattern_is_configured(self, monkeypatch):
        self._no_patterns(monkeypatch)

        pathless = _BitbucketDiffstat(None, None)
        files = [pathless, _BitbucketDiffstat('src/app.py', 'src/app.py')]

        assert filter_ignored(list(files), platform='bitbucket') == files

    def test_gitlab_rename_survives_when_no_pattern_is_configured(self, monkeypatch):
        self._no_patterns(monkeypatch)

        renamed = _gitlab_change('poetry.lock', 'notes.txt')

        assert filter_ignored([renamed], platform='gitlab') == [renamed]

    def test_nothing_is_filtered_when_every_pattern_fails_to_compile(self, monkeypatch):
        """An unusable pattern leaves no pattern to match, so the list is untouched."""
        self._no_patterns(monkeypatch, regex=['(((||', '[[['])

        pathless = {'diff': 'diff --git a/x b/x'}
        files = [pathless, _gitlab_change('src/app.py', 'src/app.py')]

        assert filter_ignored(list(files), platform='gitlab') == files

    def test_pathless_entry_is_dropped_once_a_pattern_exists(self, monkeypatch):
        """The drop belongs to a pattern pass, which is the only thing that excludes."""
        self._no_patterns(monkeypatch, regex=[r'^vendor/'])

        pathless = {'diff': 'diff --git a/x b/x'}
        files = [pathless, _gitlab_change('src/app.py', 'src/app.py')]

        assert filter_ignored(list(files), platform='gitlab') == [files[1]]

    def test_azure_leading_slash_does_not_defeat_the_ignore_patterns(self, monkeypatch):
        """Azure DevOps reports "/vendor/lib/x.js"; the anchored pattern must still match."""
        self._no_patterns(monkeypatch, regex=[r'^vendor/'])

        files = ['/vendor/lib/jquery.min.js', '/src/app.cs']

        assert filter_ignored(list(files), platform='azure') == ['/src/app.cs']

    def test_azure_paths_without_a_leading_slash_still_filter(self, monkeypatch):
        self._no_patterns(monkeypatch, regex=[r'^vendor/'])

        files = ['vendor/lib/jquery.min.js', 'src/app.cs']

        assert filter_ignored(list(files), platform='azure') == ['src/app.cs']

    def test_azure_keeps_unmatched_paths(self, monkeypatch):
        self._no_patterns(monkeypatch, regex=[r'^vendor/'])

        files = ['/src/app.cs', '/docs/readme.md']

        assert filter_ignored(list(files), platform='azure') == files


# A changed-file entry per platform, built so that each carries the same path twice over.
_ENTRY_SHAPES = {
    'github': lambda p: type('', (object,), {'filename': p})(),
    'codecommit': lambda p: type('', (object,), {'filename': p})(),
    'bitbucket': lambda p: _BitbucketDiffstat(p, p),
    'bitbucket_server': lambda p: {'path': {'toString': p}},
    'gitlab': lambda p: _gitlab_change(p, p),
    'azure': lambda p: p,
    'gitea': lambda p: {'filename': p},
    'gerrit': lambda p: type('', (object,), {'b_path': p, 'a_path': p})(),
}


class TestUnfilteredPlatformIsReported:
    """A platform the dispatch cannot read passes every file through, so say so.

    The dispatch's `else` breaks out for a platform name it does not recognise, which
    leaves the list untouched. That is the safe direction to fail -- dropping
    files the user never asked to exclude would hide real changes -- but it is only safe
    while it is visible, because an unfiltered list inflates the prompt and pushes the
    run into the token budget and fallback-model paths.
    """

    @staticmethod
    def _ignore(monkeypatch):
        monkeypatch.setattr(global_settings.ignore, 'regex', [r'^vendor/'])
        monkeypatch.setattr(global_settings.ignore, 'glob', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

    def test_unrecognised_platform_warns_that_nothing_was_filtered(self, monkeypatch):
        self._ignore(monkeypatch)

        files = [type('', (object,), {'filename': 'vendor/lib.py'})()]

        warnings = _capture_warnings(lambda: filter_ignored(list(files), platform='giteaaa'))

        assert len(warnings) == 1, f"Expected one warning, got {warnings}"

    def test_unrecognised_platform_warning_names_the_platform(self, monkeypatch):
        """The platform string is the only thing that identifies the bad call site.

        It goes in the message rather than the artifact because the message is what a
        reader scanning the log sees.
        """
        self._ignore(monkeypatch)

        files = [type('', (object,), {'filename': 'vendor/lib.py'})()]

        warnings = _capture_warnings(lambda: filter_ignored(list(files), platform='giteaaa'))

        assert len(warnings) == 1
        assert 'giteaaa' in warnings[0]

    def test_unrecognised_platform_warns_once_per_call_not_once_per_pattern(self, monkeypatch):
        """The `break` after the warning keeps N patterns from producing N warnings."""
        monkeypatch.setattr(global_settings.ignore, 'regex', [r'^vendor/', r'^secrets/', r'.*\.pem$'])
        monkeypatch.setattr(global_settings.ignore, 'glob', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

        files = [type('', (object,), {'filename': 'vendor/lib.py'})()]

        warnings = _capture_warnings(lambda: filter_ignored(list(files), platform='giteaaa'))

        assert len(warnings) == 1, f"Expected one warning, got {warnings}"

    def test_unrecognised_platform_does_not_warn_when_there_is_nothing_to_filter(self, monkeypatch):
        """No compiled pattern means no filter was skipped, so warning would be noise."""
        monkeypatch.setattr(global_settings.ignore, 'regex', [])
        monkeypatch.setattr(global_settings.ignore, 'glob', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

        files = [type('', (object,), {'filename': 'vendor/lib.py'})()]

        warnings = _capture_warnings(lambda: filter_ignored(list(files), platform='giteaaa'))

        assert warnings == []

    def test_every_supported_platform_actually_drops_a_matching_file(self, monkeypatch):
        """Each listed platform must reach its branch; an unreachable one is dead config."""
        self._ignore(monkeypatch)

        for platform, build in _ENTRY_SHAPES.items():
            matching = build('vendor/lib.py')
            kept = build('src/app.py')

            assert filter_ignored([matching, kept], platform=platform) == [kept], (
                f"platform {platform!r} did not apply the ignore pattern"
            )

    def test_supported_platforms_do_not_warn(self, monkeypatch):
        self._ignore(monkeypatch)

        for platform, build in _ENTRY_SHAPES.items():
            files = [build('vendor/lib.py')]

            assert _capture_warnings(lambda p=platform, f=files: filter_ignored(list(f), platform=p)) == [], (
                f"platform {platform!r} should not warn"
            )


class TestFilterFailureIsReported:
    """A failure leaves the list in whatever state the last completed pass left it.

    Each pattern pass rebinds `files` only once that pass completes, so an exception
    on a later pass returns the earlier passes' result; an exception before the first
    pass returns the caller's list untouched. Both states may hold files the [ignore]
    rules should have excluded, which is what the report has to say -- and what it
    must not overstate, since no pass may have run at all.
    """

    @staticmethod
    def _exploding_settings(monkeypatch):
        class _Exploding:
            @property
            def ignore(self):
                raise RuntimeError('settings unavailable')

        monkeypatch.setattr('pr_agent.algo.file_filter.get_settings', lambda: _Exploding())

    def test_failure_states_that_the_list_may_still_contain_ignored_files(self, monkeypatch):
        """The message has to say what the caller now has, not just what went wrong."""
        self._exploding_settings(monkeypatch)

        files = [type('', (object,), {'filename': 'vendor/lib.py'})()]

        errors = _capture_errors(lambda: filter_ignored(list(files), platform='github'))

        assert len(errors) == 1, f"Expected one error, got {errors}"
        assert 'filtering did not complete' in errors[0]
        assert 'may still' in errors[0]

    def test_failure_returns_every_file_when_no_pattern_pass_has_run(self, monkeypatch):
        """A failure before the passes leaves the caller's list untouched."""
        self._exploding_settings(monkeypatch)

        files = [type('', (object,), {'filename': 'vendor/lib.py'})()]

        assert filter_ignored(list(files), platform='github') == files

    def test_failure_after_an_earlier_pass_returns_that_pass_s_result(self, monkeypatch):
        """Pin the partly filtered state the message describes.

        `filename` raises once two passes have read it, so the first pattern drops
        `vendor/lib.py` and the second fails part-way. The result is the first pass's
        list: shorter than the input, so "the unfiltered list is being used" would be
        false, yet `vendor/lib.py` is gone, so it is not a safe list either.
        """
        monkeypatch.setattr(global_settings.ignore, 'regex', [r'^vendor/', r'^secrets/'])
        monkeypatch.setattr(global_settings.ignore, 'glob', [])
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', [])

        class _FailsOnThirdRead:
            def __init__(self):
                self.reads = 0

            @property
            def filename(self):
                self.reads += 1
                if self.reads > 2:
                    raise RuntimeError('boom')
                return 'src/app.py'

        files = [type('', (object,), {'filename': 'vendor/lib.py'})(), _FailsOnThirdRead()]

        captured = {}
        errors = _capture_errors(lambda: captured.setdefault('result', filter_ignored(list(files), 'github')))
        result = captured['result']

        assert len(result) == 1
        assert result[0] is files[1]
        assert len(errors) == 1, f"Expected the failure to be reported once, got {errors}"
        assert 'filtering did not complete' in errors[0]


class TestRegexBudget:
    """The globstar variants are bounded, so a long or hostile glob list cannot run away."""

    # distinct five-globstar globs, so the budget is really spent rather than deduplicated away
    HEAVY = tuple(f"dir{i}/**/b/**/c/**/d/**/e/**/f/x.py" for i in range(12))

    def test_a_glob_at_the_cap_yields_at_most_the_documented_number(self):
        pattern = 'a0/**/b/**/c/**/d/**/e/**/f/**/x.py'
        assert len(file_filter._globstar_offsets(pattern)) == file_filter._MAX_ENUMERATED_GLOBSTARS

        assert len(translate_globs_to_regexes([pattern])) == 2 ** file_filter._MAX_ENUMERATED_GLOBSTARS

    def test_a_long_list_of_globstar_globs_stops_at_the_variant_ceiling(self):
        budget = file_filter._RegexBudget(file_filter._MAX_IGNORE_GLOB_VARIANT_REGEXES)

        regexes = translate_globs_to_regexes(self.HEAVY, budget)

        assert budget.variants == file_filter._MAX_IGNORE_GLOB_VARIANT_REGEXES
        assert len(regexes) == budget.variants + len(self.HEAVY), 'only the variants are capped'

    def test_the_ceiling_is_reported_once_per_call(self):
        """Once per call, not once per glob and not once per process: a long-lived server serves
        many repositories, and the next one has to be told as well."""
        first = _capture_logs(lambda: translate_globs_to_regexes(self.HEAVY))
        second = _capture_logs(lambda: translate_globs_to_regexes(self.HEAVY))

        assert first.count('zero-directory variant') == 1, "one report is enough, not one per glob"
        assert 'zero-directory variant' in second, "a later call still has to be told"

    @pytest.mark.parametrize(
        ('globs', 'reported'),
        [
            (['**/**', '**/**/**', '**/**/**/**'], 'matches every file'),
            (['**/a/**/b/**/c/**/d/**/e/**/f/**/g/**/h/x.py',
              '**/q/**/r/**/s/**/t/**/u/**/v/**/w/**/x/**/y.py'],
             'zero-directory globstar variants'),
        ],
    )
    def test_one_condition_repeated_across_globs_is_reported_once(self, globs, reported):
        """Several globs can hit the same condition, and one line per glob is log noise."""
        logs = _capture_logs(lambda: translate_globs_to_regexes(globs))

        assert logs.count(reported) == 1

    @pytest.mark.parametrize(
        ('glob', 'form'),
        [
            ('*', None),  # the operator writing the everything-matcher outright, no globstar
            ('**', None),
            ('**/*', '*'),  # the root-level form of a '**/'-leading glob
            ('**/**', '**'),
            ('**/**/**', '**'),  # only a variant collapses here
        ],
    )
    def test_a_glob_that_drops_every_file_is_reported(self, glob, form):
        """A configured form is kept whatever the budget says, so it can empty a review silently.

        `*` and `**` carry no globstar, so nothing but the configured-form check can report them;
        the rest collapse to the everything-matcher through a form that is kept either way.
        """
        logs = _capture_logs(lambda: translate_globs_to_regexes([glob]))

        assert f"The ignore glob '{glob}' matches every file" in logs
        assert (f"as '{form}' does" in logs) == bool(form), "name the form only when it differs"
        assert f"'{glob}' does" not in logs, "a configured glob is not restated as its own form"

    @pytest.mark.parametrize('later', ['*', '**/*'])
    def test_a_glob_after_a_spent_budget_is_still_reported(self, later):
        """The budget returns early, which used to skip the check for every later glob."""
        logs = _capture_logs(lambda: translate_globs_to_regexes(list(self.HEAVY) + [later]))

        assert 'Ignoring the remaining zero-directory variants' in logs, \
            "the ceiling was not reached, so the early return never ran"
        assert f"The ignore glob '{later}' matches every file" in logs

    @pytest.mark.parametrize('glob', ['src/**/**/**', 'a/**/b.py', 'vendor/**'])
    def test_a_glob_that_only_covers_part_of_the_tree_is_not_reported(self, glob):
        """`src/**` still leaves the rest of the repository to analyze, so it is not this warning."""
        logs = _capture_logs(lambda: translate_globs_to_regexes([glob]))

        assert 'matches every file' not in logs

    def test_the_budget_is_shared_by_the_lists_of_one_request(self, monkeypatch):
        """One budget per filter_ignored call, not one per list it translates."""
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', ['protobuf'])
        monkeypatch.setattr(global_settings.generated_code, 'protobuf',
                            ['dir{i}/**/b/**/c/**/d/**/e/**/f/x.py'.format(i=i) for i in range(12)])
        monkeypatch.setattr(global_settings.ignore, 'glob', list(self.HEAVY))
        monkeypatch.setattr(global_settings.ignore, 'regex', [])
        budgets = []
        original = file_filter.translate_globs_to_regexes

        def counted(globs, budget=None):
            budgets.append(budget)
            return original(globs, budget)

        monkeypatch.setattr(file_filter, 'translate_globs_to_regexes', counted)
        filter_ignored([type('', (object,), {'filename': 'a.py'})()])

        assert len(budgets) == 2, 'both the ignore list and the generated-code list are translated'
        assert budgets[0] is budgets[1], 'both lists must share one budget'
        assert budgets[0].variants <= file_filter._MAX_IGNORE_GLOB_VARIANT_REGEXES

    def test_a_one_shot_iterable_of_globs_keeps_its_variants(self):
        """Two passes need a sequence; a consumed iterator would silently lose every variant."""
        pattern = 'a0/**/b/**/c/**/d/**/e/**/f/**/x.py'

        assert translate_globs_to_regexes(iter([pattern])) == translate_globs_to_regexes([pattern])

    def test_a_repeated_glob_is_expanded_once(self, monkeypatch):
        """A repeat never charges the variant budget, so without this it is expanded every time."""
        pattern = 'a0/**/b/**/c/**/d/**/e/**/f/**/x.py'
        original = file_filter.fnmatch.translate

        def count_for(globs):
            calls = []
            monkeypatch.setattr(file_filter.fnmatch, 'translate',
                                lambda value: (calls.append(value), original(value))[1])
            translate_globs_to_regexes(globs)
            return len(calls)

        once, repeated = count_for([pattern]), count_for([pattern] * 200)

        assert once == 2 ** len(file_filter._globstar_offsets(pattern)), 'the count has to be real'
        assert repeated == once, f'200 repeats were translated {repeated} times, not {once}'

    @pytest.mark.parametrize('pattern', ['**/**/**/', '**/'])
    def test_a_pattern_of_only_globstars_does_not_produce_an_empty_regex(self, pattern):
        """fnmatch.translate('') matches only the empty name, so no regex may be one."""
        regexes = translate_globs_to_regexes([pattern])

        assert regexes[0] == fnmatch.translate(pattern)
        assert fnmatch.translate('') not in regexes, "the empty name is not a file path"

    def test_a_repeated_glob_costs_nothing_and_does_not_crowd_out_others(self):
        """Every regex is compiled and run per file, so repeats must not spend the budget."""
        pattern = 'a0/**/b/**/c/**/d/**/e/**/f/**/x.py'
        alone = translate_globs_to_regexes([pattern])

        with_repeats = translate_globs_to_regexes([pattern] * 50)

        assert with_repeats == alone
        assert fnmatch.translate('private/report.py') in translate_globs_to_regexes(
            [pattern] * 50 + ['private/report.py'])

    def test_the_variant_ceiling_spans_every_list_of_one_request(self):
        """filter_ignored translates several lists, and each regex runs against every file."""
        budget = file_filter._RegexBudget(file_filter._MAX_IGNORE_GLOB_VARIANT_REGEXES)
        other = [f"other{i}/**/b/**/c/**/d/**/e/**/f/x.py" for i in range(12)]

        first = translate_globs_to_regexes(self.HEAVY, budget)
        spent = budget.variants
        second = translate_globs_to_regexes(other, budget)

        assert spent == file_filter._MAX_IGNORE_GLOB_VARIANT_REGEXES
        assert len(second) < len(translate_globs_to_regexes(other)), 'truncated by what came before'
        assert budget.variants == file_filter._MAX_IGNORE_GLOB_VARIANT_REGEXES
        assert len(budget.kept) == len(first) + len(second)

    def test_a_list_kept_with_a_shared_budget_still_returns_its_own_regexes(self):
        budget = file_filter._RegexBudget(file_filter._MAX_IGNORE_GLOB_VARIANT_REGEXES)
        translate_globs_to_regexes(['a/**/b.py'], budget)

        later = translate_globs_to_regexes(['private/report.py'], budget)

        assert later == [fnmatch.translate('private/report.py')]

    @pytest.mark.parametrize('later', [
        '**/secret.py',  # within the per-glob cap: the root-level form is also a variant
        '**/private/**/a/**/b/**/c/**/d/**/e/**/f/report.py',  # over it: the root-level form is all
    ])
    def test_a_root_level_form_survives_a_spent_variant_budget(self, later):
        """It is coverage this change did not add, so new expansion must not squeeze it out."""
        budget = file_filter._RegexBudget(0)  # no room for any new variant at all

        regexes = translate_globs_to_regexes(list(self.HEAVY) + [later], budget)

        assert fnmatch.translate(later[len('**/'):]) in regexes

    def test_the_glob_that_lost_its_variants_is_named(self):
        """A generic line would not say which configured glob stopped applying."""
        budget = file_filter._RegexBudget(0)

        logs = _capture_logs(
            lambda: translate_globs_to_regexes(self.HEAVY, budget))

        assert f"'{self.HEAVY[0]}'" in logs
        assert 'still analyzed' in logs

    def test_a_long_list_of_globs_keeps_every_configured_form(self):
        """A configured glob costs one regex, as it did before this change, and dropping one
        analyzes files the operator asked to exclude. Losing a variant here is fine: these
        globs hold a '**/', so what has to survive is the form the operator wrote."""
        globs = [f'pkg{i}/**/generated_mod{i}.py' for i in range(300)]

        regexes = translate_globs_to_regexes(globs)

        assert all(fnmatch.translate(glob) in regexes for glob in globs)

    def test_a_rejected_variant_is_not_reported_as_matching_everything(self):
        """A glob the budget refused to keep cannot silently ignore everything either."""
        globs = ['kept.py', '**/**/**']

        def run():  # the '**/**' root-level form is kept, the '**' variant has no room
            return translate_globs_to_regexes(globs, file_filter._RegexBudget(0))

        kept = run()
        logs = _capture_logs(run)

        assert fnmatch.translate('**/**') in kept, "the rejected variant should be the '**' one"
        assert fnmatch.translate('**') not in kept
        assert 'matches every file' not in logs
        assert 'matches every file' in _capture_logs(
            lambda: translate_globs_to_regexes(globs, file_filter._RegexBudget(64)))

    def test_a_glob_that_expands_widely_does_not_starve_another_glob(self):
        """A configured ignore glob that is dropped only says nothing, which reads as 'no match'."""
        globs = list(self.HEAVY) + ['private/report.py']

        regexes = translate_globs_to_regexes(globs)

        assert all(fnmatch.translate(glob) in regexes for glob in globs)
