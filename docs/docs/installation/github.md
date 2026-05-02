In this page we will cover how to install and run PR-Agent as a GitHub Action or GitHub App, and how to configure it for your needs.

## Run as a GitHub Action

You can use our pre-built Github Action Docker image to run PR-Agent as a Github Action.

1) Add the following file to your repository under `.github/workflows/pr_agent.yml`:

```yaml
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      issues: write
      pull-requests: write
      contents: write
    name: Run pr agent on every pull request, respond to user comments
    steps:
      - name: PR Agent action step
        id: pragent
        uses: the-pr-agent/pr-agent@main
        env:
          OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

2) Add the following secret to your repository under `Settings > Secrets and variables > Actions > New repository secret > Add secret`:

```
Name = OPENAI_KEY
Secret = <your key>
```

The GITHUB_TOKEN secret is automatically created by GitHub.

3) Merge this change to your main branch.
When you open your next PR, you should see a comment from `github-actions` bot with a review of your PR, and instructions on how to use the rest of the tools.

4) You may configure PR-Agent by adding environment variables under the env section corresponding to any configurable property in the [configuration](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) file. Some examples:

```yaml
      env:
        # ... previous environment values
        OPENAI.ORG: "<Your organization name under your OpenAI account>"
        PR_REVIEWER.REQUIRE_TESTS_REVIEW: "false" # Disable tests review
        PR_CODE_SUGGESTIONS.NUM_CODE_SUGGESTIONS: 6 # Increase number of code suggestions
```

See detailed usage instructions in the [USAGE GUIDE](../usage-guide/automations_and_usage.md#github-action)

### Configuration Examples

This section provides detailed, step-by-step examples for configuring PR-Agent with different models and advanced options in GitHub Actions.

#### Quick Start Examples

##### Basic Setup (OpenAI Default)

Copy this minimal workflow to get started with the default OpenAI models:

```yaml
name: PR Agent
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      issues: write
      pull-requests: write
      contents: write
    steps:
      - name: PR Agent action step
        uses: the-pr-agent/pr-agent@main
        env:
          OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

##### Gemini Setup

Ready-to-use workflow for Gemini models:

```yaml
name: PR Agent (Gemini)
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      issues: write
      pull-requests: write
      contents: write
    steps:
      - name: PR Agent action step
        uses: the-pr-agent/pr-agent@main
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          config.model: "gemini/gemini-1.5-flash"
          config.fallback_models: '["gemini/gemini-1.5-flash"]'
          GOOGLE_AI_STUDIO.GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          github_action_config.auto_review: "true"
          github_action_config.auto_describe: "true"
          github_action_config.auto_improve: "true"
```

#### Claude Setup

Ready-to-use workflow for Claude models:

```yaml
name: PR Agent (Claude)
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      issues: write
      pull-requests: write
      contents: write
    steps:
      - name: PR Agent action step
        uses: the-pr-agent/pr-agent@main
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          config.model: "anthropic/claude-3-opus-20240229"
          config.fallback_models: '["anthropic/claude-3-haiku-20240307"]'
          ANTHROPIC.KEY: ${{ secrets.ANTHROPIC_KEY }}
          github_action_config.auto_review: "true"
          github_action_config.auto_describe: "true"
          github_action_config.auto_improve: "true"
```

#### Basic Configuration with Tool Controls

Start with this enhanced workflow that includes tool configuration:

```yaml
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      issues: write
      pull-requests: write
      contents: write
    name: Run pr agent on every pull request, respond to user comments
    steps:
      - name: PR Agent action step
        id: pragent
        uses: the-pr-agent/pr-agent@main
        env:
          OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          # Enable/disable automatic tools
          github_action_config.auto_review: "true"
          github_action_config.auto_describe: "true"
          github_action_config.auto_improve: "true"
          # Configure which PR events trigger the action
          github_action_config.pr_actions: '["opened", "reopened", "ready_for_review", "review_requested"]'
```

#### Switching Models

##### Using Gemini (Google AI Studio)

To use Gemini models instead of the default OpenAI models:

