## Overview

The similar issue tool retrieves the most similar issues to the current issue.
It can be invoked manually by commenting on any PR:

```
/similar_issue
```

## Example usage

![similar_issue_original_issue](https://codium.ai/images/pr_agent/similar_issue_original_issue.png){width=768}

![similar_issue_comment](https://codium.ai/images/pr_agent/similar_issue_comment.png){width=768}

![similar_issue](https://codium.ai/images/pr_agent/similar_issue.png){width=768}

Note that to perform retrieval, the `similar_issue` tool indexes all the repo previous issues (once).

### Selecting a Vector Database

Configure your preferred database by changing the `pr_similar_issue` parameter in `configuration.toml` file.

#### Available Options

Choose from the following Vector Databases:

1. LanceDB
2. Pinecone
3. Qdrant

#### Pinecone Configuration

To use Pinecone with the `similar issue` tool, add these credentials to `.secrets.toml` (or set as environment variables):

```
[pinecone]
api_key = "..."
environment = "..."
```

These parameters can be obtained by registering to [Pinecone](https://app.pinecone.io/?sessionType=signup/).

#### Qdrant Configuration

To use Qdrant with the `similar issue` tool, add these credentials to `.secrets.toml` (or set as environment variables):

```
[qdrant]
url = "https://YOUR-QDRANT-URL" # e.g., https://xxxxxxxx-xxxxxxxx.eu-central-1-0.aws.cloud.qdrant.io
api_key = "..."
```

Then select Qdrant in `configuration.toml`:

```
[pr_similar_issue]
vectordb = "qdrant"
```

You can get a free managed Qdrant instance from [Qdrant Cloud](https://cloud.qdrant.io/).

Qdrant points are stored in a collection named `codium-ai-pr-agent-issues-v2`.

!!! note "Upgrading an index created before the point-id fix"

    Earlier versions derived the point id from the issue id alone, so the same issue number collided
    across repositories. The id is now seeded with the repository name, which means points written by
    an earlier version are never rewritten or deleted - they still carry a matching `metadata.repo`
    payload, so they stay queryable and can surface alongside their replacements.

    The `-v2` collection suffix sidesteps this: the new index is written to
    `codium-ai-pr-agent-issues-v2`, leaving the pre-existing `codium-ai-pr-agent-issues` collection
    untouched. Nothing is deleted, and the first run after the upgrade re-indexes the repository into
    the new collection. Once you are satisfied with the results, you can delete the old
    `codium-ai-pr-agent-issues` collection from Qdrant by hand to reclaim the storage.

## How to use

- Install the tool's extra dependencies (vector databases and datasets), which a bare `uv sync` does not include:
`uv sync --group similar-issue`

- To invoke the 'similar issue' tool from **CLI**, run:
`uv run pr-agent --issue_url=... similar_issue`

- To invoke the 'similar' issue tool via online usage, [comment](https://github.com/the-pr-agent/pr-agent/issues/178#issuecomment-1716934893) on a PR:
`/similar_issue`

- You can also enable the 'similar issue' tool to run automatically when a new issue is opened, by adding it to the [pr_commands list in the github_app section](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml#L229)
