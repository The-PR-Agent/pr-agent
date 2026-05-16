---
title: "Compression Strategy"
sidebar_position: 2
---


`Supported Git Platforms: GitHub, GitLab, Bitbucket`


## Overview

There are two scenarios:

1. The PR is small enough to fit in a single prompt (including system and user prompt)
2. The PR is too large to fit in a single prompt (including system and user prompt)

For both scenarios, we first use the following strategy

#### Repo language prioritization strategy

We prioritize the languages of the repo based on the following criteria:

1. Exclude binary files and non code files (e.g. images, pdfs, etc)
2. Given the main languages used in the repo
3. We sort the PR files by the most common languages in the repo (in descending order):
   * ```[[file.py, file2.py],[file3.js, file4.jsx],[readme.md]]```

### Small PR

In this case, we can fit the entire PR in a single prompt:

1. Exclude binary files and non code files (e.g. images, pdfs, etc)
2. We Expand the surrounding context of each patch to 3 lines above and below the patch

### Large PR

#### Motivation

Pull Requests can be very long and contain a lot of information with varying degree of relevance to the pr-agent.
We want to be able to pack as much information as possible in a single LMM prompt, while keeping the information relevant to the pr-agent.

#### Compression strategy

We prioritize additions over deletions:

* Combine all deleted files into a single list (`deleted files`)
* File patches are a list of hunks, remove all hunks of type deletion-only from the hunks in the file patch

#### Adaptive and token-aware file patch fitting

We use [tiktoken](https://github.com/openai/tiktoken) to tokenize the patches after the modifications described above, and we use the following strategy to fit the patches into the prompt:

1. Within each language we sort the files by the number of tokens in the file (in descending order):
    * ```[[file2.py, file.py],[file4.jsx, file3.js],[readme.md]]```
2. Iterate through the patches in the order described above
3. Add the patches to the prompt until the prompt reaches a certain buffer from the max token length
4. If there are still patches left, add the remaining patches as a list called `other modified files` to the prompt until the prompt reaches the max token length (hard stop), skip the rest of the patches.
5. If we haven't reached the max token length, add the `deleted files` to the prompt until the prompt reaches the max token length (hard stop), skip the rest of the patches.

#### Example

<img src="https://qodo.ai/images/git_patch_logic.png" alt="Core Abilities" width="768" />
