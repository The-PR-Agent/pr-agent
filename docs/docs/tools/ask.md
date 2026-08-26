## Overview

The `ask` tool answers questions about the PR, based on the PR code changes. Make sure to be specific and clear in your questions.
It can be invoked manually by commenting on any PR:

```
/ask "..."
```

## Example usage

![Ask Comment](https://codium.ai/images/pr_agent/ask_comment.png){width=512}

![Ask](https://codium.ai/images/pr_agent/ask.png){width=512}

## Ask lines

You can run `/ask` on specific lines of code in the PR from the PR's diff view. The tool will answer questions based on the code changes in the selected lines.

- Click on the '+' sign next to the line number to select the line.
- To select multiple lines, click on the '+' sign of the first line and then hold and drag to select the rest of the lines.
- write `/ask "..."` in the comment box and press `Add single comment` button.

![Ask Line](https://codium.ai/images/pr_agent/Ask_line.png){width=512}

Note that the tool does not have "memory" of previous questions, and answers each question independently.

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

1\. First, post a comment that contains **only** the image:

![Ask image1](https://codium.ai/images/pr_agent/ask_images1.png){width=512}

2\. Quote reply to that comment:

![Ask image2](https://codium.ai/images/pr_agent/ask_images2.png){width=512}

3\. In the screen opened, type the question below the image:

![Ask image3](https://codium.ai/images/pr_agent/ask_images3.png){width=512}
![Ask image4](https://codium.ai/images/pr_agent/ask_images4.png){width=512}

4\. Post the comment, and receive the answer:

![Ask image5](https://codium.ai/images/pr_agent/ask_images5.png){width=512}

See a full video tutorial [here](https://codium.ai/images/pr_agent/ask_image_video.mov)

## Configuration options

???+ example "General options"

    <table>
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
        <td>If set to true, the tool will use the conversation history when answering questions on specific code lines (GitHub only). Default is true.</td>
      </tr>
    </table>

Example usage in a configuration file:

```toml
[pr_questions]
extra_instructions = "Do not answer questions that ask to rate PR quality on a scale of 1 to 10."
```

Example usage in a PR comment:

```
/ask "What does this change do?" --pr_questions.extra_instructions="Answer in one short paragraph."
```
