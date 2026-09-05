# Language Selection, source: https://github.com/bigcode-project/bigcode-dataset/blob/main/language_selection/programming-languages-to-file-extensions.json  # noqa E501
from collections.abc import Callable
from typing import Dict

from pr_agent.config_loader import get_settings


def filter_bad_extensions(files):
    # Bad Extensions, source: https://github.com/EleutherAI/github-downloader/blob/345e7c4cbb9e0dc8a0615fd995a08bf9d73b3fe6/download_repo_text.py  # noqa: E501
    bad_extensions = get_settings().bad_extensions.default
    if get_settings().config.use_extra_bad_extensions:
        bad_extensions += get_settings().bad_extensions.extra
    return [f for f in files if f.filename is not None and is_valid_file(f.filename, bad_extensions)]


def is_valid_file(filename:str, bad_extensions=None) -> bool:
    if not filename:
        return False
    if not bad_extensions:
        bad_extensions = get_settings().bad_extensions.default
        if get_settings().config.use_extra_bad_extensions:
            bad_extensions += get_settings().bad_extensions.extra

    auto_generated_files_exact = {
        'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'composer.lock', 'Gemfile.lock',
        'poetry.lock', 'go.sum', '.terraform.lock.hcl', 'uv.lock',
        'Cargo.lock', 'Pipfile.lock', 'mix.lock', 'pubspec.lock', 'bun.lockb',
    }
    auto_generated_suffixes = ('.min.js', '.min.css', '.js.map', '.ts.map', '.css.map')
    if filename.replace('\\', '/').split('/')[-1] in auto_generated_files_exact:
        return False
    if filename.endswith(auto_generated_suffixes):
        return False

    return filename.split('.')[-1] not in bad_extensions


def build_language_file_matcher(language_extension_map: Dict) -> Callable[[str], str | None]:
    """Build a filename classifier from the configured language extensions."""
    exact_to_language = {}
    folded_to_languages = {}
    for language, extensions in language_extension_map.items():
        for extension in extensions:
            token = extension.lstrip("*")
            exact_to_language.setdefault(token, language)
            folded_to_languages.setdefault(token.casefold(), set()).add(language)
    max_suffix_depth = max(
        (token.count(".") for token in exact_to_language if token.startswith(".")),
        default=0,
    )

    def get_language(filename: str) -> str | None:
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        parts = name.split(".")
        candidates = [name] + [
            "." + ".".join(parts[i:])
            for i in range(
                max(1, len(parts) - max_suffix_depth),
                len(parts),
            )
        ]

        for candidate in candidates:
            language = exact_to_language.get(candidate)
            if language:
                return language
            languages = folded_to_languages.get(candidate.casefold(), set())
            if len(languages) == 1:
                return next(iter(languages))

        return None

    return get_language


def sort_files_by_main_languages(languages: Dict, files: list):
    """
    Sort files by their main language, put the files that are in the main language first and the rest files after
    """
    # sort languages by their size
    languages_sorted_list = [k for k, v in sorted(languages.items(), key=lambda item: item[1], reverse=True)]
    # languages_sorted = sorted(languages, key=lambda x: x[1], reverse=True)
    # get all extensions for the languages
    language_extension_map_org = get_settings().language_extension_map_org
    configured_language_names = {
        language.lower(): language
        for language in language_extension_map_org
    }
    selected_language_names = {
        configured_language_names[language.lower()]: language
        for language in languages_sorted_list
        if language.lower() in configured_language_names
    }
    get_language = build_language_file_matcher(language_extension_map_org)

    # filter out files bad extensions
    files_filtered = filter_bad_extensions(files)

    # sort files by their language, put the files that are in the main language first
    # and the rest files after, map languages_sorted to their respective files
    files_sorted = []
    rest_files = {}

    # if no languages detected, put all files in the "Other" category
    if not languages:
        files_sorted = [({"language": "Other", "files": list(files_filtered)})]
        return files_sorted

    files_by_language = {language: [] for language in languages_sorted_list}
    for file in files_filtered:
        configured_language = get_language(file.filename)
        selected_language = selected_language_names.get(configured_language)
        if selected_language:
            files_by_language[selected_language].append(file)
        else:
            rest_files.setdefault(file.filename, file)

    for language in languages_sorted_list:
        if files_by_language[language]:
            files_sorted.append({"language": language, "files": files_by_language[language]})
    files_sorted.append({"language": "Other", "files": list(rest_files.values())})
    return files_sorted
