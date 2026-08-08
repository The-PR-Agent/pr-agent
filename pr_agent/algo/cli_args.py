from base64 import b64decode, encode, b64encode
import hashlib

class CliArgs:
    @staticmethod
    def validate_user_args(args: list) -> (bool, str):
        try:
            if not args:
                return True, ""

            # decode forbidden args
            # b64encode('word'.encode()).decode()
            _encoded_parts = [
                'c2hhcmVkX3NlY3JldA==',  # shared_secret
                'dXNlcg==',  # user
                'c3lzdGVt',  # system
                'ZW5hYmxlX2NvbW1lbnRfYXBwcm92YWw=',  # enable_comment_approval
                'ZW5hYmxlX21hbnVhbF9hcHByb3ZhbA==',  # enable_manual_approval
                'ZW5hYmxlX2F1dG9fYXBwcm92YWw=',  # enable_auto_approval
                'YXBwcm92ZV9wcl9vbl9zZWxmX3Jldmlldw==',  # approve_pr_on_self_review
                'YmFzZV91cmw=',  # base_url
                'dXJs',  # url
                'YXBwX25hbWU=',  # app_name
                'c2VjcmV0X3Byb3ZpZGVy',  # secret_provider
                'c2tpcF9rZXlz',  # skip_keys
                'b3BlbmFpLmtleQ==',  # openai.key
                'QU5BTFlUSUNTX0ZPTERFUg==',  # ANALYTICS_FOLDER
                'dXJp',  # uri
                'YXBwX2lk',  # app_id
                'd2ViaG9va19zZWNyZXQ=',  # webhook_secret
                'YmVhcmVyX3Rva2Vu',  # bearer_token
                'UEVSU09OQUxfQUNDRVNTX1RPS0VO',  # PERSONAL_ACCESS_TOKEN
                'b3ZlcnJpZGVfZGVwbG95bWVudF90eXBl',  # override_deployment_type
                'cHJpdmF0ZV9rZXk=',  # private_key
                'bG9jYWxfY2FjaGVfcGF0aA==',  # local_cache_path
                'ZW5hYmxlX2xvY2FsX2NhY2hl',  # enable_local_cache
                'amlyYV9iYXNlX3VybA==',  # jira_base_url
                'YXBpX2Jhc2U=',  # api_base
                'YXBpX3R5cGU=',  # api_type
                'YXBpX3ZlcnNpb24=',  # api_version
            ]
            _encoded_args = ':'.join(_encoded_parts)

            forbidden_cli_args = []
            for e in _encoded_args.split(':'):
                forbidden_cli_args.append(b64decode(e).decode())

            # lowercase all forbidden args
            for i, _ in enumerate(forbidden_cli_args):
                forbidden_cli_args[i] = forbidden_cli_args[i].lower()
                if '.' not in forbidden_cli_args[i]:
                    forbidden_cli_args[i] = '.' + forbidden_cli_args[i]

            for arg in args:
                if arg.startswith('--'):
                    arg_word = arg.lower()
                    arg_word = arg_word.replace('__', '.')  # replace double underscore with dot, e.g. --openai__key -> --openai.key
                    for forbidden_arg_word in forbidden_cli_args:
                        if forbidden_arg_word in arg_word:
                            return False, forbidden_arg_word
            return True, ""
        except Exception as e:
            return False, str(e)


