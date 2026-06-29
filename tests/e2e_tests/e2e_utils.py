FILE_PATH: str = "pr_agent/cli_pip.py"

PR_HEADER_START_WITH = '### **User description**\nupdate cli_pip.py\n\n\n___\n\n### **PR Type**'
REVIEW_START_WITH = '## PR Reviewer Guide 🔍\n\n<table>\n<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>:'
IMPROVE_START_WITH_REGEX_PATTERN = r'^## PR Code Suggestions ✨\n\n<!-- [a-z0-9]+ -->\n\n<table><thead><tr><td>Category</td>'

NUM_MINUTES: int = 5

NEW_FILE_CONTENT: str = """\
from pr_agent import cli
from pr_agent.config_loader import get_settings


def main():
    # Fill in the following values
    provider = "github"  # GitHub provider
    user_token = "..."  # GitHub user token
    openai_key = "ghs_afsdfasdfsdf"  # Example OpenAI key
    pr_url = "..."  # PR URL, for example 'https://github.com/Codium-ai/pr-agent/pull/809'
    command = "/improve"  # Command to run (e.g. '/review', '/describe', 'improve', '/ask="What is the purpose of this PR?"')

    # Setting the configurations
    get_settings().set("CONFIG.git_provider", provider)
    get_settings().set("openai.key", openai_key)
    get_settings().set("github.user_token", user_token)

    # Run the command. Feedback will appear in GitHub PR comments
    output = cli.run_command(pr_url, command)

    print(output)

if __name__ == '__main__':
    main()
"""
