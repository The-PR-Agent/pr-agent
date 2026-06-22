## Azure DevOps Pipeline

You can use a pre-built Action Docker image to run PR-Agent as an Azure DevOps pipeline.
Add the following file to your repository under `azure-pipelines.yml`:

```yaml
# Opt out of CI triggers
trigger: none

# Configure PR trigger
# pr:
#   branches:
#     include:
#     - '*'
#   autoCancel: true
#   drafts: false

# NOTE for Azure Repos Git:
# Azure Repos does not honor YAML pr: triggers. Configure Build Validation
# via Branch Policies instead (see note below). You can safely omit pr:.

stages:
- stage: pr_agent
  displayName: 'PR Agent Stage'
  jobs:
  - job: pr_agent_job
    displayName: 'PR Agent Job'
    pool:
      vmImage: 'ubuntu-latest'
    container:
      image: pragent/pr-agent:latest
      options: --entrypoint ""
    variables:
      - group: pr_agent
    steps:
    - script: |
        echo "Running PR Agent action step"

        # Construct PR_URL
        PR_URL="${SYSTEM_COLLECTIONURI}${SYSTEM_TEAMPROJECT}/_git/${BUILD_REPOSITORY_NAME}/pullrequest/${SYSTEM_PULLREQUEST_PULLREQUESTID}"
        echo "PR_URL=$PR_URL"

        # Extract organization URL from System.CollectionUri
        ORG_URL=$(echo "$(System.CollectionUri)" | sed 's/\/$//') # Remove trailing slash if present
        echo "Organization URL: $ORG_URL"

        export azure_devops__org="$ORG_URL"
        export config__git_provider="azure"

        pr-agent --pr_url="$PR_URL" describe
        pr-agent --pr_url="$PR_URL" review
        pr-agent --pr_url="$PR_URL" improve
      env:
        azure_devops__pat: $(azure_devops_pat)
        openai__key: $(OPENAI_KEY)
      displayName: 'Run PR-Agent'
```

This script will run PR-Agent on every new merge request, with the `improve`, `review`, and `describe` commands.
Note that you need to export the `azure_devops__pat` and `OPENAI_KEY` variables in the Azure DevOps pipeline settings (Pipelines -> Library -> + Variable group):

![PR-Agent](https://codium.ai/images/pr_agent/azure_devops_pipeline_secrets.png){width=468}

Make sure to give pipeline permissions to the `pr_agent` variable group.

> Note that Azure Pipelines lacks support for triggering workflows from PR comments. If you find a viable solution, please contribute it to our [issue tracker](https://github.com/the-pr-agent/pr-agent/issues)

### Azure Repos Git PR triggers and Build Validation

Azure Repos Git does not use YAML `pr:` triggers for pipelines. Instead, configure Build Validation on the target branch to run the PR Agent pipeline for pull requests:

1. Go to Project Settings → Repositories → Branches.
2. Select the target branch and open Branch Policies.
3. Under Build Validation, add a policy:
   - Select the PR Agent pipeline (the `azure-pipelines.yml` above).
   - Set it as Required.
4. Remove the `pr:` section from your YAML (not needed for Azure Repos Git).

This distinction applies specifically to Azure Repos Git. Other providers like GitHub and Bitbucket Cloud can use YAML-based PR triggers.

## Azure DevOps from CLI

To use Azure DevOps provider use the following settings in configuration.toml:

```toml
[config]
git_provider="azure"
```

Azure DevOps provider supports [PAT token](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate?view=azure-devops&tabs=Windows) or [DefaultAzureCredential](https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication-overview#authentication-in-server-environments) authentication.
PAT is faster to create, but has built-in expiration date, and will use the user identity for API calls.
Using DefaultAzureCredential you can use managed identity or Service principle, which are more secure and will create separate ADO user identity (via AAD) to the agent.

If PAT was chosen, you can assign the value in .secrets.toml.
If DefaultAzureCredential was chosen, you can assigned the additional env vars like AZURE_CLIENT_SECRET directly,
or use managed identity/az cli (for local development) without any additional configuration.
in any case, 'org' value must be assigned in .secrets.toml:

```toml
[azure_devops]
org = "https://dev.azure.com/YOUR_ORGANIZATION/"
# pat = "YOUR_PAT_TOKEN" needed only if using PAT for authentication
```

## Azure DevOps Webhook

To trigger from an Azure webhook, you need to manually [add a webhook](https://learn.microsoft.com/en-us/azure/devops/service-hooks/services/webhooks?view=azure-devops).
Use the "Pull request created" type to trigger a review, or "Pull request commented on" to trigger any supported comment with /<command> <args> comment on the relevant PR. Note that for the "Pull request commented on" trigger, only API v2.0 is supported.

For webhook security, create a sporadic username/password pair and configure the webhook username and password on both the server and Azure DevOps webhook. These will be sent as basic Auth data by the webhook with each request:

```toml
[azure_devops_server]
webhook_username = "<basic auth user>"
webhook_password = "<basic auth password>"
```

> :warning: **Ensure that the webhook endpoint is only accessible over HTTPS** to mitigate the risk of credential interception when using basic authentication.
