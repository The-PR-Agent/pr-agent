from dynaconf import Dynaconf

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.log import get_logger
from pr_agent.mcp import MCPRuntime, MCPRuntimeError


class PRConfig:
    """
    The PRConfig class is responsible for listing all configuration options available for the user.
    """
    def __init__(self, pr_url: str, args=None, ai_handler=None):
        """
        Initialize the PRConfig object with the necessary attributes and objects to comment on a pull request.

        Args:
            pr_url (str): The URL of the pull request to be reviewed.
            args (list, optional): List of arguments passed to the PRReviewer class. Defaults to None.
        """
        self.git_provider = get_git_provider()(pr_url)

    async def run(self):
        get_logger().info('Getting configuration settings...')
        get_logger().info('Preparing configs...')
        pr_comment = self._prepare_pr_configs()
        if get_settings().config.publish_output:
            get_logger().info('Pushing configs...')
            self.git_provider.publish_comment(pr_comment)
            self.git_provider.remove_initial_comment()
        return ""

    def _prepare_pr_configs(self) -> str:
        try:
            conf_file = get_settings().find_file("configuration.toml")
            dynconf_kwargs = {'core_loaders': [],  # DISABLE default loaders, otherwise will load toml files more than once.
                 'loaders': ['pr_agent.custom_merge_loader'],
                 # Use a custom loader to merge sections, but overwrite their overlapping values. Do not use ENV variables.
                 'merge_enabled': True
                 # Merge multiple TOML files; prevent full section overwrite—only overlapping keys in sections overwrite prior ones.
             }
            conf_settings = Dynaconf(settings_files=[conf_file],
                                     # Security: Disable all dynamic loading features
                                     load_dotenv=False,  # Don't load .env files
                                     envvar_prefix=False,
                                     **dynconf_kwargs
                                     )
        except Exception as e:
            get_logger().error("Caught exception during Dynaconf loading. Returning empty dict",
                               artifact={"exception": e})
            conf_settings = {}
        markdown_text = self._prepare_mcp_status_block()
        configuration_headers = [header.lower() for header in conf_settings.keys()]
        relevant_configs = {
            header: configs for header, configs in get_settings().to_dict().items()
            if (header.lower().startswith("pr_") or header.lower().startswith("config")) and header.lower() in configuration_headers
        }

        skip_keys = ['ai_disclaimer', 'ai_disclaimer_title', 'ANALYTICS_FOLDER', 'secret_provider', "skip_keys", "app_id", "redirect",
                     'trial_prefix_message', 'no_eligible_message', 'identity_provider', 'ALLOWED_REPOS',
                     'APP_NAME', 'PERSONAL_ACCESS_TOKEN', 'shared_secret', 'key', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'user_token',
                     'private_key', 'private_key_id', 'client_id', 'client_secret', 'token', 'bearer_token', 'jira_api_token','webhook_secret']
        partial_skip_keys = ['key', 'secret', 'token', 'private']
        extra_skip_keys = get_settings().config.get('config.skip_keys', [])
        if extra_skip_keys:
            skip_keys.extend(extra_skip_keys)
        skip_keys_lower = [key.lower() for key in skip_keys]


        markdown_text += "<details> <summary><strong>🛠️ PR-Agent Configurations:</strong></summary> \n\n"
        markdown_text += f"\n\n```yaml\n\n"
        for header, configs in relevant_configs.items():
            if configs:
                markdown_text += "\n\n"
                markdown_text += f"==================== {header} ===================="
            for key, value in configs.items():
                if key.lower() in skip_keys_lower:
                    continue
                if any(skip_key in key.lower() for skip_key in partial_skip_keys):
                    continue
                markdown_text += f"\n{header.lower()}.{key.lower()} = {repr(value) if isinstance(value, str) else value}"
                markdown_text += "  "
        markdown_text += "\n```"
        markdown_text += "\n</details>\n"
        get_logger().info(f"Possible Configurations outputted to PR comment", artifact=markdown_text)
        return markdown_text

    def _prepare_mcp_status_block(self) -> str:
        try:
            status = MCPRuntime().get_status()
        except (MCPRuntimeError, ValueError, TypeError, KeyError):
            return ""
        if not status["enabled"] and not status["configured_servers"]:
            return ""

        markdown_text = "<details> <summary><strong> MCP Runtime Status </strong></summary><hr>\n\n"
        markdown_text += "```yaml\n"
        markdown_text += f"mcp.enabled = {status['enabled']}\n"
        markdown_text += f"mcp.configured_servers = {status['configured_servers']}\n"
        markdown_text += f"mcp.connected_servers = {status['connected_servers']}\n"
        markdown_text += "```\n"
        markdown_text += "</details>\n\n"
        return markdown_text
