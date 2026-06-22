import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.diff_provider import DiffGitProvider

DIFF = """diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 line1
-line2
+line2-changed
 line3
"""


def test_provider_end_to_end_files_and_output(capsys):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)

    # files parsed and content reconstructed where working tree is absent
    files = provider.get_diff_files()
    assert files[0].filename == "foo.py"
    assert files[0].base_file == ""  # foo.py not on disk -> patch-only fallback

    # output reaches stdout
    provider.publish_comment("## PR Review\n- finding one")
    assert "finding one" in capsys.readouterr().out
