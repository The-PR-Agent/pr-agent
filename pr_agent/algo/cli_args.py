from base64 import b64decode

import yaml

from pr_agent.config_security import (
    CLI_HOST_ONLY_KEYS_BY_SECTION,
    REPO_HOST_ONLY_KEYS_BY_SECTION,
    REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION,
)

_MAPPING_MAX_DEPTH = 32
_MAPPING_MAX_VISITS = 128
_MAPPING_TOO_COMPLEX_ARG = ".mapping_value_too_complex"


class CliArgs:
    @staticmethod
    def _host_only_setting_arg(arg: str) -> str | None:
        """Return a protected setting token when a CLI arg targets a host-only key."""
        setting_name = arg.lstrip('-').split('=', 1)[0].strip().replace('__', '.')
        section, separator, key = setting_name.partition('.')
        if not separator:
            if (section in REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION
                    or section in REPO_HOST_ONLY_KEYS_BY_SECTION
                    or section in CLI_HOST_ONLY_KEYS_BY_SECTION):
                return f'.{section}'
            return None

        allowed_keys = REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION.get(section)
        if allowed_keys is not None and key not in allowed_keys:
            return f'.{section}.{key}'
        host_only_keys = REPO_HOST_ONLY_KEYS_BY_SECTION.get(section, frozenset())
        if key.split('.', 1)[0] in host_only_keys:
            return f'.{section}.{key}'
        cli_host_only_keys = CLI_HOST_ONLY_KEYS_BY_SECTION.get(section, frozenset())
        if key.split('.', 1)[0] in cli_host_only_keys:
            return f'.{section}.{key}'
        return None

    @staticmethod
    def _blocked_setting_path(path: str, forbidden_cli_args: list) -> str | None:
        """Return the blocked token for a dotted section.key path, else None."""
        arg_word = f"--{path}=x".replace('__', '.').lower()
        host_only_arg = CliArgs._host_only_setting_arg(arg_word)
        if host_only_arg:
            return host_only_arg
        for forbidden_arg_word in forbidden_cli_args:
            if forbidden_arg_word in arg_word:
                return forbidden_arg_word
        return None

    @staticmethod
    def _mapping_setting_paths(
        section: str,
        value: object,
        _depth: int = 0,
        _ancestors: set | None = None,
        _visits: list | None = None,
    ) -> list | None:
        """Collect the dotted section.key path of every node in a mapping value.

        A key path is included even when its value is an empty container, so a forbidden
        or host-only key assigned ``{}`` or ``[]`` cannot evade validation. Identities on
        the current branch are tracked to cut cycles, and depth and total-visit caps stop
        YAML aliases from expanding into unbounded work. Any breach returns None and the
        caller rejects the argument.
        """
        if _ancestors is None:
            _ancestors = set()
        if _visits is None:
            _visits = [0]
        _visits[0] += 1
        if _depth > _MAPPING_MAX_DEPTH or _visits[0] > _MAPPING_MAX_VISITS:
            return None
        if isinstance(value, dict):
            value_id = id(value)
            if value_id in _ancestors:
                return None
            _ancestors.add(value_id)
            paths = []
            for key, nested in value.items():
                path = f"{section}.{key}"
                paths.append(path)
                child_paths = CliArgs._mapping_setting_paths(
                    path, nested, _depth + 1, _ancestors, _visits
                )
                if child_paths is None:
                    return None
                paths.extend(child_paths)
            _ancestors.discard(value_id)
            return paths
        if isinstance(value, list):
            value_id = id(value)
            if value_id in _ancestors:
                return None
            _ancestors.add(value_id)
            paths = [section]
            for item in value:
                child_paths = CliArgs._mapping_setting_paths(
                    section, item, _depth + 1, _ancestors, _visits
                )
                if child_paths is None:
                    return None
                paths.extend(child_paths)
            _ancestors.discard(value_id)
            return paths
        return [section]

    @staticmethod
    def _mapping_value(arg: str):
        """Return (section, parsed mapping) for a --section={key: value} arg, else None."""
        arg = arg.strip()
        if not arg.startswith('--'):
            return None
        setting_name, separator, value_text = arg[2:].partition('=')
        if not separator:
            return None
        try:
            parsed_value = yaml.safe_load(value_text)
        except Exception:
            return None
        if not isinstance(parsed_value, dict):
            return None
        return setting_name.replace('__', '.').lower(), parsed_value

    @staticmethod
    def is_mapping_arg(arg: str) -> bool:
        """Whether a --section=... argument carries a {key: value} mapping value.

        Callers that project arguments down to their keys must keep a mapping value
        intact so ``validate_user_args`` can inspect each nested ``section.key`` path.
        """
        return CliArgs._mapping_value(arg) is not None

    @staticmethod
    def _mapping_value_offending_arg(arg: str, forbidden_cli_args: list) -> str | None:
        """Return the blocked token when a mapping value hides one, "" when it does not,
        and None when the value is not a mapping.

        The forbidden and host-only checks above only see the section before ``=``. A mapping
        value sets many keys at once, so each nested ``section.key`` path must be validated the
        same way to keep ``--<section>={key: value}`` from smuggling a forbidden key past them.
        """
        mapping = CliArgs._mapping_value(arg)
        if mapping is None:
            return None
        section, parsed_value = mapping
        paths = CliArgs._mapping_setting_paths(section, parsed_value)
        if paths is None:
            return _MAPPING_TOO_COMPLEX_ARG
        for path in paths:
            offending = CliArgs._blocked_setting_path(path, forbidden_cli_args)
            if offending:
                return offending
        return ""

    @staticmethod
    def validate_user_args(args: list) -> (bool, str):
        try:
            if not args:
                return True, ""

            # decode forbidden args
            # b64encode('word'.encode()).decode()
            # NOTE: extra_config_url / description_path / review_path / improve_path
            # are added to block CLI injection of arbitrary filesystem write
            # targets in LocalGitProvider (where the bot writes PR-Agent output)
            # and remote-config fetchers.
            # NOTE: push_outputs is host-only config: it POSTs the full review to an
            # operator-chosen sink. Both the dotted form (--push_outputs.webhook_url=...)
            # and the whole-section form (--push_outputs={...}) are blocked, so a PR
            # comment cannot redirect review output to an attacker-controlled host.
            _encoded_args = (
                'c2hhcmVkX3NlY3JldA==:dXNlcg==:c3lzdGVt'
                ':ZW5hYmxlX2NvbW1lbnRfYXBwcm92YWw=:ZW5hYmxlX21hbnVhbF9hcHByb3ZhbA=='
                ':ZW5hYmxlX2F1dG9fYXBwcm92YWw=:YXBwcm92ZV9wcl9vbl9zZWxmX3Jldmlldw=='
                ':YmFzZV91cmw=:dXJs:d2ViX3VybA==:YXBwX25hbWU=:c2VjcmV0X3Byb3ZpZGVy'
                ':Z2l0X3Byb3ZpZGVy:c2tpcF9rZXlz:b3BlbmFpLmtleQ==:QU5BTFlUSUNTX0ZPTERFUg=='
                ':dXJp:YXBwX2lk:d2ViaG9va19zZWNyZXQ=:YmVhcmVyX3Rva2Vu'
                ':UEVSU09OQUxfQUNDRVNTX1RPS0VO:b3ZlcnJpZGVfZGVwbG95bWVudF90eXBl'
                ':cHJpdmF0ZV9rZXk=:bG9jYWxfY2FjaGVfcGF0aA==:ZW5hYmxlX2xvY2FsX2NhY2hl'
                ':amlyYV9iYXNlX3VybA==:YXBpX2Jhc2U=:YXBpX3R5cGU=:YXBpX3ZlcnNpb24='
                ':c2tpcF9rZXlz:ZXh0cmFfY29uZmlnX3VybA==:ZGVzY3JpcHRpb25fcGF0aA=='
                ':cmV2aWV3X3BhdGg=:aW1wcm92ZV9wYXRo'
                ':cHVzaF9vdXRwdXRzLg==:cHVzaF9vdXRwdXRzPQ=='
            )

            forbidden_cli_args = []
            for e in _encoded_args.split(':'):
                forbidden_cli_args.append(b64decode(e).decode())

            # lowercase all forbidden args
            for i, _ in enumerate(forbidden_cli_args):
                forbidden_cli_args[i] = forbidden_cli_args[i].lower()
                # A bare word is only meaningful as a section-qualified key, so anchor it with a
                # dot. A word that already carries a '.' or a '=' is a literal to match as-is.
                if '.' not in forbidden_cli_args[i] and '=' not in forbidden_cli_args[i]:
                    forbidden_cli_args[i] = '.' + forbidden_cli_args[i]

            for arg in args:
                arg = arg.strip()
                if arg.startswith('--'):
                    arg_word = arg.lower()
                    arg_word = arg_word.replace('__', '.')  # replace double underscore with dot, e.g. --openai__key -> --openai.key
                    host_only_arg = CliArgs._host_only_setting_arg(arg_word)
                    if host_only_arg:
                        return False, host_only_arg
                    # A mapping value sets many keys at once, so validate each nested
                    # section.key path against the host-only and forbidden lists instead of
                    # matching the value text, which may legitimately mention a key name.
                    mapping_offending_arg = CliArgs._mapping_value_offending_arg(arg, forbidden_cli_args)
                    if mapping_offending_arg is not None:
                        if mapping_offending_arg:
                            return False, mapping_offending_arg
                        continue
                    for forbidden_arg_word in forbidden_cli_args:
                        if forbidden_arg_word in arg_word:
                            return False, forbidden_arg_word
            return True, ""
        except Exception as e:
            return False, str(e)
