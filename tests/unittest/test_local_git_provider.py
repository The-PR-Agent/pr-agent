import git

from pr_agent.git_providers.local_git_provider import LocalGitProvider


def _make_repo(tmp_path, filenames):
    repo = git.Repo.init(tmp_path)
    for name in filenames:
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")
        repo.index.add([str(f)])
    repo.index.commit("init")
    return repo


def test_get_languages_returns_language_names(tmp_path):
    # get_languages() must key on language NAMES (e.g. "Python"), not raw
    # extensions ("py"): sort_files_by_main_languages() maps names back to
    # extensions, so extension keys would drop every file into "Other" and
    # defeat the hunk prioritisation this method exists for.
    repo = _make_repo(tmp_path, ["a.py", "b.py", "c.py", "d.js", "weird.zzz"])
    provider = object.__new__(LocalGitProvider)  # bypass heavy __init__
    provider.repo = repo

    languages = provider.get_languages()
    # 3 Python + 1 JavaScript known; .zzz is unknown and excluded from the total.
    assert languages == {"Python": 75.0, "JavaScript": 25.0}

    # Verify the values flow through the real consumer into proper buckets.
    from pr_agent.algo.language_handler import sort_files_by_main_languages

    class _F:
        def __init__(self, name):
            self.filename = name

    files = [_F("a.py"), _F("d.js"), _F("weird.zzz")]
    buckets = {b["language"]: {f.filename for f in b["files"]}
               for b in sort_files_by_main_languages(languages, files)}
    assert buckets["Python"] == {"a.py"}
    assert buckets["JavaScript"] == {"d.js"}
    assert buckets["Other"] == {"weird.zzz"}  # unknown extension falls through


def test_get_languages_matches_full_names_and_multipart_extensions(tmp_path):
    # Beyond simple ".ext", the language map also has full-filename rules
    # ("Dockerfile") and multi-part extensions (".cmake.in"); Path.suffix alone
    # would miss both. Match on the whole filename and dotted-suffix fallbacks.
    repo = _make_repo(tmp_path, ["Dockerfile", "build.cmake.in", "app.py"])
    provider = object.__new__(LocalGitProvider)
    provider.repo = repo

    languages = provider.get_languages()
    # One file each -> ~33.33% apiece, and none dropped as "unknown".
    assert set(languages) == {"Dockerfile", "CMake", "Python"}
    assert all(abs(v - 100 / 3) < 1e-6 for v in languages.values())
