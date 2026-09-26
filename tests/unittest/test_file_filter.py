from pr_agent.algo.file_filter import filter_ignored
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
