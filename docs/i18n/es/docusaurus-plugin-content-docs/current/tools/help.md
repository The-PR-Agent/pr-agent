---
title: "Help"
sidebar_position: 9
---

## Overview

The `help` tool provides a list of all the available tools and their descriptions.
For PR-Agent users, it also enables to trigger each tool by checking the relevant box.

It can be invoked manually by commenting on any PR:

```
/help
```

Calling `/help` without a question displays the tool list without initializing an AI handler,
so errors during that initialization do not block the list.
`/help <question>` still uses the configured AI model.

When answering a question, the tool tries the configured fallback models if a model returns an invalid response format
or an empty answer. If all attempts fail, no answer is published and the command records a failure.
A response that reports no relevant information is still shown normally.

## Example usage

Invoke the `help` tool by commenting on a PR with:

<img src="/img/help1.png" alt="Help tool input" width="750" />


Response will include a list of available tools:

<img src="/img/help2.webp" alt="Help tool output" width="750" />
