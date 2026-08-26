# Language Selection, source: https://github.com/bigcode-project/bigcode-dataset/blob/main/language_selection/programming-languages-to-file-extensions.json  # noqa E501
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


def sort_files_by_main_languages(languages: Dict, files: list):
    """
    Sort files by their main language, put the files that are in the main language first and the rest files after
    """
    # sort languages by their size
    languages_sorted_list = [k for k, v in sorted(languages.items(), key=lambda item: item[1], reverse=True)]
    # languages_sorted = sorted(languages, key=lambda x: x[1], reverse=True)
    # get all extensions for the languages
    main_extensions = []
    language_extension_map_org = get_settings().language_extension_map_org
    language_extension_map = {k.lower(): v for k, v in language_extension_map_org.items()}
    for language in languages_sorted_list:
        if language.lower() in language_extension_map:
            main_extensions.append(language_extension_map[language.lower()])
        else:
            main_extensions.append([])

    # filter out files bad extensions
    files_filtered = filter_bad_extensions(files)

    # sort files by their extension, put the files that are in the main extension first
    # and the rest files after, map languages_sorted to their respective files
    files_sorted = []
    rest_files = {}

    # if no languages detected, put all files in the "Other" category
    if not languages:
        files_sorted = [({"language": "Other", "files": list(files_filtered)})]
        return files_sorted

    main_extensions_flat = []
    for ext in main_extensions:
        main_extensions_flat.extend(ext)

    for extensions, lang in zip(main_extensions, languages_sorted_list):  # noqa: B905
        tmp = []
        for file in files_filtered:
            extension_str = f".{file.filename.split('.')[-1]}"
            if extension_str in extensions:
                tmp.append(file)
            else:
                if (file.filename not in rest_files) and (extension_str not in main_extensions_flat):
                    rest_files[file.filename] = file
        if len(tmp) > 0:
            files_sorted.append({"language": lang, "files": tmp})
    files_sorted.append({"language": "Other", "files": list(rest_files.values())})
    return files_sorted
