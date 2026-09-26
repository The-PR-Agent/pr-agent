from pr_agent.algo.file_filter import filter_ignored
from pr_agent.config_loader import global_settings


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
