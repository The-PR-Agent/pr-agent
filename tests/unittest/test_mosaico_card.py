"""Tests for the MOSAICO agent card (A2A 1.0)."""
import json
import os

from google.protobuf.json_format import MessageToDict

from pr_agent.mosaico.card import (OBSERVABILITY_EXTENSION_URI,
                                   build_agent_card)

_REGISTRATION_JSON = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker", "mosaico",
    "pr-agent-solution-agent.json",
)


class TestAgentCard:
    def test_supported_interfaces_present(self):
        """A2A 1.0: url+protocol_version moved into supported_interfaces."""
        card = build_agent_card()
        assert len(card.supported_interfaces) == 1
        iface = card.supported_interfaces[0]
        assert iface.protocol_binding == "JSONRPC"
        assert iface.protocol_version == "1.0"
        assert iface.url.endswith("/")

    def test_url_absent_from_card_top_level(self):
        """Non-vacuity: the old 0.3 top-level url= field MUST NOT appear in the 1.0 card."""
        d = MessageToDict(build_agent_card())
        assert "url" not in d, f"top-level 'url' must not appear in 1.0 card: {d}"
        assert "protocolVersion" not in d, f"top-level 'protocolVersion' must not appear: {d}"

    def test_supported_interfaces_serialise(self):
        """Serialised card carries supportedInterfaces (camelCase) with the interface URL."""
        d = MessageToDict(build_agent_card())
        assert "supportedInterfaces" in d
        ifaces = d["supportedInterfaces"]
        assert len(ifaces) == 1
        assert ifaces[0]["protocolBinding"] == "JSONRPC"
        assert ifaces[0]["protocolVersion"] == "1.0"

    def test_streaming_false(self):
        card = build_agent_card()
        assert card.capabilities.streaming is False

    def test_observability_extension_required(self):
        card = build_agent_card()
        exts = list(card.capabilities.extensions)
        assert exts, "no extensions advertised"
        obs = [e for e in exts if e.uri == OBSERVABILITY_EXTENSION_URI]
        assert len(obs) == 1, "observability extension not found exactly once"
        assert obs[0].required is True

    def test_observability_extension_uri_value(self):
        assert OBSERVABILITY_EXTENSION_URI == \
            "https://mosaico-project.eu/extensions/mosaico-observability"

    def test_observability_extension_serialises(self):
        """Extension must survive MessageToDict round-trip (regression vs broken toml)."""
        d = MessageToDict(build_agent_card())
        exts = d["capabilities"]["extensions"]
        obs = [e for e in exts if e["uri"] == OBSERVABILITY_EXTENSION_URI]
        assert len(obs) == 1
        assert obs[0]["required"] is True

    def test_four_skills(self):
        card = build_agent_card()
        skill_ids = {s.id for s in card.skills}
        assert skill_ids == {"review", "improve", "describe", "ask"}

    def test_input_output_modes(self):
        card = build_agent_card()
        assert list(card.default_input_modes) == ["text", "text/plain"]
        assert "text/markdown" in list(card.default_output_modes)

    def test_version_is_nonempty(self):
        card = build_agent_card()
        assert isinstance(card.version, str) and card.version


class TestRoutingDistinctiveness:
    """Lock the PR/diff niche so the card stays distinct from the generic Mini-SWE-agent
    in the repository's vector-similarity routing."""

    def test_description_names_pr_and_diff_niche(self):
        desc = build_agent_card().description.lower()
        assert "pull-request" in desc or "pull request" in desc
        assert "diff" in desc

    def test_description_anchors_scope_positively(self):
        # Distinctiveness is carried POSITIVELY (state our scope), never by negation:
        # embedding similarity ignores negation and naming "generic coding" would pull
        # us toward the Mini-SWE cluster. Assert a positive anchoring phrase is present
        # and that the generic-SWE wording never appears in our embedding text.
        desc = build_agent_card().description.lower()
        assert "anchored to" in desc or "supplied in the request" in desc
        assert "generic software engineering" not in desc

    def test_ask_skill_is_pr_or_diff_scoped(self):
        card = build_agent_card()
        ask = next(s for s in card.skills if s.id == "ask")
        low = ask.description.lower()
        # Scoped to a PR or diff; must NOT advertise generic "about code" Q&A.
        assert "pull request" in low or "pull-request" in low
        assert "diff" in low
        assert "about code" not in low

    def test_registration_json_consistent_with_card(self):
        with open(_REGISTRATION_JSON, encoding="utf-8") as f:
            reg = json.load(f)
        for field in ("description", "role", "objective"):
            low = reg[field].lower()
            assert "diff" in low or "pull-request" in low or "pull request" in low, (
                f"registration JSON '{field}' must carry the PR/diff niche to stay "
                f"consistent with the card: {reg[field]!r}"
            )
