# Fetching Ticket Context for PRs

`Supported Git Platforms: GitHub, GitLab, Bitbucket`

!!! note "Branch-name issue linking: GitHub only (for now)"
    Extracting issue links from the **branch name** (and the optional `branch_issue_regex` setting) is currently implemented for **GitHub only**. Support for GitLab, Bitbucket, and other platforms is planned for a later release. The GitHub flow was the most relevant to implement first; other providers will follow.

## Overview

PR-Agent streamlines code review workflows by seamlessly connecting with multiple ticket management systems.
This integration enriches the review process by automatically surfacing relevant ticket information and context alongside code changes.

**Ticket systems supported**:

- [GitHub/Gitlab Issues](#githubgitlab-issues-integration)
- [Jira](#jira-integration)

**Ticket data fetched:**

1. Ticket Title
2. Ticket Description
3. Custom Fields (Acceptance criteria)
4. Subtasks (linked tasks)
5. Labels
6. Attached Images/Screenshots

## Affected Tools

Ticket Recognition Requirements:

- The PR description should contain a link to the ticket or if the branch name starts with the ticket id / number.
- For Jira tickets, you should follow the instructions in [Jira Integration](#jira-integration) in order to authenticate with Jira.

### Describe tool

PR-Agent will recognize the ticket and use the ticket content (title, description, labels) to provide additional context for the code changes.
By understanding the reasoning and intent behind modifications, the LLM can offer more insightful and relevant code analysis.

### Review tool

Similarly to the `describe` tool, the `review` tool will use the ticket content to provide additional context for the code changes.

In addition, this feature will evaluate how well a Pull Request (PR) adheres to its original purpose/intent as defined by the associated ticket or issue mentioned in the PR description.
Each ticket will be assigned a label (Compliance/Alignment level), Indicates the degree to which the PR fulfills its original purpose:

- Fully Compliant
- Partially Compliant
- Not Compliant
- PR Code Verified

![Ticket Compliance](https://www.qodo.ai/images/pr_agent/ticket_compliance_review.png){width=768}

A `PR Code Verified` label indicates the PR code meets ticket requirements, but requires additional manual testing beyond the code scope. For example - validating UI display across different environments (Mac, Windows, mobile, etc.).


#### Configuration options

-

    By default, the `review` tool will automatically validate if the PR complies with the referenced ticket.
    If you want to disable this feedback, add the following line to your configuration file:

    ```toml
    [pr_reviewer]
    require_ticket_analysis_review=false
    ```

-

    If you set:
    ```toml
    [pr_reviewer]
    check_pr_additional_content=true
    ```
    (default: `false`)

    the `review` tool will also validate that the PR code doesn't contain any additional content that is not related to the ticket. If it does, the PR will be labeled at best as `PR Code Verified`, and the `review` tool will provide a comment with the additional unrelated content found in the PR code.

## GitHub/Gitlab Issues Integration

PR-Agent will automatically recognize GitHub/Gitlab issues mentioned in the PR description and fetch the issue content.
Examples of valid GitHub/Gitlab issue references:

- `https://github.com/<ORG_NAME>/<REPO_NAME>/issues/<ISSUE_NUMBER>` or `https://gitlab.com/<ORG_NAME>/<REPO_NAME>/-/issues/<ISSUE_NUMBER>`
- `#<ISSUE_NUMBER>`
- `<ORG_NAME>/<REPO_NAME>#<ISSUE_NUMBER>`

Branch names can also be used to link issues, for example:
- `123-fix-bug` (where `123` is the issue number)

This branch-name detection applies **only when the git provider is GitHub**. Support for other platforms is planned for later.

Since PR-Agent is integrated with GitHub, it doesn't require any additional configuration to fetch GitHub issues.

## Jira Integration

We support both Jira Cloud and Jira Server/Data Center.

### Jira Cloud

#### Email/Token Authentication

You can create an API token from your Atlassian account:

1. Log in to https://id.atlassian.com/manage-profile/security/api-tokens.

2. Click Create API token.

3. From the dialog that appears, enter a name for your new token and click Create.

4. Click Copy to clipboard.

![Jira Cloud API Token](https://images.ctfassets.net/zsv3d0ugroxu/1RYvh9lqgeZjjNe5S3Hbfb/155e846a1cb38f30bf17512b6dfd2229/screenshot_NewAPIToken){width=384}

5. In your [configuration file](../usage-guide/configuration_options.md) add the following lines:

```toml
[jira]
jira_api_token = "YOUR_API_TOKEN"
jira_api_email = "YOUR_EMAIL"
```

### Jira Data Center/Server

#### Using Basic Authentication for Jira Data Center/Server

You can use your Jira username and password to authenticate with Jira Data Center/Server.

In your Configuration file/Environment variables/Secrets file, add the following lines:

```toml
jira_api_email = "your_username"
jira_api_token = "your_password"
```

(Note that indeed the 'jira_api_email' field is used for the username, and the 'jira_api_token' field is used for the user password.)

##### Validating Basic authentication via Python script

If you are facing issues retrieving tickets in PR-Agent with Basic auth, you can validate the flow using a Python script.
This following steps will help you check if the basic auth is working correctly, and if you can access the Jira ticket details:

1. run `pip install jira==3.8.0`

2. run the following Python script (after replacing the placeholders with your actual values):

???- example "Script to validate basic auth"

    ```python
    from jira import JIRA
    
    
    if __name__ == "__main__":
        try:
            # Jira server URL
            server = "https://..."
            # Basic auth
            username = "..."
            password = "..."
            # Jira ticket code (e.g. "PROJ-123")
            ticket_id = "..."
    
            print("Initializing JiraServerTicketProvider with JIRA server")
            # Initialize JIRA client
            jira = JIRA(
                server=server,
                basic_auth=(username, password),
                timeout=30
            )
            if jira:
                print(f"JIRA client initialized successfully")
            else:
                print("Error initializing JIRA client")
    
            # Fetch ticket details
            ticket = jira.issue(ticket_id)
            print(f"Ticket title: {ticket.fields.summary}")
    
        except Exception as e:
            print(f"Error fetching JIRA ticket details: {e}")
    ```

#### Using a Personal Access Token (PAT) for Jira Data Center/Server

1. Create a [Personal Access Token (PAT)](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html) in your Jira account
2. In your Configuration file/Environment variables/Secrets file, add the following lines:

```toml
[jira]
jira_base_url = "YOUR_JIRA_BASE_URL" # e.g. https://jira.example.com
jira_api_token = "YOUR_API_TOKEN"
```

##### Validating PAT token via Python script

If you are facing issues retrieving tickets in PR-Agent with PAT token, you can validate the flow using a Python script.
This following steps will help you check if the token is working correctly, and if you can access the Jira ticket details:

1. run `pip install jira==3.8.0`

2. run the following Python script (after replacing the placeholders with your actual values):

??? example- "Script to validate PAT token"

    ```python
    from jira import JIRA
    
    
    if __name__ == "__main__":
        try:
            # Jira server URL
            server = "https://..."
            # Jira PAT token
            token_auth = "..."
            # Jira ticket code (e.g. "PROJ-123")
            ticket_id = "..."
    
            print("Initializing JiraServerTicketProvider with JIRA server")
            # Initialize JIRA client
            jira = JIRA(
                server=server,
                token_auth=token_auth,
                timeout=30
            )
            if jira:
                print(f"JIRA client initialized successfully")
            else:
                print("Error initializing JIRA client")
    
            # Fetch ticket details
            ticket = jira.issue(ticket_id)
            print(f"Ticket title: {ticket.fields.summary}")
    
        except Exception as e:
            print(f"Error fetching JIRA ticket details: {e}")
    ```


### Multi-JIRA Server Configuration

PR-Agent supports connecting to multiple JIRA servers using different authentication methods.

=== "Email/Token (Basic Auth)"

    Configure multiple servers using Email/Token authentication:

    - `jira_servers`: List of JIRA server URLs
    - `jira_api_token`: List of API tokens (for Cloud) or passwords (for Data Center)
    - `jira_api_email`: List of emails (for Cloud) or usernames (for Data Center)
    - `jira_base_url`: Default server for ticket IDs like `PROJ-123`, Each repository can configure (local config file) its own `jira_base_url` to choose which server to use by default.

    **Example Configuration:**
    ```toml
    [jira]
    # Server URLs
    jira_servers = ["https://company.atlassian.net", "https://datacenter.jira.com"]

    # API tokens/passwords
    jira_api_token = ["cloud_api_token_here", "datacenter_password"]

    # Emails/usernames (both required)
    jira_api_email = ["user@company.com", "datacenter_username"]

    # Default server for ticket IDs
    jira_base_url = "https://company.atlassian.net"
    ```

=== "PAT Auth"

    Configure multiple servers using Personal Access Token authentication:

    - `jira_servers`: List of JIRA server URLs
    - `jira_api_token`: List of PAT tokens
    - `jira_api_email`: Not needed (can be omitted or left empty)
    - `jira_base_url`: Default server for ticket IDs like `PROJ-123`, Each repository can configure (local config file) its own `jira_base_url` to choose which server to use by default.

    **Example Configuration:**
    ```toml
    [jira]
    # Server URLs
    jira_servers = ["https://server1.jira.com", "https://server2.jira.com"]

    # PAT tokens only
    jira_api_token = ["pat_token_1", "pat_token_2"]

    # Default server for ticket IDs
    jira_base_url = "https://server1.jira.com"
    ```

    **Mixed Authentication (Email/Token + PAT):**
    ```toml
    [jira]
    jira_servers = ["https://company.atlassian.net", "https://server.jira.com"]
    jira_api_token = ["cloud_api_token", "server_pat_token"]
    jira_api_email = ["user@company.com", ""]  # Empty for PAT
    ```




### How to link a PR to a Jira ticket

To integrate with Jira, you can link your PR to a ticket using either of these methods:

**Method 1: Description Reference:**

Include a ticket reference in your PR description, using either the complete URL format `https://<JIRA_ORG>.atlassian.net/browse/ISSUE-123` or the shortened ticket ID `ISSUE-123` (without prefix or suffix for the shortened ID).

**Method 2: Branch Name Detection:**

Name your branch with the ticket ID as a prefix (e.g., `ISSUE-123-feature-description` or `ISSUE-123/feature-description`).

!!! note "Jira Base URL"
    For shortened ticket IDs or branch detection (method 2 for JIRA cloud), you must configure the Jira base URL in your configuration file under the [jira] section:

    ```toml
    [jira]
    jira_base_url = "https://<JIRA_ORG>.atlassian.net"
    ```
    Where `<JIRA_ORG>` is your Jira organization identifier (e.g., `mycompany` for `https://mycompany.atlassian.net`).
