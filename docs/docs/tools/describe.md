## Overview

The `describe` tool scans the PR code changes, and generates a description for the PR - title, type, summary, walkthrough and labels.

The tool can be triggered automatically every time a new PR is [opened](../usage-guide/automations_and_usage.md#github-app-automatic-tools-when-a-new-pr-is-opened), or it can be invoked manually by commenting on any PR:

```
/describe
```

## Example usage

### Manual triggering

Invoke the tool manually by commenting `/describe` on any PR:

![Describe comment](https://codium.ai/images/pr_agent/describe_comment.png){width=512}

After ~30 seconds, the tool will generate a description for the PR:

![Describe New](https://codium.ai/images/pr_agent/describe_new.png){width=512}

If you want to edit [configurations](#configuration-options), add the relevant ones to the command:

```
/describe --pr_description.some_config1=... --pr_description.some_config2=...
```

### Automatic triggering

To run the `describe` automatically when a PR is opened, define in a [configuration file](../usage-guide/configuration_options.md#wiki-configuration-file):

```
[github_app]
pr_commands = [
    "/describe",
    ...
]

[pr_description]
publish_labels = true
...
```

- The `pr_commands` lists commands that will be executed automatically when a PR is opened.
- The `[pr_description]` section contains the configurations for the `describe` tool you want to edit (if any).

## Preserving the original user description

By default, PR-Agent tries to preserve your original PR description by placing it above the generated content.
This requires including your description during the initial PR creation.

"PR-Agent removed the original description from the PR. Why"?

From our experience, there are two possible reasons:

- If you edit the description _while_ the automated tool is running, a race condition may occur, potentially causing your original description to be lost. Hence, create a description before launching the PR.

- When _updating_ PR descriptions, the `/describe` tool considers everything above the "PR Type" field as user content and will preserve it.
Everything below this marker is treated as previously auto-generated content and will be replaced.

![Describe comment](https://codium.ai/images/pr_agent/pr_description_user_description.png){width=512}

## Sequence Diagram Support 
The `/describe` tool includes a Mermaid sequence diagram showing component/function interactions. 

This option is enabled by default via the `pr_description.enable_pr_diagram` param.


[//]: # (### How to enable\disable)

[//]: # ()
[//]: # (In your configuration:)

[//]: # ()
[//]: # (```)

[//]: # (toml)

[//]: # ([pr_description])

[//]: # (enable_pr_diagram = true)

[//]: # (```)

## Configuration options

???+ example "Possible configurations"

    <table>
      <tr>
        <td><b>publish_labels</b></td>
        <td>If set to true, the tool will publish labels to the PR. Default is false.</td>
      </tr>
      <tr>
        <td><b>publish_description_as_comment</b></td>
        <td>If set to true, the tool will publish the description as a comment to the PR. If false, it will overwrite the original description. Default is false.</td>
      </tr>
      <tr>
        <td><b>publish_description_as_comment_persistent</b></td>
        <td>If set to true and `publish_description_as_comment` is true, the tool will publish the description as a persistent comment to the PR. Default is true.</td>
      </tr>
      <tr>
        <td><b>add_original_user_description</b></td>
        <td>If set to true, the tool will add the original user description to the generated description. Default is true.</td>
      </tr>
      <tr>
        <td><b>generate_ai_title</b></td>
        <td>If set to true, the tool will also generate an AI title for the PR. Default is false.</td>
      </tr>
      <tr>
        <td><b>extra_instructions</b></td>
        <td>Optional extra instructions to the tool. For example: "focus on the changes in the file X. Ignore change in ..."</td>
      </tr>
      <tr>
        <td><b>enable_pr_type</b></td>
        <td>If set to false, it will not show the `PR type` as a text value in the description content. Default is true.</td>
      </tr>
      <tr>
        <td><b>final_update_message</b></td>
        <td>If set to true, it will add a comment message [`PR Description updated to latest commit...`](https://github.com/the-pr-agent/pr-agent/pull/499#issuecomment-1837412176) after finishing calling `/describe`. Default is true.</td>
      </tr>
      <tr>
        <td><b>enable_semantic_files_types</b></td>
        <td>If set to true, "Changes walkthrough" section will be generated. Default is true.</td>
      </tr>
      <tr>
            <td><b>file_table_collapsible_open_by_default</b></td>
            <td>If set to true, the file list in the "Changes walkthrough" section will be open by default. If set to false, it will be closed by default. Default is false.</td>
      </tr>
      <tr>
        <td><b>collapsible_file_list</b></td>
        <td>If set to true, the file list in the "Changes walkthrough" section will be collapsible. If set to "adaptive", the file list will be collapsible only if there are more than 8 files. Default is "adaptive".</td>
      </tr>
      <tr>
        <td><b>enable_large_pr_handling</b></td>
        <td>If set to true, in case of a large PR the tool will make several calls to the AI and combine them to be able to cover more files. Default is true.</td>
      </tr>
      <tr>
        <td><b>enable_help_text</b></td>
        <td>If set to true, the tool will display a help text in the comment. Default is false.</td>
      </tr>
      <tr>
        <td><b>enable_pr_diagram</b></td>
        <td>If set to true, the tool will generate a horizontal Mermaid flowchart summarizing the main pull request changes. This field remains empty if not applicable. Default is true.</td>
      </tr>
      <tr>
        <td><b>auto_create_ticket</b></td>
        <td>If set to true, this will automatically create a ticket in the ticketing system when a PR is opened. Default is false.</td>
      </tr>
    </table>

## Markers template

To enable markers, set `pr_description.use_description_markers=true`.
Markers enable to easily integrate user's content and auto-generated content, with a template-like mechanism.

For example, if the PR original description was:

```
User content...

## PR Type:
pr_agent:type

## PR Description:
pr_agent:summary

## PR Walkthrough:
pr_agent:walkthrough

## PR Diagram:
pr_agent:diagram
```

The marker `pr_agent:type` will be replaced with the PR type, `pr_agent:summary` will be replaced with the PR summary, `pr_agent:walkthrough` will be replaced with the PR walkthrough, and `pr_agent:diagram` will be replaced with the sequence diagram (if enabled).

![Describe markers before](https://codium.ai/images/pr_agent/describe_markers_before.png){width=512}

becomes

![Describe markers after](https://codium.ai/images/pr_agent/describe_markers_after.png){width=512}

**Configuration params**:

- `use_description_markers`: if set to true, the tool will use markers template. It replaces every marker of the form `pr_agent:marker_name` with the relevant content. Default is false.
- `include_generated_by_header`: if set to true, the tool will add a dedicated header: 'Generated by PR Agent at ...' to any automatic content. Default is true.
- `diagram`: if present as a marker, will be replaced by the PR sequence diagram (if enabled).

## Custom labels

The default labels of the describe tool are quite generic, since they are meant to be used in any repo: [`Bug fix`, `Tests`, `Enhancement`, `Documentation`, `Other`].

You can define custom labels that are relevant for your repo and use cases.
Custom labels can be defined in a configuration file, or directly in the repo's [labels page](#handle-custom-labels-from-the-repos-labels-page).

Make sure to provide proper title, and a detailed and well-phrased description for each label, so the tool will know when to suggest it.
Each label description should be a **conditional statement**, that indicates if to add the label to the PR or not, according to the PR content.

???+ tip "Auto-remove custom label when no longer relevant"
    If the custom label is no longer relevant, it will be automatically removed from the PR by running the `generate_labels` tool or the `describe` tool.


### Handle custom labels from a configuration file

Example for a custom labels configuration setup in a configuration file:

```
[config]
enable_custom_labels=true


[custom_labels."sql_changes"]
description = "Use when a PR contains changes to SQL queries"

[custom_labels."test"]
description = "use when a PR primarily contains new tests"

...
```

### Handle custom labels from the Repo's labels page

You can also control the custom labels that will be suggested by the `describe` tool from the repo's labels page:

- GitHub : go to `https://github.com/{owner}/{repo}/labels` (or click on the "Labels" tab in the issues or PRs page)
- GitLab : go to `https://gitlab.com/{owner}/{repo}/-/labels` (or click on "Manage" -> "Labels" on the left menu)

Now add/edit the custom labels. they should be formatted as follows:

- Label name: The name of the custom label.
- Description: Start the description of with prefix `pr_agent:`, for example: `pr_agent: Description of when AI should suggest this label`.<br>

Examples for custom labels:

- `Main topic:performance` -  pr_agent:The main topic of this PR is performance
- `New endpoint` -  pr_agent:A new endpoint was added in this PR
- `SQL query` -  pr_agent:A new SQL query was added in this PR
- `Dockerfile changes` - pr_agent:The PR contains changes in the Dockerfile
- ...

The description should be comprehensive and detailed, indicating when to add the desired label. For example:
![Add native custom labels](https://codium.ai/images/pr_agent/add_native_custom_labels.png){width=768}

## Usage Tips

!!! tip "Automation"
    - When you first install PR-Agent app, the [default mode](../usage-guide/automations_and_usage.md#github-app) for the describe tool is:
    ```
    pr_commands = ["/describe", ...]
    ```
    meaning the `describe` tool will run automatically on every PR, with the default configurations.

- Markers are an alternative way to control the generated description, to give maximal control to the user. If you set:

   ```
   pr_commands = ["/describe --pr_description.use_description_markers=true", ...]
   ```

   the tool will replace every marker of the form `pr_agent:marker_name` in the PR description with the relevant content, where `marker_name` is one of the following:
         *`type`: the PR type.
         * `summary`: the PR summary.
         * `walkthrough`: the PR walkthrough.

- Note that when markers are enabled, if the original PR description does not contain any markers, the tool will not alter the description at all.
