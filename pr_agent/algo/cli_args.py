from base64 import b64decode

from pr_agent.config_security import REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION


class CliArgs:
    @staticmethod
    def _host_only_setting_arg(arg: str) -> str | None:
        """Return a protected setting token when a CLI arg targets a host-only key."""
        setting_name = arg.removeprefix('--').split('=', 1)[0].replace('__', '.')
        section, separator, key = setting_name.partition('.')
        if not separator:
            if section in REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION:
                return f'.{section}'
            return None

        allowed_keys = REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION.get(section)
        if allowed_keys is not None and key not in allowed_keys:
            return f'.{section}.{key}'
        return None

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
                    for forbidden_arg_word in forbidden_cli_args:
                        if forbidden_arg_word in arg_word:
                            return False, forbidden_arg_word
            return True, ""
        except Exception as e:
            return False, str(e)