```yaml
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Set the model to Gemini
        config.model: "gemini/gemini-1.5-flash"
        config.fallback_models: '["gemini/gemini-1.5-flash"]'
        # Add your Gemini API key
        GOOGLE_AI_STUDIO.GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        # Tool configuration
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

**Required Secrets:**

- Add `GEMINI_API_KEY` to your repository secrets (get it from [Google AI Studio](https://aistudio.google.com/))

**Note:** When using non-OpenAI models like Gemini, you don't need to set `OPENAI_KEY` - only the model-specific API key is required.

##### Using Claude (Anthropic)

To use Claude models:

```yaml
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Set the model to Claude
        config.model: "anthropic/claude-3-opus-20240229"
        config.fallback_models: '["anthropic/claude-3-haiku-20240307"]'
        # Add your Anthropic API key
        ANTHROPIC.KEY: ${{ secrets.ANTHROPIC_KEY }}
        # Tool configuration
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

**Required Secrets:**

- Add `ANTHROPIC_KEY` to your repository secrets (get it from [Anthropic Console](https://console.anthropic.com/))

**Note:** When using non-OpenAI models like Claude, you don't need to set `OPENAI_KEY` - only the model-specific API key is required.

##### Using Azure OpenAI

To use Azure OpenAI services:

```yaml
      env:
        OPENAI_KEY: ${{ secrets.AZURE_OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Azure OpenAI configuration
        OPENAI.API_TYPE: "azure"
        OPENAI.API_VERSION: "2023-05-15"
        OPENAI.API_BASE: ${{ secrets.AZURE_OPENAI_ENDPOINT }}
        OPENAI.DEPLOYMENT_ID: ${{ secrets.AZURE_OPENAI_DEPLOYMENT }}
        # Set the model to match your Azure deployment
        config.model: "gpt-4o"
        config.fallback_models: '["gpt-4o"]'
        # Tool configuration
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

**Required Secrets:**

- `AZURE_OPENAI_KEY`: Your Azure OpenAI API key
- `AZURE_OPENAI_ENDPOINT`: Your Azure OpenAI endpoint URL
- `AZURE_OPENAI_DEPLOYMENT`: Your deployment name

##### Using Local Models (Ollama)

To use local models via Ollama:

```yaml
      env:
        OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Set the model to a local Ollama model
        config.model: "ollama/qwen2.5-coder:32b"
        config.fallback_models: '["ollama/qwen2.5-coder:32b"]'
        config.custom_model_max_tokens: "128000"
        # Ollama configuration
        OLLAMA.API_BASE: "http://localhost:11434"
        # Tool configuration
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

**Note:** For local models, you'll need to use a self-hosted runner with Ollama installed, as GitHub Actions hosted runners cannot access localhost services.

#### Advanced Configuration Options

##### Custom Review Instructions

Add specific instructions for the review process:

```yaml
      env:
        OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Custom review instructions
        pr_reviewer.extra_instructions: "Focus on security vulnerabilities and performance issues. Check for proper error handling."
        # Tool configuration
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

##### Language-Specific Configuration

Configure for specific programming languages:

```yaml
      env:
        OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Language-specific settings
        pr_reviewer.extra_instructions: "Focus on Python best practices, type hints, and docstrings."
        pr_code_suggestions.num_code_suggestions: "8"
        pr_code_suggestions.suggestions_score_threshold: "7"
        # Tool configuration
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

##### Selective Tool Execution

Run only specific tools automatically:

```yaml
      env:
        OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Only run review and describe, skip improve
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "false"
        # Only trigger on PR open and reopen
        github_action_config.pr_actions: '["opened", "reopened"]'
```

#### Using Configuration Files

Instead of setting all options via environment variables, you can use a `.pr_agent.toml` file in your repository root:

1. Create a `.pr_agent.toml` file in your repository root:

```toml
[config]
model = "gemini/gemini-1.5-flash"
fallback_models = ["anthropic/claude-3-opus-20240229"]

[pr_reviewer]
extra_instructions = "Focus on security issues and code quality."

[pr_code_suggestions]
num_code_suggestions = 6
suggestions_score_threshold = 7
```

2. Use a simpler workflow file:

```yaml
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
  issue_comment:
jobs:
  pr_agent_job:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      issues: write
      pull-requests: write
      contents: write
    name: Run pr agent on every pull request, respond to user comments
    steps:
      - name: PR Agent action step
        id: pragent
        uses: the-pr-agent/pr-agent@main
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GOOGLE_AI_STUDIO.GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          ANTHROPIC.KEY: ${{ secrets.ANTHROPIC_KEY }}
          github_action_config.auto_review: "true"
          github_action_config.auto_describe: "true"
          github_action_config.auto_improve: "true"
```

#### Troubleshooting Common Issues

##### Model Not Found Errors

If you get model not found errors:

1. **Check model name format**: Ensure you're using the correct model identifier format (e.g., `gemini/gemini-1.5-flash`, not just `gemini-1.5-flash`)

2. **Verify API keys**: Make sure your API keys are correctly set as repository secrets

3. **Check model availability**: Some models may not be available in all regions or may require specific access

##### Environment Variable Format

Remember these key points about environment variables:

- Use dots (`.`) or double underscores (`__`) to separate sections and keys
- Boolean values should be strings: `"true"` or `"false"`
- Arrays should be JSON strings: `'["item1", "item2"]'`
- Model names are case-sensitive

##### Rate Limiting

If you encounter rate limiting:

```yaml
      env:
        OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Add a fallback model for better reliability
        config.fallback_models: '["gpt-5.4-mini"]'
        # Increase timeout for slower models
        config.ai_timeout: "300"
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

##### Common Error Messages and Solutions

**Error: "Model not found"**
- **Solution**: Check the model name format and ensure it matches the exact identifier. See the [Changing a model in PR-Agent](../usage-guide/changing_a_model.md) guide for supported models and their correct identifiers.

**Error: "API key not found"**
- **Solution**: Verify that your API key is correctly set as a repository secret and the environment variable name matches exactly
- **Note**: For non-OpenAI models (Gemini, Claude, etc.), you only need the model-specific API key, not `OPENAI_KEY`

**Error: "Rate limit exceeded"**
- **Solution**: Add fallback models or increase the `config.ai_timeout` value

**Error: "Permission denied"**
- **Solution**: Ensure your workflow has the correct permissions set:
  ```yaml
  permissions:
    issues: write
    pull-requests: write
    contents: write
  ```

**Error: "Invalid JSON format"**

- **Solution**: Check that arrays are properly formatted as JSON strings:

```yaml

Correct:
config.fallback_models: '["model1", "model2"]'
Incorrect (interpreted as a YAML list, not a string):
config.fallback_models: ["model1", "model2"]
```

##### Debugging Tips

1. **Enable verbose logging**: Add `config.verbosity_level: "2"` to see detailed logs
2. **Check GitHub Actions logs**: Look at the step output for specific error messages
3. **Test with minimal configuration**: Start with just the basic setup and add options one by one
4. **Verify secrets**: Double-check that all required secrets are set in your repository settings

##### Performance Optimization

For better performance with large repositories:

```yaml
      env:
        OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        # Optimize for large PRs
        config.large_patch_policy: "clip"
        config.max_model_tokens: "32000"
        config.patch_extra_lines_before: "3"
        config.patch_extra_lines_after: "1"
        github_action_config.auto_review: "true"
        github_action_config.auto_describe: "true"
        github_action_config.auto_improve: "true"
```

#### Reference

For more detailed configuration options, see:

- [Changing a model in PR-Agent](../usage-guide/changing_a_model.md)
- [Configuration options](../usage-guide/configuration_options.md)
- [Automations and usage](../usage-guide/automations_and_usage.md#github-action)

### Using a specific release

!!! tip ""
    if you want to pin your action to a specific release (v0.34.2 for example) for stability reasons, use:
    ```yaml
    ...
        steps:
          - name: PR Agent action step
            id: pragent
            uses: docker://pragent/pr-agent:0.34.2-github_action
    ...
    ```

    For enhanced security, you can also specify the Docker image by its [digest](https://hub.docker.com/repository/docker/pragent/pr-agent/tags):
    ```yaml
    ...
        steps:
          - name: PR Agent action step
            id: pragent
            uses: docker://pragent/pr-agent@sha256:a0b36966ca3a197ca739fa1e65c16703076fc1c744cd423ca203b8c21707d71c
    ...
    ```

### Action for GitHub enterprise server

!!! tip ""
    To use the action with a GitHub enterprise server, add an environment variable `GITHUB.BASE_URL` with the API URL of your GitHub server.

    For example, if your GitHub server is at `https://github.mycompany.com`, add the following to your workflow file:
    ```yaml
          env:
            # ... previous environment values
            GITHUB.BASE_URL: "https://github.mycompany.com/api/v3"
    ```

---

## Run as a GitHub App

Allowing you to automate the review process on your private or public repositories.

1) Create a GitHub App from the [Github Developer Portal](https://docs.github.com/en/developers/apps/creating-a-github-app).

   - Set the following permissions:
     - Pull requests: Read & write
     - Issue comment: Read & write
     - Metadata: Read-only
     - Contents: Read-only
   - Set the following events:
     - Issue comment
     - Pull request
     - Push (if you need to enable triggering on PR update)

2) Generate a random secret for your app, and save it for later. For example, you can use:

```bash
WEBHOOK_SECRET=$(python -c "import secrets; print(secrets.token_hex(10))")
```

3) Acquire the following pieces of information from your app's settings page:

   - App private key (click "Generate a private key" and save the file)
   - App ID

