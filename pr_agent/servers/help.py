class HelpMessage:
    @staticmethod
    def get_general_commands_text():
       commands_text = "> - **/review**: Request a review of your Pull Request.   \n" \
                "> - **/describe**: Update the PR title and description based on the contents of the PR.   \n" \
                "> - **/improve [--extended]**: Suggest code improvements. Extended mode provides a higher quality feedback.   \n" \
                "> - **/ask \\<QUESTION\\>**: Ask a question about the PR.   \n" \
                "> - **/update_changelog**: Update the changelog based on the PR's contents.   \n" \
                "> - **/help_docs \\<QUESTION\\>**: Given a path to documentation (either for this repository or for a given one), ask a question.   \n" \
                "> - **/add_docs**: Generate docstring for new components introduced in the PR.   \n" \
                "> - **/generate_labels**: Generate labels for the PR based on the PR's contents.   \n\n" \
                ">See the [tools guide](https://pr-agent-docs.codium.ai/tools/) for more details.\n" \
                ">To list the possible configuration parameters, add a **/config** comment.   \n"
       return commands_text


    @staticmethod
    def get_general_bot_help_text():
        output = f"> To invoke the PR-Agent, add a comment using one of the following commands:  \n{HelpMessage.get_general_commands_text()} \n"
        return output

    @staticmethod
    def get_review_usage_guide():
        output ="**Overview:**\n"
        output +=("The `review` tool scans the PR code changes, and generates a PR review which includes several types of feedbacks, such as possible PR issues, security threats and relevant test in the PR. More feedbacks can be [added](https://pr-agent-docs.codium.ai/tools/review/#general-configurations) by configuring the tool.\n\n"
                  "The tool can be triggered [automatically](https://pr-agent-docs.codium.ai/usage-guide/automations_and_usage/#github-app-automatic-tools-when-a-new-pr-is-opened) every time a new PR is opened, or can be invoked manually by commenting on any PR.\n")
        output +="""\
- When commenting, to edit [configurations](https://github.com/Codium-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml#L23) related to the review tool (`pr_reviewer` section), use the following template:
```
/review --pr_reviewer.some_config1=... --pr_reviewer.some_config2=...
```
- With a [configuration file](https://pr-agent-docs.codium.ai/usage-guide/configuration_options/), use the following template:
```
[pr_reviewer]
some_config1=...
some_config2=...
```
    """

        output += f"\n\nSee the review [usage page](https://pr-agent-docs.codium.ai/tools/review/) for a comprehensive guide on using this tool.\n\n"

        return output



    @staticmethod
    def get_describe_usage_guide():
        output = "**Overview:**\n"
        output += "The `describe` tool scans the PR code changes, and generates a description for the PR - title, type, summary, walkthrough and labels. "
        output += "The tool can be triggered [automatically](https://pr-agent-docs.codium.ai/usage-guide/automations_and_usage/#github-app-automatic-tools-when-a-new-pr-is-opened) every time a new PR is opened, or can be invoked manually by commenting on a PR.\n"
        output += """\

When commenting, to edit [configurations](https://github.com/Codium-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml#L46) related to the describe tool (`pr_description` section), use the following template:
```
/describe --pr_description.some_config1=... --pr_description.some_config2=...
```
With a [configuration file](https://pr-agent-docs.codium.ai/usage-guide/configuration_options/), use the following template:
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
- When you first install the app, the [default mode](https://pr-agent-docs.codium.ai/usage-guide/automations_and_usage/#github-app-automatic-tools-when-a-new-pr-is-opened) for the describe tool is:
```
pr_commands = ["/describe", ...]
```
meaning the `describe` tool will run automatically on every PR.

- Markers are an alternative way to control the generated description, to give maximal control to the user. If you set:
```
pr_commands = ["/describe --pr_description.use_description_markers=true", ...]
```
the tool will replace every marker of the form `pr_agent:marker_name` in the PR description with the relevant content, where `marker_name` is one of the following:
  - `type`: the PR type.
  - `summary`: the PR summary.
  - `walkthrough`: the PR walkthrough.
  - `diagram`: the PR sequence diagram (if enabled).

Note that when markers are enabled, if the original PR description does not contain any markers, the tool will not alter the description at all.

"""
        output += "\n\n</details></td></tr>\n\n"

        # custom labels
        output += "<tr><td><details> <summary><strong> Custom labels </strong></summary><hr>\n\n"
        output += """\
The default labels of the `describe` tool are quite generic: [`Bug fix`, `Tests`, `Enhancement`, `Documentation`, `Other`].

If you specify [custom labels](https://pr-agent-docs.codium.ai/tools/describe/#handle-custom-labels-from-the-repos-labels-page) in the repo's labels page or via configuration file, you can get tailored labels for your use cases.
Examples for custom labels:
- `Main topic:performance` - pr_agent:The main topic of this PR is performance
- `New endpoint` - pr_agent:A new endpoint was added in this PR
- `SQL query` - pr_agent:A new SQL query was added in this PR
- `Dockerfile changes` - pr_agent:The PR contains changes in the Dockerfile
- ...

The list above is eclectic, and aims to give an idea of different possibilities. Define custom labels that are relevant for your repo and use cases.
Note that Labels are not mutually exclusive, so you can add multiple label categories.
Make sure to provide proper title, and a detailed and well-phrased description for each label, so the tool will know when to suggest it.
"""
        output += "\n\n</details></td></tr>\n\n"

        # extra instructions
        output += "<tr><td><details> <summary><strong> Utilizing extra instructions</strong></summary><hr>\n\n"
        output += '''\
The `describe` tool can be configured with extra instructions, to guide the model to a feedback tailored to the needs of your project.

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
        output += "\n\n<tr><td><details> <summary><strong> More PR-Agent commands</strong></summary><hr> \n\n"
        output += HelpMessage.get_general_bot_help_text()
        output += "\n\n</details></td></tr>\n\n"

        output += "</table>"

        output += f"\n\nSee the [describe usage](https://pr-agent-docs.codium.ai/tools/describe/) page for a comprehensive guide on using this tool.\n\n"

        return output

    @staticmethod
    def get_ask_usage_guide():
        output = "**Overview:**\n"
        output += """\
The `ask` tool answers questions about the PR, based on the PR code changes.
It can be invoked manually by commenting on any PR:
```
/ask "..."
```

Note that the tool does not have "memory" of previous questions, and answers each question independently.
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

        output += f"\n\nSee the [ask usage](https://pr-agent-docs.codium.ai/tools/ask/) page for a comprehensive guide on using this tool.\n\n"

        return output


    @staticmethod
    def get_improve_usage_guide():
        output = "**Overview:**\n"
        output += "The code suggestions tool, named `improve`, scans the PR code changes, and automatically generates code suggestions for improving the PR."
        output += "The tool can be triggered [automatically](https://pr-agent-docs.codium.ai/usage-guide/automations_and_usage/#github-app-automatic-tools-when-a-new-pr-is-opened) every time a new PR is opened, or can be invoked manually by commenting on a PR.\n"
        output += """\
- When commenting, to edit [configurations](https://github.com/Codium-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml#L78) related to the improve tool (`pr_code_suggestions` section), use the following template:

```
/improve --pr_code_suggestions.some_config1=... --pr_code_suggestions.some_config2=...
```

- With a [configuration file](https://pr-agent-docs.codium.ai/usage-guide/configuration_options/), use the following template:

```
[pr_code_suggestions]
some_config1=...
some_config2=...
```

"""

        output += f"\n\nSee the improve [usage page](https://pr-agent-docs.codium.ai/tools/improve/) for a comprehensive guide on using this tool.\n\n"

        return output


    @staticmethod
    def get_help_docs_usage_guide():
        output = "**Overview:**\n"
        output += """\
The help docs tool, named `help_docs`, answers a question based on a given relative path of documentation, either from the repository of this merge request or from a given one."
It can be invoked manually by commenting on any PR:
```
/help_docs "..."
```
"""
        output += f"\n\nSee the [help_docs usage](https://pr-agent-docs.codium.ai/tools/help_docs/) page for a comprehensive guide on using this tool.\n\n"
        return output
