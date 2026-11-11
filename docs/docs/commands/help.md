---
title: "Help"
sidebar_position: 9
---

## Overview

The `help` command provides a list of all the available commands and their descriptions.
Invoke commands by posting their slash commands. The checkboxes shown on GitHub do not execute commands when checked.

On Bitbucket Cloud, Gerrit and CodeCommit, `/help` without a question displays an unsupported-format notice instead of the command list. `/help <question>` can still answer questions from the documentation.

Local mode only displays the unsupported-format notice for `/help`. Plain-diff mode supports the command list, but `/help <question>` is currently unavailable in both local and plain-diff modes.

It can be invoked manually by commenting on any PR:

```
/help
```

Calling `/help` without a question displays the command list, where supported, without initializing an AI handler,
so errors during that initialization do not block the list.
`/help <question>` still uses the configured AI model.

When answering a question, the command tries the configured fallback models if a model returns an invalid response format
or an empty answer. If all attempts fail, no answer is published and the command records a failure.
A response that reports no relevant information is still shown normally.

## Example usage

Invoke the `help` command by commenting on a PR with:

<img src="/img/help1.png" alt="Help command input" width="750" />


Response will include a list of available commands:

<img src="/img/help2.webp" alt="Help command output" width="750" />
