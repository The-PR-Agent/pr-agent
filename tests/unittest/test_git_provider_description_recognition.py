from pr_agent.git_providers.git_provider import GitProvider


class DummyGitProvider(GitProvider):
    """Minimal concrete GitProvider used to exercise description recognition."""

    def __init__(self, description: str = ""):
        self._description = description
        self.user_description = None

    def is_supported(self, capability: str) -> bool:
        return True

    def get_pr_description_full(self) -> str:
        return self._description

    def get_files(self):
        return []

    def get_diff_files(self):
        return []

    def publish_description(self, pr_title, pr_body):
        pass

    def publish_code_suggestions(self, code_suggestions):
        return False

    def get_languages(self):
        return {}

    def get_pr_branch(self):
        return "main"

    def get_user_id(self):
        return 1

    def get_repo_settings(self):
        return ""

    def publish_comment(self, pr_comment, is_temporary=False):
        pass

    def publish_inline_comment(self, body, relevant_file, relevant_line_in_file, original_suggestion=None):
        pass

    def publish_inline_comments(self, comments):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def get_issue_comments(self):
        return []

    def publish_labels(self, labels):
        pass

    def get_pr_labels(self, update=False):
        return []

    def add_eyes_reaction(self, issue_comment_id, disable_eyes=False):
        return None

    def remove_reaction(self, issue_comment_id, reaction_id):
        return False

    def get_commit_messages(self):
        return []


# --- Test fixtures ---

GENERATED_DESCRIPTION = """### **User description**
Some original user text

___

### **PR Type**
Bug fix

___

### **Description**
This PR fixes the bug
"""

# A generated description with a line leaked above the first standard header,
# e.g. an instruction the model was told to emit via extra_instructions.
# Without the hidden marker, leading noise defeats startswith detection.
# This is a known limitation for old descriptions (pre-marker); the hidden
# HTML comment handles this case for new descriptions.
LEADING_NOISE_DESCRIPTION = """Recommendation: please review this MR
### **User description**
Some original user text

___

### **PR Type**
Bug fix

___

### **Description**
This PR fixes the bug
"""

PLAIN_USER_DESCRIPTION = "This is a plain human description of the MR."

# A generated description that carries the hidden HTML comment injected
# at write time by pr_agent.tools.pr_description.
HIDDEN_MARKER_DESCRIPTION = """<!-- pr-agent-generated -->
### **User description**
Some original user text

___

### **PR Type**
Bug fix

___

### **Description**
This PR fixes the bug
"""

# Hidden marker + leading noise: the HTML comment is detected anywhere,
# so leading noise does NOT defeat recognition.
HIDDEN_MARKER_WITH_NOISE = """Recommendation: please review this MR
<!-- pr-agent-generated -->
### **User description**
Some original user text

___

### **PR Type**
Bug fix
"""

# A human description that quotes a pr-agent section header inside a
# blockquote.  This must NOT be classified as generated.
HUMAN_QUOTING_HEADER = """Some human text.

> ### **PR Type**
> Bug fix

More human text."""


# --- _is_generated_by_pr_agent tests ---

def test_is_generated_when_description_starts_with_header():
    """Legacy descriptions that start with a standard header are detected."""
    provider = DummyGitProvider(GENERATED_DESCRIPTION)
    assert provider._is_generated_by_pr_agent(GENERATED_DESCRIPTION.lower()) is True


def test_hidden_html_marker_is_detected():
    """The hidden HTML comment injected at write time is the primary signal."""
    provider = DummyGitProvider(HIDDEN_MARKER_DESCRIPTION)
    assert provider._is_generated_by_pr_agent(HIDDEN_MARKER_DESCRIPTION.lower()) is True


def test_hidden_marker_with_leading_noise():
    """Even with extra leading content, the HTML comment is detected anywhere."""
    provider = DummyGitProvider(HIDDEN_MARKER_WITH_NOISE)
    assert provider._is_generated_by_pr_agent(HIDDEN_MARKER_WITH_NOISE.lower()) is True


def test_plain_user_description_is_not_generated():
    provider = DummyGitProvider(PLAIN_USER_DESCRIPTION)
    assert provider._is_generated_by_pr_agent(PLAIN_USER_DESCRIPTION.lower()) is False


def test_generic_header_in_middle_of_user_text_is_not_generated():
    """A human description that happens to contain a generic section header
    (not a pr-agent-specific one) must not be flagged as generated."""
    description = "Some overview text\n\n### **Description**\nDetails here"
    provider = DummyGitProvider(description)
    assert provider._is_generated_by_pr_agent(description.lower()) is False


def test_human_text_quoting_header_is_not_generated():
    """A human description that quotes a pr-agent header in a blockquote
    must not be misclassified as generated.  This was the failure mode of
    matching visible headers anywhere in the body."""
    provider = DummyGitProvider(HUMAN_QUOTING_HEADER)
    assert provider._is_generated_by_pr_agent(HUMAN_QUOTING_HEADER.lower()) is False


def test_leading_noise_without_marker_not_detected():
    """Old descriptions (no hidden marker) with leading noise above the first
    header are NOT detected by startswith.  This is a known limitation:
    after one /describe cycle the re-embedded content will have headers at
    the top (get_user_description clips to first header), so the next cycle
    detects it.  The hidden marker handles this for new descriptions."""
    provider = DummyGitProvider(LEADING_NOISE_DESCRIPTION)
    assert provider._is_generated_by_pr_agent(LEADING_NOISE_DESCRIPTION.lower()) is False


# --- get_user_description tests ---

def test_get_user_description_from_clean_generated_description():
    provider = DummyGitProvider(GENERATED_DESCRIPTION)
    assert provider.get_user_description() == "Some original user text"


def test_get_user_description_with_hidden_marker():
    """The hidden HTML comment should not interfere with user description extraction."""
    provider = DummyGitProvider(HIDDEN_MARKER_DESCRIPTION)
    assert provider.get_user_description() == "Some original user text"


def test_get_user_description_preserves_plain_user_description():
    provider = DummyGitProvider(PLAIN_USER_DESCRIPTION)
    assert provider.get_user_description() == PLAIN_USER_DESCRIPTION


def test_get_user_description_with_leading_content():
    """When the description has leading noise but no hidden marker, it is
    treated as a plain user description (the whole text is returned)."""
    provider = DummyGitProvider(LEADING_NOISE_DESCRIPTION)
    result = provider.get_user_description()
    assert result == LEADING_NOISE_DESCRIPTION.strip()
