from pr_agent.command_descriptions import COMMAND_DESCRIPTIONS
from pr_agent.config_loader import get_bot_display_name


class HelpMessage:
    @staticmethod
    def get_general_commands_text():
       commands_text = f"> - **/review**: {COMMAND_DESCRIPTIONS['review']}   \n" \
                f"> - **/describe**: {COMMAND_DESCRIPTIONS['describe']}   \n" \
                f"> - **/improve**: {COMMAND_DESCRIPTIONS['improve']}   \n" \
                "> - **/ask \\<QUESTION\\>**: Ask a question about the PR.   \n" \
                "> - **/update_changelog**: Update the changelog based on the PR's contents.   \n" \
                "> - **/add_docs**: Generate docstring for new components introduced in the PR.   \n" \
                "> - **/generate_labels**: Generate labels for the PR based on the PR's contents.   \n\n" \
                ">See the [commands guide](https://docs.pr-agent.ai/commands/) for more details.\n" \
                ">To list the possible configuration parameters, add a **/config** comment.   \n"
       return commands_text


    @staticmethod
    def get_general_bot_help_text():
        output = f"> To invoke the {get_bot_display_name()}, add a comment using one of the following commands:  \n{HelpMessage.get_general_commands_text()} \n"
        return output

    @staticmethod
    def get_review_usage_guide():
        output ="**Overview:**\n"
        output += (f"{COMMAND_DESCRIPTIONS['review']} "
                  "More feedback can be [added](https://docs.pr-agent.ai/commands/review/#configuration-options) by configuring the command.\n\n"
                  "The command can be triggered [automatically](https://docs.pr-agent.ai/usage-guide/automations_and_usage/#github-app-automatic-commands-when-a-new-pr-is-opened) every time a new PR is opened, or can be invoked manually by commenting on any PR.\n")
        output +="""\
- When commenting, to edit [configurations](https://docs.pr-agent.ai/usage-guide/configuration_reference/#pr_reviewer-review) related to the review command (`pr_reviewer` section), use the following template:
```
/review --pr_reviewer.some_config1=... --pr_reviewer.some_config2=...
```
- With a [configuration file](https://docs.pr-agent.ai/usage-guide/configuration_options/), use the following template:
```
[pr_reviewer]
some_config1=...
some_config2=...
```
    """

        output += "\n\nSee the review [usage page](https://docs.pr-agent.ai/commands/review/) for a comprehensive guide on using this command.\n\n"

        return output



    @staticmethod
    def get_describe_usage_guide():
        output = "**Overview:**\n"
        output += f"{COMMAND_DESCRIPTIONS['describe']} "
        output += "The command can be triggered [automatically](https://docs.pr-agent.ai/usage-guide/automations_and_usage/#github-app-automatic-commands-when-a-new-pr-is-opened) every time a new PR is opened, or can be invoked manually by commenting on a PR.\n"
        output += """\

When commenting, to edit [configurations](https://docs.pr-agent.ai/usage-guide/configuration_reference/#pr_description-describe) related to the describe command (`pr_description` section), use the following template:
```
/describe --pr_description.some_config1=... --pr_description.some_config2=...
```
With a [configuration file](https://docs.pr-agent.ai/usage-guide/configuration_options/), use the following template:
```
[pr_description]
some_config1=...
some_config2=...
```
"""
        output += "\n\n<table>"

        # automation
        output += "<tr><td><details> <summary><strong> Enabling\\disabling automation </strong></summary><hr>\n\n"
        output += """\
- When you first install the app, the [default mode](https://docs.pr-agent.ai/usage-guide/automations_and_usage/#github-app-automatic-commands-when-a-new-pr-is-opened) for the describe command is:
```
pr_commands = ["/describe", ...]
```
meaning the `describe` command will run automatically on every PR.

- Markers are an alternative way to control the generated description, to give maximal control to the user. If you set:
```
pr_commands = ["/describe --pr_description.use_description_markers=true", ...]
```
the command will replace every marker of the form `pr_agent:marker_name` in the PR description with the relevant content, where `marker_name` is one of the following:
  - `type`: the PR type.
  - `summary`: the PR summary.
  - `walkthrough`: the PR walkthrough.
  - `diagram`: the PR flowchart (if enabled).

Note that when markers are enabled, if the original PR description does not contain any markers, the command will not alter the description at all.

"""
        output += "\n\n</details></td></tr>\n\n"

        # custom labels
        output += "<tr><td><details> <summary><strong> Custom labels </strong></summary><hr>\n\n"
        output += """\
The default labels of the `describe` command are quite generic: [`Bug fix`, `Tests`, `Enhancement`, `Documentation`, `Other`].

If you specify [custom labels](https://docs.pr-agent.ai/commands/describe/#handle-custom-labels-from-the-repos-labels-page) in the repo's labels page or via configuration file, you can get tailored labels for your use cases.
Examples for custom labels:
- `Main topic:performance` - pr_agent:The main topic of this PR is performance
- `New endpoint` - pr_agent:A new endpoint was added in this PR
- `SQL query` - pr_agent:A new SQL query was added in this PR
- `Dockerfile changes` - pr_agent:The PR contains changes in the Dockerfile
- ...

The list above is eclectic, and aims to give an idea of different possibilities. Define custom labels that are relevant for your repo and use cases.
Note that Labels are not mutually exclusive, so you can add multiple label categories.
Make sure to provide proper title, and a detailed and well-phrased description for each label, so the command will know when to suggest it.
"""
        output += "\n\n</details></td></tr>\n\n"

        # extra instructions
        output += "<tr><td><details> <summary><strong> Utilizing extra instructions</strong></summary><hr>\n\n"
        output += '''\
The `describe` command can be configured with extra instructions, to guide the model to a feedback tailored to the needs of your project.

Be specific, clear, and concise in the instructions. With extra instructions, you are the prompter. Notice that the general structure of the description is fixed, and cannot be changed. Extra instructions can change the content or style of each sub-section of the PR description.

Examples for extra instructions:
```
[pr_description]
extra_instructions="""\
- The PR title should be in the format: '<PR type>: <title>'
- The title should be short and concise (up to 10 words)
- ...
"""
```
Use triple quotes to write multi-line instructions. Use bullet points to make the instructions more readable.
'''
        output += "\n\n</details></td></tr>\n\n"


        # general
        output += f"\n\n<tr><td><details> <summary><strong> More {get_bot_display_name()} commands</strong></summary><hr> \n\n"
        output += HelpMessage.get_general_bot_help_text()
        output += "\n\n</details></td></tr>\n\n"

        output += "</table>"

        output += "\n\nSee the [describe usage](https://docs.pr-agent.ai/commands/describe/) page for a comprehensive guide on using this command.\n\n"

        return output

    @staticmethod
    def get_ask_usage_guide():
        output = "**Overview:**\n"
        output += """\
The `ask` command answers questions about the PR, based on the PR code changes.
It can be invoked manually by commenting on any PR:
```
/ask "..."
```

Note that the command does not have "memory" of previous questions, and answers each question independently.
You can ask questions about the entire PR, about specific code lines, or about an image related to the PR code changes.
        """
        # output += "\n\n<table>"
        #
        # # # general
        # # output += "\n\n<tr><td><details> <summary><strong> More PR-Agent commands</strong></summary><hr> \n\n"
        # # output += HelpMessage.get_general_bot_help_text()
        # # output += "\n\n</details></td></tr>\n\n"
        #
        # output += "</table>"

        output += "\n\nSee the [ask usage](https://docs.pr-agent.ai/commands/ask/) page for a comprehensive guide on using this command.\n\n"

        return output


    @staticmethod
    def get_improve_usage_guide():
        output = "**Overview:**\n"
        output += f"{COMMAND_DESCRIPTIONS['improve']} "
        output += "Configure it through the [review settings](https://docs.pr-agent.ai/commands/review/#configuration-options).\n"
        output += "\n\nSee the improve [usage page](https://docs.pr-agent.ai/commands/improve/) for a comprehensive guide on using this command.\n\n"

        return output


    @staticmethod
    def get_help_docs_usage_guide():
        output = "**Overview:**\n"
        output += """\
The help docs command, named `help_docs`, answers a question based on a given relative path of documentation, either from the repository of this merge request or from a given one."
It can be invoked manually by commenting on any PR:
```
/help_docs "..."
```
"""
        output += "\n\nSee the [help_docs usage](https://docs.pr-agent.ai/commands/help_docs/) page for a comprehensive guide on using this command.\n\n"
        return output
