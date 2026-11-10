---
title: "Ask"
sidebar_position: 5
---

## Overview

The `ask` tool answers questions about the PR, based on the PR code changes. Make sure to be specific and clear in your questions.
It can be invoked manually by commenting on any PR:

```
/ask "..."
```

## Example usage

<img src="/img/ask_comment.png" alt="Ask Comment" width="512" />

<img src="/img/ask.png" alt="Ask" width="512" />

## Repository instructions

Top-level `/ask` can use the same repository instruction files as `/review`, `/describe`, and `/improve`.
For example, ask whether a migration follows the rollback rules in an existing architecture decision record:

```toml
[config]
repo_context_files = ["AGENTS.md", "docs/adr/database-migrations.md"]
```

```text
/ask "Does this migration follow our rollback and compatibility rules?"
```

Like `/review`, `/describe`, and `/improve`, top-level `/ask` reads `AGENTS.md` from the repository root by default.
Set `config.repo_context_files = []` in the root repository settings to disable repository instructions for all tools.
Comment arguments cannot change the configured file list.

The existing [repository context settings](../usage-guide/additional_configurations.md#bringing-per-repo-context-files-to-pr-agent)
control file selection, source branches, imports, and line limits. Files come from the repository's default branch unless
configured otherwise. Providers without repository file access skip this context; missing or empty files are also skipped.
This behavior does not affect `/ask_line`.

The question, selected discussion history, and packed diff keep their existing token budget. The instruction block,
already bounded by `repo_context_max_lines`, is included only if it fits in the remaining model context in full.
Otherwise, `/ask` keeps its original prompt and adds a short omission notice to the answer. It does not retry another model
just to fit instructions. Normal model retries reconsider the original instruction block without reading the files again.
Including repository instructions can add repository API reads and input tokens, but does not add a separate model request.

## Ask lines

You can run `/ask` on specific lines of code in the PR from the PR's diff view. The tool will answer questions based on the code changes in the selected lines.

- Click on the '+' sign next to the line number to select the line.
- To select multiple lines, click on the '+' sign of the first line and then hold and drag to select the rest of the lines.
- write `/ask "..."` in the comment box and press `Add single comment` button.

<img src="/img/Ask_line.png" alt="Ask Line" width="512" />

If no matching diff hunk is found for the selected lines, the tool explains this without calling a model.

With `pr_questions.use_conversation_history=true` (the default), line questions on GitHub and Azure DevOps can include earlier comments from the same thread. Azure DevOps also supports history for threaded top-level questions. Other questions are answered independently.

## Ask on images

You can also ask questions about images that appear in the comment, where the entire PR code will be used as context.
<br>
The basic syntax is:

```
/ask "..."

[Image](https://real_link_to_image)
```

where `https://real_link_to_image` is the direct link to the image.

Note that GitHub has a built-in mechanism of pasting images in comments. However, pasted image does not provide a direct link.
To get a direct link to an image, we recommend using the following scheme:

1. First, post a comment that contains **only** the image:

   <img src="/img/ask_images1.png" alt="Ask image1" width="512" />

2. Quote reply to that comment:

   <img src="/img/ask_images2.png" alt="Ask image2" width="512" />

3. In the screen opened, type the question below the image:

   <img src="/img/ask_images3.png" alt="Ask image3" width="512" />
   <img src="/img/ask_images4.png" alt="Ask image4" width="512" />

4. Post the comment, and receive the answer:

   <img src="/img/ask_images5.png" alt="Ask image5" width="512" />

See a full video tutorial [here](https://codium.ai/images/pr_agent/ask_image_video.mov)

## Configuration options

<details open>
<summary>General options</summary>

<table>
  <tr>
    <td><b>ask_heading</b></td>
    <td>
      Plain-text heading for top-level <code>/ask</code> answers. The default is <code>Ask</code>.
      Markdown renderers escape punctuation to keep the surrounding formatting and ❓ emoji fixed,
      while plain-text converters publish the configured text without escape characters.
      This does not affect <code>/ask_line</code> replies or the <code>Answer</code> section heading.
    </td>
  </tr>
  <tr>
    <td><b>extra_instructions</b></td>
    <td>Optional extra instructions to the tool. For example: "Do not answer questions that ask to rate PR quality on a scale of 1 to 10. Instead, tell the user this type of question is not allowed."</td>
  </tr>
  <tr>
    <td><b>enable_help_text</b></td>
    <td>If set to true, the tool will display a help text in the comment. Default is false.</td>
  </tr>
  <tr>
    <td><b>use_conversation_history</b></td>
    <td>If set to true, the tool will use the conversation history from the same thread when answering questions on specific code lines (GitHub and Azure DevOps) or threaded top-level questions (Azure DevOps only). Default is true.</td>
  </tr>
</table>

</details>

Example usage in a configuration file:

```toml
[pr_questions]
ask_heading = "Architecture Question"
extra_instructions = "Do not answer questions that ask to rate PR quality on a scale of 1 to 10."
```

The heading above renders as `### **Architecture Question** ❓`.

Example usage in a PR comment:

```
/ask "What does this change do?" --pr_questions.extra_instructions="Answer in one short paragraph."
```
