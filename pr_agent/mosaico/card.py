"""Builds the MOSAICO A2A agent card for pr-agent.

Mirrors docstring-agent/__main__.py:36-68. The observability extension is advertised
as required=True; streaming is advertised as False (load-bearing: the reference agent
selects message/send vs message/stream from capabilities.streaming)."""
import os

from a2a.types import (AgentCapabilities, AgentCard, AgentExtension,
                       AgentInterface, AgentSkill)

from pr_agent.algo.utils import get_version

OBSERVABILITY_EXTENSION_URI = "https://mosaico-project.eu/extensions/mosaico-observability"

AGENT_NAME = "PR-Agent Solution Agent"
DEFAULT_PORT = 9000


def _jsonrpc_interface() -> AgentInterface:
    host = os.getenv("AGENT_CARD_HOST") or "localhost"
    port = os.getenv("AGENT_CARD_PORT") or os.getenv("PORT") or str(DEFAULT_PORT)
    return AgentInterface(
        protocol_binding="JSONRPC",
        protocol_version="1.0",
        url=f"http://{host}:{port}/",
    )


def _build_skills() -> list:
    return [
        AgentSkill(
            id="review",
            name="Review",
            description="Review a pull request or supplied diff and produce a structured "
                        "code review (key issues, effort estimate, security concerns).",
            tags=["review", "code-review", "pull-request"],
            examples=["Review https://github.com/org/repo/pull/1",
                      "Review this diff"],
        ),
        AgentSkill(
            id="improve",
            name="Improve",
            description="Propose concrete code suggestions to improve a pull request or "
                        "supplied diff.",
            tags=["improve", "code-suggestions", "pull-request"],
            examples=["Improve https://github.com/org/repo/pull/1",
                      "Suggest improvements for this diff"],
        ),
        AgentSkill(
            id="describe",
            name="Describe",
            description="Generate a title and description for a pull request or supplied diff.",
            tags=["describe", "description", "pull-request"],
            examples=["Describe https://github.com/org/repo/pull/1",
                      "Describe this diff"],
        ),
        AgentSkill(
            id="ask",
            name="Ask",
            description="Answer a free-text question scoped to a specific pull request or "
                        "supplied git diff (requires a PR URL or a diff to answer).",
            tags=["ask", "question", "pull-request", "diff"],
            examples=["What does this PR change?",
                      "Ask https://github.com/org/repo/pull/1 what the risk is"],
        ),
    ]


def build_agent_card() -> AgentCard:
    extensions = [
        AgentExtension(
            uri=OBSERVABILITY_EXTENSION_URI,
            description="Includes metadata in A2A messages to enable end-to-end "
                        "observability, such as trace linking between the reference "
                        "agent and any downstream agents.",
            required=True,
        ),
    ]
    return AgentCard(
        name=AGENT_NAME,
        description="PR-Agent solution agent for pull-request and unified-diff code review. "
                    "Given a pull-request URL or a supplied git diff, it produces a structured "
                    "code review (key issues, security concerns, effort estimate), inline code "
                    "suggestions, a generated PR title and description, and answers to questions "
                    "about that specific pull request or diff. Every action is anchored to a "
                    "concrete pull request or git diff supplied in the request.",
        version=get_version(),
        supported_interfaces=[_jsonrpc_interface()],
        default_input_modes=["text", "text/plain"],
        default_output_modes=["text", "text/plain", "text/markdown"],
        capabilities=AgentCapabilities(
            streaming=False,
            extensions=extensions,
        ),
        skills=_build_skills(),
    )
