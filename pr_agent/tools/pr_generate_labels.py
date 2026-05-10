import copy
import re
from functools import partial
from typing import List, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import get_pr_diff, retry_with_fallback_models
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import get_user_labels, load_yaml, set_custom_labels
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.tools.registry import ToolRegistry


@ToolRegistry.register("generate_labels")
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
            self.git_provider.pr,
            self.vars,
            get_settings().pr_custom_labels_prompt.system,
            get_settings().pr_custom_labels_prompt.user,
        )

        # Initialize patches_diff and prediction attributes
        self.patches_diff = None
        self.prediction = None

    async def run(self):
        """
        Generates a PR labels using an AI model and publishes it to the PR.
        """

        try:
            get_logger().info(f"Generating a PR labels {self.pr_id}")
            if get_settings().config.publish_output:
                self.git_provider.publish_comment("Preparing PR labels...", is_temporary=True)

            await retry_with_fallback_models(self._prepare_prediction)

            get_logger().info(f"Preparing answer {self.pr_id}")
            if self.prediction:
                self._prepare_data()
            else:
                return None

            pr_labels = self._prepare_labels()

            if get_settings().config.publish_output:
                get_logger().info(f"Pushing labels {self.pr_id}")

                current_labels = self.git_provider.get_pr_labels()
                user_labels = get_user_labels(current_labels)
                pr_labels = pr_labels + user_labels

                if self.git_provider.is_supported("get_labels"):
                    self.git_provider.publish_labels(pr_labels)
                elif pr_labels:
                    value = ', '.join(v for v in pr_labels)
                    pr_labels_text = f"## PR Labels:\n{value}\n"
                    self.git_provider.publish_comment(pr_labels_text, is_temporary=False)
                self.git_provider.remove_initial_comment()
        except Exception as e:
            get_logger().error(f"Error generating PR labels {self.pr_id}: {e}")

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

        get_logger().info(f"Getting PR diff {self.pr_id}")
        self.patches_diff = get_pr_diff(self.git_provider, self.token_handler, model)
        get_logger().info(f"Getting AI prediction {self.pr_id}")
        self.prediction = await self._get_prediction(model)

    async def _get_prediction(self, model: str) -> str:
        """
        Generate an AI prediction for the PR labels based on the provided model.

        Args:
            model (str): The name of the model to be used for generating the prediction.

        Returns:
            str: The generated AI prediction.
        """
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff  # update diff

        environment = Environment(undefined=StrictUndefined)
        set_custom_labels(variables, self.git_provider)
        self.variables = variables

        system_prompt = environment.from_string(get_settings().pr_custom_labels_prompt.system).render(self.variables)
        user_prompt = environment.from_string(get_settings().pr_custom_labels_prompt.user).render(self.variables)

        response, finish_reason, _ = await self.ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt
        )

        return response

    def _prepare_data(self):
        # Load the AI prediction data into a dictionary
        self.data = load_yaml(self.prediction.strip())



    def _prepare_labels(self) -> List[str]:
        pr_types = []

        # If the 'labels' key is present in the dictionary, split its value by comma and assign it to 'pr_types'
        if 'labels' in self.data:
            if type(self.data['labels']) == list:
                pr_types = self.data['labels']
            elif type(self.data['labels']) == str:
                pr_types = self.data['labels'].split(',')
        pr_types = [label.strip() for label in pr_types]

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