4) Clone this repository:

```bash
git clone https://github.com/the-pr-agent/pr-agent.git
```

5) Copy the secrets template file and fill in the following:

```bash
cp pr_agent/settings/.secrets_template.toml pr_agent/settings/.secrets.toml
# Edit .secrets.toml file
```

- Your OpenAI key.
- Copy your app's private key to the private_key field.
- Copy your app's ID to the app_id field.
- Copy your app's webhook secret to the webhook_secret field.
- Set deployment_type to 'app' in [configuration.toml](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml)

    > The .secrets.toml file is not copied to the Docker image by default, and is only used for local development.
    > If you want to use the .secrets.toml file in your Docker image, you can add remove it from the .dockerignore file.
    > In most production environments, you would inject the secrets file as environment variables or as mounted volumes.
    > For example, in order to inject a secrets file as a volume in a Kubernetes environment you can update your pod spec to include the following,
    > assuming you have a secret named `pr-agent-settings` with a key named `.secrets.toml`:

    ```
           volumes:
            - name: settings-volume
              secret:
                secretName: pr-agent-settings
    // ...
           containers:
    // ...
              volumeMounts:
                - mountPath: /app/pr_agent/settings_prod
                  name: settings-volume
    ```

    > Another option is to set the secrets as environment variables in your deployment environment, for example `OPENAI.KEY` and `GITHUB.USER_TOKEN`.

