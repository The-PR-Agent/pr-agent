import copy
from functools import partial
from typing import List

from pydantic import ValidationError

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.output_models import Labels
from pr_agent.algo.pr_processing import (
    OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
    FallbackEligibleError,
    get_pr_diff,
    retry_with_fallback_models,
)
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import get_user_labels, load_yaml, set_custom_labels
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.log import get_logger


class PRGenerateLabels:
    def __init__(self, pr_url: str, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        """
        Initialize the PRGenerateLabels object with the necessary attributes and objects for generating labels
        corresponding to the PR using an AI model.
        Args:
            pr_url (str): The URL of the pull request.
            args (list, optional): List of arguments passed to the PRGenerateLabels class. Defaults to None.
        """
        # Initialize the git provider and main PR language
        self.git_provider = get_git_provider()(pr_url)
        self.main_pr_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.pr_id = self.git_provider.get_pr_id()

        # Initialize the AI handler
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_pr_language

        # Initialize the variables dictionary
        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.git_provider.get_pr_description(full=False),
            "language": self.main_pr_language,
            "diff": "",  # empty diff for initial calculation
            "extra_instructions": get_settings().pr_description.extra_instructions,
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "enable_custom_labels": get_settings().config.enable_custom_labels,
            "custom_labels_class": "",  # will be filled if necessary in 'set_custom_labels' function
        }

        # Initialize the token handler
        self.token_handler = TokenHandler(
            getattr(self.git_provider, "pr", None),
            self.vars,
            get_settings().pr_custom_labels_prompt.system,
            get_settings().pr_custom_labels_prompt.user,
        )

        # Initialize prediction state
        self.patches_diff = None
        self.prediction = None
        self.data = None

    async def run(self):
        """
        Generates a PR labels using an AI model and publishes it to the PR.
        """

        progress_comment = None
        try:
            get_logger().info(f"Generating a PR labels {self.pr_id}")
            if get_settings().config.publish_output:
                progress_comment = self.git_provider.publish_comment(
                    "Preparing PR labels...", is_temporary=True
                )

            await retry_with_fallback_models(self._prepare_prediction, git_provider=self.git_provider)

            get_logger().info(f"Preparing answer {self.pr_id}")
            if self.prediction is None:
                return None

            pr_labels = self._prepare_labels()

            if get_settings().config.publish_output:
                get_logger().info(f"Pushing labels {self.pr_id}")

                if self.git_provider.is_supported("get_labels"):
                    current_labels = self.git_provider.get_pr_labels()
                    user_labels = get_user_labels(current_labels)
                    pr_labels = pr_labels + user_labels
                    self.git_provider.publish_labels(pr_labels)
                elif pr_labels:
                    value = ', '.join(v for v in pr_labels)
                    pr_labels_text = f"## PR Labels:\n{value}\n"
                    self.git_provider.publish_comment(pr_labels_text, is_temporary=False)
        except Exception as e:
            get_logger().error(f"Error generating PR labels {self.pr_id}: {e}")
            if get_settings().config.get("propagate_tool_errors", False):
                raise
        finally:
            if progress_comment is not None:
                try:
                    self.git_provider.remove_initial_comment()
                except Exception as e:
                    get_logger().error(f"Failed to remove temporary PR labels comment {self.pr_id}: {e}")

        return ""

    async def _prepare_prediction(self, model: str) -> None:
        """
        Prepare the AI prediction for the PR labels based on the provided model.

        Args:
            model (str): The name of the model to be used for generating the prediction.

        Returns:
            None

        Raises:
            Any exceptions raised by the 'get_pr_diff' and '_get_prediction' functions.

        """

        self.prediction = None
        self.data = None

        get_logger().info(f"Getting PR diff {self.pr_id}")
        variables = copy.deepcopy(self.vars)
        set_custom_labels(variables, self.git_provider)
        output_token_reserve = getattr(self.ai_handler, "get_output_token_reserve", None)
        budget = AttemptTokenBudget.for_prompt_attempt(
            model,
            getattr(self.git_provider, "pr", None),
            variables,
            get_settings().pr_custom_labels_prompt.system,
            get_settings().pr_custom_labels_prompt.user,
            ai_handler=self.ai_handler,
            output_token_reserve=output_token_reserve,
        )
        patches_diff = get_pr_diff(
            self.git_provider,
            budget.token_handler,
            model,
            output_token_reserve=output_token_reserve,
        )
        if not patches_diff:
            raise FallbackEligibleError(f"No PR diff fits the /generate_labels request for {model}")
        fitted = budget.fit_prompt_variable(
            variables,
            "diff",
            patches_diff,
            ai_handler=self.ai_handler,
            default_output_tokens=OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
            preserve_minimum=True,
        )
        if fitted.optional_text != patches_diff:
            raise FallbackEligibleError(
                f"The complete packed labels diff does not fit the token limit for {model}"
            )
        variables["diff"] = fitted.optional_text
        self.patches_diff = fitted.optional_text
        self._attempt_system_prompt = fitted.system_prompt
        self._attempt_user_prompt = fitted.user_prompt
        get_logger().info(f"Getting AI prediction {self.pr_id}")
        prediction = await self._get_prediction(model)
        data = self._load_valid_labels_yaml(prediction)

        self.variables = variables
        self.prediction = prediction
        self.data = data

    async def _get_prediction(self, model: str) -> str:
        """
        Generate an AI prediction for the PR labels based on the provided model.

        Args:
            model (str): The name of the model to be used for generating the prediction.

        Returns:
            str: The generated AI prediction.
        """
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=self._attempt_system_prompt,
            user=self._attempt_user_prompt,
        )

        return response

    def _prepare_data(self):
        self.data = self._load_valid_labels_yaml(self.prediction)

    @staticmethod
    def _load_valid_labels_yaml(prediction: str) -> dict:
        """Load a usable labels response or fail the current model attempt."""
        data = load_yaml(prediction.strip())
        try:
            return Labels.model_validate(data).model_dump()
        except ValidationError as error:
            first_error = error.errors(include_input=False)[0]
            field = ".".join(str(part) for part in first_error["loc"]) or "$"
            raise FallbackEligibleError(f"Invalid labels model output at {field}: {first_error['msg']}") from error

    def _prepare_labels(self) -> List[str]:
        pr_types = self.data["labels"].copy()

        # convert lowercase labels to original case
        try:
            if "labels_minimal_to_labels_dict" in self.variables:
                d: dict = self.variables["labels_minimal_to_labels_dict"]
                for i, label_i in enumerate(pr_types):
                    if label_i in d:
                        pr_types[i] = d[label_i]
        except Exception as e:
            get_logger().error(f"Error converting labels to original case {self.pr_id}: {e}")

        return pr_types
