"""The lancedb similar-issue query uses the same cosine metric and five-hit limit as the other backends."""
from types import SimpleNamespace

from pr_agent.tools.pr_similar_issue import _lancedb_similar_search


class _FakeBuilder:
    def __init__(self, rows):
        self._rows = rows
        self.distance_type_value = None
        self.limit_value = None
        self.where_called_with = None

    def distance_type(self, value):
        self.distance_type_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def where(self, expr, prefilter=False):
        self.where_called_with = (expr, prefilter)
        return self

    def to_list(self):
        return self._rows


def _fake_table(builder):
    return SimpleNamespace(search=lambda query: builder)


def test_lancedb_query_requests_cosine_distance_and_five_hits():
    """The search reflects cosine similarity scores and matches the other backends' five-hit limit."""
    rows = [
        {"id": "1.issue", "_distance": 0.11, "text": "a"},
        {"id": "2.issue", "_distance": 0.30, "text": "b"},
    ]
    builder = _FakeBuilder(rows)

    result = _lancedb_similar_search(_fake_table(builder), [0.0] * 8, "org/repo-a")

    assert builder.distance_type_value == "cosine"
    assert builder.limit_value == 5
    assert builder.where_called_with == ("metadata.repo='org/repo-a'", True)
    assert result == rows