6) Build a Docker image for the app and optionally push it to a Docker repository. We'll use Dockerhub as an example:

    ```bash
    docker build . -t pragent/pr-agent:github_app --target github_app -f docker/Dockerfile
    docker push pragent/pr-agent:github_app  # Push to your Docker repository
    ```

7. Host the app using a server, serverless function, or container environment. Alternatively, for development and
   debugging, you may use tools like smee.io to forward webhooks to your local machine.
    You can check [Deploy as a Lambda Function](#deploy-as-a-lambda-function)

8. Go back to your app's settings, and set the following:

   - Webhook URL: The URL of your app's server or the URL of the smee.io channel.
   - Webhook secret: The secret you generated earlier.

9. Install the app by navigating to the "Install App" tab and selecting your desired repositories.

> **Note:** When running PR-Agent from GitHub app, the default configuration file (configuration.toml) will be loaded.
> However, you can override the default tool parameters by uploading a local configuration file `.pr_agent.toml`
> For more information please check out the [USAGE GUIDE](../usage-guide/automations_and_usage.md#github-app)
---

## Additional deployment methods

### Deploy as a Lambda Function

Note that since AWS Lambda env vars cannot have "." in the name, you can replace each "." in an env variable with "__".<br>
For example: `GITHUB.WEBHOOK_SECRET` --> `GITHUB__WEBHOOK_SECRET`

1. Follow steps 1-5 from [here](#run-as-a-github-app).
2. Build a docker image that can be used as a lambda function

    ```shell
    docker buildx build --platform=linux/amd64 . -t pragent/pr-agent:github_lambda --target github_lambda -f docker/Dockerfile.lambda
   ```
   (Note: --target github_lambda is optional as it's the default target)


3. Push image to ECR

    ```shell
    docker tag pragent/pr-agent:github_lambda <AWS_ACCOUNT>.dkr.ecr.<AWS_REGION>.amazonaws.com/pragent/pr-agent:github_lambda
    docker push <AWS_ACCOUNT>.dkr.ecr.<AWS_REGION>.amazonaws.com/pragent/pr-agent:github_lambda
    ```

4. Create a lambda function that uses the uploaded image. Set the lambda timeout to be at least 3m.
5. Configure the lambda function to have a Function URL.
6. In the environment variables of the Lambda function, specify `AZURE_DEVOPS_CACHE_DIR` to a writable location such as /tmp. (see [link](https://github.com/the-pr-agent/pr-agent/pull/450#issuecomment-1840242269))
7. Go back to steps 8-9 of [Method 5](#run-as-a-github-app) with the function url as your Webhook URL.
    The Webhook URL would look like `https://<LAMBDA_FUNCTION_URL>/api/v1/github_webhooks`

#### Using AWS Secrets Manager

For production Lambda deployments, use AWS Secrets Manager instead of environment variables:

1. Create a secret in AWS Secrets Manager with JSON format like this:

```json
{
  "openai.key": "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "github.webhook_secret": "your-webhook-secret-from-step-2",
  "github.private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
}
```

2. Add IAM permission `secretsmanager:GetSecretValue` to your Lambda execution role
3. Set these environment variables in your Lambda:

```bash
AWS_SECRETS_MANAGER__SECRET_ARN=arn:aws:secretsmanager:us-east-1:123456789012:secret:pr-agent-secrets-AbCdEf
CONFIG__SECRET_PROVIDER=aws_secrets_manager
```

---

### AWS CodeCommit Setup

Not all features have been added to CodeCommit yet.  As of right now, CodeCommit has been implemented to run the PR-Agent CLI on the command line, using AWS credentials stored in environment variables.  (More features will be added in the future.)  The following is a set of instructions to have PR-Agent do a review of your CodeCommit pull request from the command line:

1. Create an IAM user that you will use to read CodeCommit pull requests and post comments
    - Note: That user should have CLI access only, not Console access
2. Add IAM permissions to that user, to allow access to CodeCommit (see IAM Role example below)
3. Generate an Access Key for your IAM user
4. Set the Access Key and Secret using environment variables (see Access Key example below)
5. Set the `git_provider` value to `codecommit` in the `pr_agent/settings/configuration.toml` settings file
6. Set the `PYTHONPATH` to include your `pr-agent` project directory
    - Option A: Add `PYTHONPATH="/PATH/TO/PROJECTS/pr-agent` to your `.env` file
    - Option B: Set `PYTHONPATH` and run the CLI in one command, for example:
        - `PYTHONPATH="/PATH/TO/PROJECTS/pr-agent python pr_agent/cli.py [--ARGS]`

---

##### AWS CodeCommit IAM Role Example

Example IAM permissions to that user to allow access to CodeCommit:

- Note: The following is a working example of IAM permissions that has read access to the repositories and write access to allow posting comments
- Note: If you only want pr-agent to review your pull requests, you can tighten the IAM permissions further, however this IAM example will work, and allow the pr-agent to post comments to the PR
- Note: You may want to replace the `"Resource": "*"` with your list of repos, to limit access to only those repos

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "codecommit:BatchDescribe*",
                "codecommit:BatchGet*",
                "codecommit:Describe*",
                "codecommit:EvaluatePullRequestApprovalRules",
                "codecommit:Get*",
                "codecommit:List*",
                "codecommit:PostComment*",
                "codecommit:PutCommentReaction",
                "codecommit:UpdatePullRequestDescription",
                "codecommit:UpdatePullRequestTitle"
            ],
            "Resource": "*"
        }
    ]
}
```

##### AWS CodeCommit Access Key and Secret

Example setting the Access Key and Secret using environment variables

```sh
export AWS_ACCESS_KEY_ID="XXXXXXXXXXXXXXXX"
export AWS_SECRET_ACCESS_KEY="XXXXXXXXXXXXXXXX"
export AWS_DEFAULT_REGION="us-east-1"
```

##### AWS CodeCommit CLI Example

After you set up AWS CodeCommit using the instructions above, here is an example CLI run that tells pr-agent to **review** a given pull request.
(Replace your specific PYTHONPATH and PR URL in the example)

```sh
PYTHONPATH="/PATH/TO/PROJECTS/pr-agent" python pr_agent/cli.py \
  --pr_url https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/MY_REPO_NAME/pull-requests/321 \
  review
```