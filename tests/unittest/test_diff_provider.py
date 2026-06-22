from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import _GIT_PROVIDERS
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


def test_registered():
    assert _GIT_PROVIDERS["diff"] is DiffGitProvider


def test_get_diff_files(monkeypatch):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    files = provider.get_diff_files()
    assert len(files) == 1
    assert files[0].filename == "foo.py"
    assert files[0].edit_type == EDIT_TYPE.MODIFIED


def test_publish_comment_to_stdout(capsys):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    provider.publish_comment("# Review\nlooks good")
    captured = capsys.readouterr()
    assert "looks good" in captured.out


def test_publish_comment_to_file(tmp_path):
    out = tmp_path / "review.md"
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", str(out))
    provider = DiffGitProvider(None)
    provider.publish_comment("# Review\nsaved")
    assert "saved" in out.read_text(encoding="utf-8")


def test_empty_diff_raises():
    get_settings().set("diff.content", "")
    get_settings().set("diff.output_path", None)
    try:
        DiffGitProvider(None)
        assert False, "expected ValueError"
    except ValueError:
        pass
