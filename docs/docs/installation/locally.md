To run PR-Agent locally, you first need to acquire two keys:

1. An OpenAI key from [here](https://platform.openai.com/api-keys){:target="_blank"}, with access to GPT-5.4 and gpt-5.4-mini (or a key for other [language models](../usage-guide/changing_a_model.md), if you prefer).
2. A personal access token from your Git platform (GitHub, GitLab, BitBucket, Gitea) with repo scope. GitHub token, for example, can be issued from [here](https://github.com/settings/tokens){:target="_blank"}

## Using Docker image

A list of the relevant tools can be found in the [tools guide](../tools/).

To invoke a tool (for example `review`), you can run PR-Agent directly from the Docker image. Here's how:

- For GitHub:

    ```bash
    docker run --rm -it -e OPENAI.KEY=<your_openai_key> -e GITHUB.USER_TOKEN=<your_github_token> pragent/pr-agent:latest --pr_url <pr_url> review
    ```

    If you are using GitHub enterprise server, you need to specify the custom url as variable.
    For example, if your GitHub server is at `https://github.mycompany.com`, add the following to the command:

    ```bash
    -e GITHUB.BASE_URL=https://github.mycompany.com/api/v3
    ```

- For GitLab:

    ```bash
    docker run --rm -it -e OPENAI.KEY=<your key> -e CONFIG.GIT_PROVIDER=gitlab -e GITLAB.PERSONAL_ACCESS_TOKEN=<your token> pragent/pr-agent:latest --pr_url <pr_url> review
    ```

    If you have a dedicated GitLab instance, you need to specify the custom url as variable:

    ```bash
    -e GITLAB.URL=<your gitlab instance url>
    ```

- For BitBucket:

    ```bash
    docker run --rm -it -e CONFIG.GIT_PROVIDER=bitbucket -e OPENAI.KEY=$OPENAI_API_KEY -e BITBUCKET.BEARER_TOKEN=$BITBUCKET_BEARER_TOKEN pragent/pr-agent:latest --pr_url=<pr_url> review
    ```

- For Gitea:

    ```bash
    docker run --rm -it -e OPENAI.KEY=<your key> -e CONFIG.GIT_PROVIDER=gitea -e GITEA.PERSONAL_ACCESS_TOKEN=<your token> pragent/pr-agent:latest --pr_url <pr_url> review
    ```

    If you have a dedicated Gitea instance, you need to specify the custom url as variable:

    ```bash
    -e GITEA.URL=<your gitea instance url>
    ```


For other git providers, update `CONFIG.GIT_PROVIDER` accordingly and check the [`pr_agent/settings/.secrets_template.toml`](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/.secrets_template.toml) file for environment variables expected names and values.

### Utilizing environment variables

It is also possible to provide or override the configuration by setting the corresponding environment variables.
You can define the corresponding environment variables by following this convention: `<TABLE>__<KEY>=<VALUE>` or `<TABLE>.<KEY>=<VALUE>`.
The `<TABLE>` refers to a table/section in a configuration file and `<KEY>=<VALUE>` refers to the key/value pair of a setting in the configuration file.

For example, suppose you want to run `pr_agent` that connects to a self-hosted GitLab instance similar to an example above.
You can define the environment variables in a plain text file named `.env` with the following content:

```bash
CONFIG__GIT_PROVIDER="gitlab"
GITLAB__URL="<your url>"
GITLAB__PERSONAL_ACCESS_TOKEN="<your token>"
OPENAI__KEY="<your key>"
```

Then, you can run `pr_agent` using Docker with the following command:

```shell
docker run --rm -it --env-file .env pragent/pr-agent:latest <tool> <tool parameter>
```

---

### I get an error when running the Docker image. What should I do?

If you encounter an error when running the Docker image, it is almost always due to a misconfiguration of api keys or tokens.

Note that litellm, which is used by pr-agent, sometimes returns non-informative error messages such as `APIError: OpenAIException - Connection error.`
Carefully check the api keys and tokens you provided and make sure they are correct.
Adjustments may be needed depending on your llm provider.

For example, for Azure OpenAI, additional keys are [needed](../usage-guide/changing_a_model.md#azure).
Same goes for other providers, make sure to check the [documentation](../usage-guide/changing_a_model.md#changing-a-model)

## Using pip package

Install the package:

```bash
pip install pr-agent
```

Then run the relevant tool with the script below.
<br>
Make sure to fill in the required parameters (`user_token`, `openai_key`, `pr_url`, `command`):

```python
from pr_agent import cli
from pr_agent.config_loader import get_settings

def main():
    # Fill in the following values
    provider = "github" # github/gitlab/bitbucket/azure_devops
    user_token = "..."  #  user token
    openai_key = "..."  # OpenAI key
    pr_url = "..."      # PR URL, for example 'https://github.com/the-pr-agent/pr-agent/pull/809'
    command = "/review" # Command to run (e.g. '/review', '/describe', '/ask="What is the purpose of this PR?"', ...)

    # Setting the configurations
    get_settings().set("CONFIG.git_provider", provider)
    get_settings().set("openai.key", openai_key)
    get_settings().set("github.user_token", user_token)

    # Run the command. Feedback will appear in GitHub PR comments
    cli.run_command(pr_url, command)


if __name__ == '__main__':
    main()
```

## Run from source

1. Clone this repository:

```bash
git clone https://github.com/the-pr-agent/pr-agent.git
```

2. Navigate to the `/pr-agent` folder and install the requirements in your favorite virtual environment:

```bash
pip install -e .
```

*Note: If you get an error related to Rust in the dependency installation then make sure Rust is installed and in your `PATH`, instructions: https://rustup.rs*

3. Copy the secrets template file and fill in your OpenAI key and your GitHub user token:

```bash
cp pr_agent/settings/.secrets_template.toml pr_agent/settings/.secrets.toml
chmod 600 pr_agent/settings/.secrets.toml
# Edit .secrets.toml file
```

4. Run the cli.py script:

```bash
python3 -m pr_agent.cli --pr_url <pr_url> review
python3 -m pr_agent.cli --pr_url <pr_url> ask <your question>
python3 -m pr_agent.cli --pr_url <pr_url> describe
python3 -m pr_agent.cli --pr_url <pr_url> improve
python3 -m pr_agent.cli --pr_url <pr_url> add_docs
python3 -m pr_agent.cli --pr_url <pr_url> generate_labels
python3 -m pr_agent.cli --issue_url <issue_url> similar_issue
...
```

[Optional] Add the pr_agent folder to your PYTHONPATH

```bash
export PYTHONPATH=$PYTHONPATH:<PATH to pr_agent folder>
```
