## Run as a GitLab Pipeline

You can use a pre-built Action Docker image to run PR-Agent as a GitLab pipeline. This is a simple way to get started with PR-Agent without setting up your own server.

(1) Add the following file to your repository under `.gitlab-ci.yml`:

```yaml
stages:
  - pr_agent

pr_agent_job:
  stage: pr_agent
  image:
    name: pragent/pr-agent:latest
    entrypoint: [""]
  script:
    - cd /app
    - echo "Running PR Agent action step"
    - export MR_URL="$CI_MERGE_REQUEST_PROJECT_URL/merge_requests/$CI_MERGE_REQUEST_IID"
    - echo "MR_URL=$MR_URL"
    - export gitlab__url=$CI_SERVER_PROTOCOL://$CI_SERVER_FQDN
    - export gitlab__PERSONAL_ACCESS_TOKEN=$GITLAB_PERSONAL_ACCESS_TOKEN
    - export config__git_provider="gitlab"
    - export openai__key=$OPENAI_KEY
    - python -m pr_agent.cli --pr_url="$MR_URL" describe
    - python -m pr_agent.cli --pr_url="$MR_URL" review
    - python -m pr_agent.cli --pr_url="$MR_URL" improve
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
```

This script will run PR-Agent on every new merge request. You can modify the `rules` section to run PR-Agent on different events.
You can also modify the `script` section to run different PR-Agent commands, or with different parameters by exporting different environment variables.

(2) Add the following masked variables to your GitLab repository (CI/CD -> Variables):

- `GITLAB_PERSONAL_ACCESS_TOKEN`: Your GitLab personal access token.

- `OPENAI_KEY`: Your OpenAI key.

Note that if your base branches are not protected, don't set the variables as `protected`, since the pipeline will not have access to them.

> **Note**: The `$CI_SERVER_FQDN` variable is available starting from GitLab version 16.10. If you're using an earlier version, this variable will not be available. However, you can combine `$CI_SERVER_HOST` and `$CI_SERVER_PORT` to achieve the same result. Please ensure you're using a compatible version or adjust your configuration.

> **Note**: The `gitlab__SSL_VERIFY` environment variable can be used to specify the path to a custom CA certificate bundle for SSL verification. GitLab exposes the `$CI_SERVER_TLS_CA_FILE` variable, which points to the custom CA certificate file configured in your GitLab instance.
> Alternatively, SSL verification can be disabled entirely by setting `gitlab__SSL_VERIFY=false`, although this is not recommended.

## Run a GitLab webhook server

1. In GitLab create a new user and give it "Reporter" role for the intended group or project.

2. For the user from step 1, generate a `personal_access_token` with `api` access.

3. Generate a random secret for your app, and save it for later (`shared_secret`). For example, you can use:

```bash
SHARED_SECRET=$(python -c "import secrets; print(secrets.token_hex(10))")
```

4. Clone this repository:

```bash
git clone https://github.com/the-pr-agent/pr-agent.git
```

5. Prepare variables and secrets. Skip this step if you plan on setting these as environment variables when running the agent:
    1. In the configuration file/variables:
        - Set `config.git_provider` to "gitlab"

    2. In the secrets file/variables:
        - Set your AI model key in the respective section
        - In the [gitlab] section, set `personal_access_token` (with token from step 2) and `shared_secret` (with secret from step 3)
        - **Authentication type**: Set `auth_type` to `"private_token"` for older GitLab versions (e.g., 11.x) or private deployments. Default is `"oauth_token"` for gitlab.com and newer versions.

6. Build a Docker image for the app and optionally push it to a Docker repository. We'll use Dockerhub as an example:

```bash
docker build . -t gitlab_pr_agent --target gitlab_webhook -f docker/Dockerfile
docker push pragent/pr-agent:gitlab_webhook  # Push to your Docker repository
```

7. Set the environmental variables, the method depends on your docker runtime. Skip this step if you included your secrets/configuration directly in the Docker image.

```bash
CONFIG__GIT_PROVIDER=gitlab
GITLAB__PERSONAL_ACCESS_TOKEN=<personal_access_token>
GITLAB__SHARED_SECRET=<shared_secret>
GITLAB__URL=https://gitlab.com
GITLAB__AUTH_TYPE=oauth_token  # Use "private_token" for older GitLab versions
OPENAI__KEY=<your_openai_api_key>
PORT=3000  # Optional: override the webhook server port
```

8. Create a webhook in your GitLab project. Set the URL to `http[s]://<PR_AGENT_HOSTNAME>/webhook`, the secret token to the generated secret from step 3, and enable the triggers `push`, `comments` and `merge request events`.

9. Test your installation by opening a merge request or commenting on a merge request using one of PR Agent's commands.

## Deploy as a Lambda Function

Note that since AWS Lambda env vars cannot have "." in the name, you can replace each "." in an env variable with "__".<br>
For example: `GITLAB.PERSONAL_ACCESS_TOKEN` --> `GITLAB__PERSONAL_ACCESS_TOKEN`

1. Follow steps 1-5 from [Run a GitLab webhook server](#run-a-gitlab-webhook-server).
2. Build a docker image that can be used as a lambda function

    ```shell
    docker buildx build --platform=linux/amd64 . -t pragent/pr-agent:gitlab_lambda --target gitlab_lambda -f docker/Dockerfile.lambda
   ```

3. Push image to ECR

    ```shell
    docker tag pragent/pr-agent:gitlab_lambda <AWS_ACCOUNT>.dkr.ecr.<AWS_REGION>.amazonaws.com/pragent/pr-agent:gitlab_lambda
    docker push <AWS_ACCOUNT>.dkr.ecr.<AWS_REGION>.amazonaws.com/pragent/pr-agent:gitlab_lambda
    ```

4. Create a lambda function that uses the uploaded image. Set the lambda timeout to be at least 3m.
5. Configure the lambda function to have a Function URL.
6. In the environment variables of the Lambda function, specify `AZURE_DEVOPS_CACHE_DIR` to a writable location such as /tmp. (see [link](https://github.com/the-pr-agent/pr-agent/pull/450#issuecomment-1840242269))
7. Go back to steps 8-9 of [Run a GitLab webhook server](#run-a-gitlab-webhook-server) with the function URL as your Webhook URL.
    The Webhook URL would look like `https://<LAMBDA_FUNCTION_URL>/webhook`

### Using AWS Secrets Manager

For production Lambda deployments, use AWS Secrets Manager instead of environment variables:

1. Create individual secrets for each GitLab webhook with this JSON format (e.g., secret name: `project-webhook-secret-001`)

```json
{
  "gitlab_token": "glpat-xxxxxxxxxxxxxxxxxxxxxxxx",
  "token_name": "project-webhook-001"
}
```

2. Create a main configuration secret for common settings (e.g., secret name: `pr-agent-main-config`)

```json
{
  "openai.key": "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

3. Set these environment variables in your Lambda:

```bash
CONFIG__SECRET_PROVIDER=aws_secrets_manager
AWS_SECRETS_MANAGER__SECRET_ARN=arn:aws:secretsmanager:us-east-1:123456789012:secret:pr-agent-main-config-AbCdEf
```

4. In your GitLab webhook configuration, set the **Secret Token** to the **Secret name** created in step 1:
   - Example: `project-webhook-secret-001`

**Important**: When using Secrets Manager, GitLab's webhook secret must be the Secrets Manager secret name.

5. Add IAM permission `secretsmanager:GetSecretValue` to your Lambda execution role
