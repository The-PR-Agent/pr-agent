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

<img src="https://qodo.ai/images/pr_agent/ask_comment.png" alt="Ask Comment" width="512" />

<img src="https://qodo.ai/images/pr_agent/ask.png" alt="Ask" width="512" />

## Ask lines

You can run `/ask` on specific lines of code in the PR from the PR's diff view. The tool will answer questions based on the code changes in the selected lines.

- Click on the '+' sign next to the line number to select the line.
- To select multiple lines, click on the '+' sign of the first line and then hold and drag to select the rest of the lines.
- write `/ask "..."` in the comment box and press `Add single comment` button.

<img src="https://qodo.ai/images/pr_agent/Ask_line.png" alt="Ask Line" width="512" />

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

<img src="https://qodo.ai/images/pr_agent/ask_images1.png" alt="Ask image1" width="512" />

2\. Quote reply to that comment:

<img src="https://qodo.ai/images/pr_agent/ask_images2.png" alt="Ask image2" width="512" />

3\. In the screen opened, type the question below the image:

<img src="https://qodo.ai/images/pr_agent/ask_images3.png" alt="Ask image3" width="512" />
<img src="https://qodo.ai/images/pr_agent/ask_images4.png" alt="Ask image4" width="512" />

4\. Post the comment, and receive the answer:

<img src="https://qodo.ai/images/pr_agent/ask_images5.png" alt="Ask image5" width="512" />

See a full video tutorial [here](https://qodo.ai/images/pr_agent/ask_image_video.mov)
