## Changing a model in PR-Agent

See [here](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/algo/__init__.py) for a list of supported models in PR-Agent.
The default model of PR-Agent is `GPT-5` from OpenAI.
To use a different model than the default, you need to edit in the [configuration file](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml#L7) the fields:

```toml
[config]
model = "..."
fallback_models = ["..."]
```

For models and environments not from OpenAI, you might need to provide additional keys and other parameters.
You can give parameters via a configuration file, or from environment variables.

!!! note "Model-specific environment variables"
    See [litellm documentation](https://litellm.vercel.app/docs/proxy/quick_start#supported-llms) for the environment variables needed per model, as they may vary and change over time. Our documentation per-model may not always be up-to-date with the latest changes.
    Failing to set the needed keys of a specific model will usually result in litellm not identifying the model type, and failing to utilize it.

### OpenAI like API
To use an OpenAI like API, set the following in your `.secrets.toml` file:

```toml
[openai]
api_base = "https://api.openai.com/v1"
api_key = "sk-..."
```

or use the environment variables (make sure to use double underscores `__`):

```bash
OPENAI__API_BASE=https://api.openai.com/v1
OPENAI__KEY=sk-...
```

### OpenAI Flex Processing

To reduce costs for non-urgent/background tasks, enable Flex Processing:

```toml
[litellm]
extra_body='{"processing_mode": "flex"}'
```

See [OpenAI Flex Processing docs](https://platform.openai.com/docs/guides/flex-processing) for details.

### Azure

To use Azure, set in your `.secrets.toml` (working from CLI), or in the GitHub `Settings > Secrets and variables` (working from GitHub App or GitHub Action):

```toml
[openai]
key = "" # your azure api key
api_type = "azure"
api_version = '2023-05-15'  # Check Azure documentation for the current API version
api_base = ""  # The base URL for your Azure OpenAI resource. e.g. "https://<your resource name>.openai.azure.com"
deployment_id = ""  # The deployment name you chose when you deployed the engine
```

and set in your configuration file:

```toml
[config]
model="" # the OpenAI model you've deployed on Azure (e.g. gpt-4o)
fallback_models=["..."]
```

To use Azure AD (Entra id) based authentication set in your `.secrets.toml` (working from CLI), or in the GitHub `Settings > Secrets and variables` (working from GitHub App or GitHub Action):

```toml
[azure_ad]
client_id = ""  # Your Azure AD application client ID
client_secret = ""  # Your Azure AD application client secret
tenant_id = ""  # Your Azure AD tenant ID
api_base = ""  # Your Azure OpenAI service base URL (e.g., https://openai.xyz.com/)
```

Passing custom headers to the underlying LLM Model API can be done by setting extra_headers parameter to litellm.

```toml
[litellm]
extra_headers='{"projectId": "<authorized projectId >", ...}') #The value of this setting should be a JSON string representing the desired headers, a ValueError is thrown otherwise.
```

This enables users to pass authorization tokens or API keys, when routing requests through an API management gateway.

### Ollama

You can run models locally through either [VLLM](https://docs.litellm.ai/docs/providers/vllm) or [Ollama](https://docs.litellm.ai/docs/providers/ollama)

E.g. to use a new model locally via Ollama, set in `.secrets.toml` or in a configuration file:

```toml
[config]
model = "ollama/qwen2.5-coder:32b"
fallback_models=["ollama/qwen2.5-coder:32b"]
custom_model_max_tokens=128000 # set the maximal input tokens for the model
duplicate_examples=true # will duplicate the examples in the prompt, to help the model to generate structured output

[ollama]
api_base = "http://localhost:11434" # or whatever port you're running Ollama on
```

By default, Ollama uses a context window size of 2048 tokens. In most cases this is not enough to cover pr-agent prompt and pull-request diff. Context window size can be overridden with the `OLLAMA_CONTEXT_LENGTH` environment variable. For example, to set the default context length to 8K, use: `OLLAMA_CONTEXT_LENGTH=8192 ollama serve`. More information you can find on the [official ollama faq](https://github.com/ollama/ollama/blob/main/docs/faq.md#how-can-i-specify-the-context-window-size).

Please note that the `custom_model_max_tokens` setting should be configured in accordance with the `OLLAMA_CONTEXT_LENGTH`. Failure to do so may result in unexpected model output.

!!! note "Local models vs commercial models"
    PR-Agent is compatible with almost any AI model, but analyzing complex code repositories and pull requests requires a model specifically optimized for code analysis.

    Commercial models such as GPT-5, Claude Sonnet, and Gemini have demonstrated robust capabilities in generating structured output for code analysis tasks with large input. In contrast, most open-source models currently available (as of January 2025) face challenges with these complex tasks.

    Based on our testing, local open-source models are suitable for experimentation and learning purposes (mainly for the `ask` command), but they are not suitable for production-level code analysis tasks.

    Hence, for production workflows and real-world usage, we recommend using commercial models.

### Hugging Face

To use a new model with Hugging Face Inference Endpoints, for example, set:

```toml
[config] # in configuration.toml
model = "huggingface/meta-llama/Llama-2-7b-chat-hf"
fallback_models=["huggingface/meta-llama/Llama-2-7b-chat-hf"]
custom_model_max_tokens=... # set the maximal input tokens for the model

[huggingface] # in .secrets.toml
key = ... # your Hugging Face api key
api_base = ... # the base url for your Hugging Face inference endpoint
```

(you can obtain a Llama2 key from [here](https://replicate.com/replicate/llama-2-70b-chat/api))

### Replicate

To use Llama2 model with Replicate, for example, set:

```toml
[config] # in configuration.toml
model = "replicate/llama-2-70b-chat:2c1608e18606fad2812020dc541930f2d0495ce32eee50074220b87300bc16e1"
fallback_models=["replicate/llama-2-70b-chat:2c1608e18606fad2812020dc541930f2d0495ce32eee50074220b87300bc16e1"]
[replicate] # in .secrets.toml
key = ...
```

(you can obtain a Llama2 key from [here](https://replicate.com/replicate/llama-2-70b-chat/api))

Also, review the [.secrets_template.toml](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/.secrets_template.toml) file for instructions on how to set keys for other models.

### Groq

To use Llama3 model with Groq, for example, set:

```toml
[config] # in configuration.toml
model = "llama3-70b-8192"
fallback_models = ["groq/llama3-70b-8192"]
[groq] # in .secrets.toml
key = ... # your Groq api key
```

(you can obtain a Groq key from [here](https://console.groq.com/keys))

### xAI

To use xAI's models with PR-Agent, set:

```toml
[config] # in configuration.toml
model = "xai/grok-2-latest"
fallback_models = ["xai/grok-2-latest"] # or any other model as fallback

[xai] # in .secrets.toml
key = "..." # your xAI API key
```

You can obtain an xAI API key from [xAI's console](https://console.x.ai/) by creating an account and navigating to the developer settings page.

### Vertex AI

To use Google's Vertex AI platform and its associated models (chat-bison/codechat-bison) set:

```toml
[config] # in configuration.toml
model = "vertex_ai/codechat-bison"
fallback_models="vertex_ai/codechat-bison"

[vertexai] # in .secrets.toml
vertex_project = "my-google-cloud-project"
vertex_location = ""
```

Your [application default credentials](https://cloud.google.com/docs/authentication/application-default-credentials) will be used for authentication so there is no need to set explicit credentials in most environments.

If you do want to set explicit credentials, then you can use the `GOOGLE_APPLICATION_CREDENTIALS` environment variable set to a path to a json credentials file.

### Google AI Studio

To use [Google AI Studio](https://aistudio.google.com/) models, set the relevant models in the configuration section of the configuration file:

```toml
[config] # in configuration.toml
model="gemini/gemini-1.5-flash"
fallback_models=["gemini/gemini-1.5-flash"]

[google_ai_studio] # in .secrets.toml
gemini_api_key = "..."
```

If you don't want to set the API key in the .secrets.toml file, you can set the `GOOGLE_AI_STUDIO.GEMINI_API_KEY` environment variable.

### Anthropic

To use Anthropic models, set the relevant models in the configuration section of the configuration file:

```toml
[config]
model="anthropic/claude-3-opus-20240229"
fallback_models=["anthropic/claude-3-opus-20240229"]
```

And also set the api key in the .secrets.toml file:

```toml
[anthropic]
KEY = "..."
```

See [litellm](https://docs.litellm.ai/docs/providers/anthropic#usage) documentation for more information about the environment variables required for Anthropic.

### Amazon Bedrock

To use Amazon Bedrock and its foundational models, add the below configuration:

```toml
[config] # in configuration.toml
model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
fallback_models=["bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"]

[aws]
AWS_ACCESS_KEY_ID="..."
AWS_SECRET_ACCESS_KEY="..."
AWS_REGION_NAME="..."
```

You can also use the new Meta Llama 4 models available on Amazon Bedrock:

```toml
[config] # in configuration.toml
model="bedrock/us.meta.llama4-scout-17b-instruct-v1:0"
fallback_models=["bedrock/us.meta.llama4-maverick-17b-instruct-v1:0"]
```

#### Using IAM Role Credentials (Recommended on AWS Compute)

When running PR-Agent on AWS infrastructure (EC2, ECS/Fargate, EKS with IRSA, Lambda, or any self-hosted GitHub Actions runner on AWS), the instance or task already has an IAM role attached. You can use those ambient credentials directly instead of storing long-lived static keys.

Set `AWS_USE_IMDS=true` in the environment. PR-Agent will resolve credentials via boto3's standard provider chain, which handles all AWS compute contexts transparently:

| Compute context | Mechanism |
|---|---|
| EC2 instance with IAM role | IMDSv2 (169.254.169.254) |
| ECS / Fargate task role | Task metadata endpoint |
| EKS pod with IRSA | Web identity token + STS |
| Lambda function | Runtime-injected credentials |

Minimal GitHub Actions workflow (no AWS secret keys required):

```yaml
- uses: Codium-ai/pr-agent@main
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    AWS_USE_IMDS: "true"
    # AWS_REGION_NAME: us-east-1  # optional if the instance metadata provides it
  with:
    command: review
```

The IAM role must have `bedrock:InvokeModel` permission on the target model ARN, for example:

```json
{
  "Effect": "Allow",
  "Action": "bedrock:InvokeModel",
  "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20240620-v1:0"
}
```

If you also configure static keys in `[aws]`, they serve as an automatic fallback: if the ambient credentials fail a Bedrock call (e.g., the role lacks `bedrock:InvokeModel`), PR-Agent retries with the static keys and logs a warning.

#### Custom Inference Profiles

To use a custom inference profile with Amazon Bedrock (for cost allocation tags and other configuration settings), add the `model_id` parameter to your configuration:

```toml
[config] # in configuration.toml
model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
fallback_models=["bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"]

[aws]
AWS_ACCESS_KEY_ID="..."
AWS_SECRET_ACCESS_KEY="..."
AWS_REGION_NAME="..."

[litellm]
model_id = "your-custom-inference-profile-id"
```

The `model_id` parameter will be passed to all Bedrock completion calls, allowing you to use custom inference profiles for better cost allocation and reporting.

See [litellm](https://docs.litellm.ai/docs/providers/bedrock#usage) documentation for more information about the environment variables required for Amazon Bedrock.

### DeepSeek

To use deepseek-chat model with DeepSeek, for example, set:

```toml
[config] # in configuration.toml
model = "deepseek/deepseek-chat"
fallback_models=["deepseek/deepseek-chat"]
```

and fill up your key

```toml
[deepseek] # in .secrets.toml
key = ...
```

(you can obtain a deepseek-chat key from [here](https://platform.deepseek.com))

### DeepInfra

To use DeepSeek model with DeepInfra, for example, set:

```toml
[config] # in configuration.toml
model = "deepinfra/deepseek-ai/DeepSeek-R1-Distill-Llama-70B"
fallback_models = ["deepinfra/deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"]
[deepinfra] # in .secrets.toml
key = ... # your DeepInfra api key
```

(you can obtain a DeepInfra key from [here](https://deepinfra.com/dash/api_keys))

### Mistral

To use models like Mistral or Codestral with Mistral, for example, set:

```toml
[config] # in configuration.toml
model = "mistral/mistral-small-latest"
fallback_models = ["mistral/mistral-medium-latest"]
[mistral] # in .secrets.toml
key = "..." # your Mistral api key
```

(you can obtain a Mistral key from [here](https://console.mistral.ai/api-keys))

### Codestral

To use Codestral model with Codestral, for example, set:

```toml
[config] # in configuration.toml
model = "codestral/codestral-latest"
fallback_models = ["codestral/codestral-2405"]
[codestral] # in .secrets.toml
key = "..." # your Codestral api key
```

(you can obtain a Codestral key from [here](https://console.mistral.ai/codestral))

### Openrouter

To use model from Openrouter, for example, set:

```toml
[config] # in configuration.toml
model="openrouter/anthropic/claude-3.7-sonnet"
fallback_models=["openrouter/deepseek/deepseek-chat"]
custom_model_max_tokens=20000

[openrouter]  # in .secrets.toml or passed an environment variable openrouter__key
key = "..." # your openrouter api key
```

(you can obtain an Openrouter API key from [here](https://openrouter.ai/settings/keys))

### Custom models

If the relevant model doesn't appear [here](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/algo/__init__.py), you can still use it as a custom model:

1. Set the model name in the configuration file:

```toml
[config]
model="custom_model_name"
fallback_models=["custom_model_name"]
```

2. Set the maximal tokens for the model:

```toml
[config]
custom_model_max_tokens= ...
```

3. Go to [litellm documentation](https://litellm.vercel.app/docs/proxy/quick_start#supported-llms), find the model you want to use, and set the relevant environment variables.

4. Most reasoning models do not support chat-style inputs (`system` and `user` messages) or temperature settings.
To bypass chat templates and temperature controls, set `config.custom_reasoning_model = true` in your configuration file.

## Dedicated parameters

### OpenAI models

```toml
[config]
reasoning_effort = "medium" # "none", "minimal", "low", "medium", "high", "xhigh"
```

With the OpenAI models that support reasoning effort (eg: gpt-5.4-mini), you can specify its reasoning effort via `config` section. The default value is `medium`. You can change it to any supported value based on your usage. Available values depend on the model and provider.

### Anthropic models

```toml
[config]
enable_claude_extended_thinking = false # Set to true to enable extended thinking feature
extended_thinking_budget_tokens = 2048
extended_thinking_max_output_tokens = 4096
```
