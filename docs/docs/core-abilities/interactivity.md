# Interactivity

`Supported Git Platforms: GitHub, GitLab`

## Overview

PR-Agent transforms static code reviews into interactive experiences by enabling direct actions from pull request (PR) comments.
Developers can immediately trigger actions and apply changes with simple checkbox clicks.

This focused workflow maintains context while dramatically reducing the time between PR creation and final merge.
The approach eliminates manual steps, provides clear visual indicators, and creates immediate feedback loops all within the same interface.

## Key Interactive Features

### 1\. Interactive `/improve` Tool

The [`/improve`](../tools/improve.md) command delivers a comprehensive interactive experience:

- _**Apply this suggestion**_: Clicking this checkbox instantly converts a suggestion into a committable code change. When committed to the PR, changes made to code that was flagged for improvement will be marked with a check mark, allowing developers to easily track and review implemented recommendations.

- _**More**_: Triggers additional suggestions generation while keeping each suggestion focused and relevant as the original set

- _**Update**_: Triggers a re-analysis of the code, providing updated suggestions based on the latest changes

- _**Author self-review**_: Interactive acknowledgment that developers have opened and reviewed collapsed suggestions

### 2\. Interactive `/help` Tool

The [`/help`](../tools/help.md) command not only lists available tools and their descriptions but also enables immediate tool invocation through interactive checkboxes.
When a user checks a tool's checkbox, PR-Agent instantly triggers that tool without requiring additional commands.
This transforms the standard help menu into an interactive launch pad for all PR-Agent capabilities, eliminating context switching by keeping developers within their PR workflow.
