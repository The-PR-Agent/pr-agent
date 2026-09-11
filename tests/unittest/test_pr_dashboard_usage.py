from decimal import Decimal

import pytest

from pr_dashboard import store, usage


def _seed(conn):
    rows = [
        ("2026-09-08T10:00:00+00:00", "github", "o/a", 1, "review", "flash", 0, 1000, "0.01", "complete", "ok"),
        ("2026-09-09T10:00:00+00:00", "github", "o/a", 2, "improve", "flash", 1, 2000, "0.02", "complete", "ok"),
        ("2026-09-09T11:00:00+00:00", "github", "o/b", 3, "review", "ollama/l3", 0, 500, None, "unavailable", "ok"),
        ("2026-09-10T09:00:00+00:00", "github", "o/b", 4, "review", None, 0, None, None, None, "failed"),
    ]
    for started, provider, slug, number, command, model, fallback, tokens, cost, cost_status, status in rows:
        conn.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (started, status, provider, slug, number, command, model, fallback, tokens, cost, cost_status),
        )


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "usage.db")
    _seed(connection)
    return connection


class TestTotals:
    def test_counts_and_sums(self, conn):
        """Totals count every attempt and sum only priced costs"""
        result = usage.totals(conn)
        assert result["runs"] == 4
        assert result["ok"] == 3
        assert result["failed"] == 1
        assert result["tokens"] == 3500
        assert result["cost"] == Decimal("0.03")

    def test_unpriced_and_fallback_counts(self, conn):
        """Unpriced and fallback runs are reported rather than hidden"""
        result = usage.totals(conn)
        assert result["unpriced_runs"] == 2
        assert result["fallback_runs"] == 1

    def test_since_filter(self, conn):
        """A since bound excludes older runs"""
        result = usage.totals(conn, since="2026-09-09T00:00:00+00:00")
        assert result["runs"] == 3
        assert result["tokens"] == 2500

    def test_cost_sum_is_decimal_exact(self, tmp_path):
        """Summed cost is exact Decimal arithmetic, not float, even for values floats mis-add

        Uses an isolated store (not the shared seeded fixture) holding only two rows priced
        0.1 and 0.2, so a float-based sum is not masked by other seeded costs rounding the
        total back to a value that happens to str() cleanly. 0.1 + 0.2 as Python floats is
        0.30000000000000004, not 0.3 - the canonical case float addition gets wrong.
        """
        connection = store.connect(tmp_path / "exact.db")
        connection.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES (?, 'ok', 'github', 'o/c', 9, 'review', 'flash', 0, 10, '0.1', 'complete')",
            ("2026-09-09T12:00:00+00:00",),
        )
        connection.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES (?, 'ok', 'github', 'o/c', 10, 'review', 'flash', 0, 10, '0.2', 'complete')",
            ("2026-09-09T13:00:00+00:00",),
        )
        # totals() sums via a WHERE ... IS NOT NULL scan; by_dimension() sums via group_concat
        # and string-splitting. Both are separate code paths and both must be exact.
        assert usage.totals(connection)["cost"] == Decimal("0.3")
        rows = usage.by_dimension(connection, "repo")
        by_label = {row["label"]: row for row in rows}
        assert by_label["o/c"]["cost"] == Decimal("0.3")


class TestByDimension:
    def test_by_repo(self, conn):
        """Usage groups by repository, newest cost first"""
        rows = usage.by_dimension(conn, "repo")
        labels = {row["label"]: row for row in rows}
        assert labels["o/a"]["tokens"] == 3000
        assert labels["o/a"]["cost"] == Decimal("0.03")
        # o/b's only two runs (3 and 4) both have total_cost_usd IS NULL -- the group has zero
        # priced rows, so its cost must read as unreported, never as a real, known 0.
        assert labels["o/b"]["cost"] is None

    def test_group_with_no_priced_runs_reports_none_not_zero(self, conn):
        """A group whose every run was unpriced (local model, failure before pricing) is None"""
        rows = usage.by_dimension(conn, "repo")
        labels = {row["label"]: row for row in rows}
        assert labels["o/b"]["cost"] is None
        assert labels["o/b"]["runs"] == 2

    def test_group_with_a_mix_of_priced_and_unpriced_runs_sums_only_the_priced_ones(self, conn):
        """A group with at least one priced run still reports a real Decimal cost"""
        rows = usage.by_dimension(conn, "repo")
        labels = {row["label"]: row for row in rows}
        # o/a's two runs (1 and 2) are both priced: 0.01 + 0.02.
        assert labels["o/a"]["cost"] == Decimal("0.03")

    def test_by_model(self, conn):
        """Usage groups by model"""
        rows = usage.by_dimension(conn, "model")
        assert {row["label"] for row in rows} >= {"flash", "ollama/l3"}

    def test_by_command(self, conn):
        """Usage groups by command"""
        rows = usage.by_dimension(conn, "command")
        assert {row["label"] for row in rows} == {"review", "improve"}

    def test_unknown_dimension_is_rejected(self, conn):
        """An unknown dimension raises rather than reaching SQL"""
        with pytest.raises(ValueError):
            usage.by_dimension(conn, "repo_slug; DROP TABLE runs")


class TestDailySeries:
    def test_groups_by_day(self, conn):
        """The daily series has one entry per day with runs"""
        series = usage.daily_tokens(conn, days=3650)
        by_day = {row["day"]: row for row in series}
        assert by_day["2026-09-09"]["tokens"] == 2500

    def test_day_with_no_priced_runs_reports_none_not_zero(self, conn):
        """2026-09-10's only run (4) has total_cost_usd IS NULL, so the day's cost is None"""
        series = usage.daily_tokens(conn, days=3650)
        by_day = {row["day"]: row for row in series}
        assert by_day["2026-09-10"]["cost"] is None
        # 2026-09-09 mixes a priced run (2, 0.02) with an unpriced one (3): it still has a
        # real Decimal cost, since the fix is about zero priced rows, not zero cost.
        assert by_day["2026-09-09"]["cost"] == Decimal("0.02")


class TestUsagePage:
    def test_renders_with_an_empty_store(self, tmp_path, monkeypatch):
        """The usage page renders before any run has been recorded"""
        from fastapi.testclient import TestClient

        from pr_dashboard import app as app_module

        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "empty.db")
        response = TestClient(application).get("/usage")
        assert response.status_code == 200
        assert "0 runs" in response.text

    def test_unpriced_cost_is_not_rendered_as_zero(self, tmp_path):
        """When no run in the window was priced, total cost reads as unreported, never as 0 USD"""
        from fastapi.testclient import TestClient

        from pr_dashboard import app as app_module

        db_path = tmp_path / "usage.db"
        connection = store.connect(db_path)
        connection.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES ('2026-09-10T09:00:00+00:00', 'ok', 'github', 'o/b', 4, 'review', 'ollama/l3', "
            "0, 500, NULL, 'unavailable')"
        )
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=db_path)
        response = TestClient(application).get("/usage")
        assert response.status_code == 200
        assert "1 runs could not be priced" in response.text
        # Every run in this store is unpriced, so the total-cost line must read as unreported,
        # never as a misleading "Cost 0 USD" that looks like a real, known zero.
        assert "cost not reported" in response.text.lower()
        assert "cost 0 usd" not in response.text.lower()

    def test_unpriced_group_reads_as_not_reported_in_the_tables(self, tmp_path):
        """By-repo/model/command tables show "not reported" for a group with zero priced runs

        This is the defect from the fix plan written down as a test: with an entirely unpriced
        repo (ollama, no pricing data), the By-repo table must not print "0" for its cost --
        that reads as "this repo's reviews are free", which is not something the dashboard
        knows.
        """
        from fastapi.testclient import TestClient

        from pr_dashboard import app as app_module

        db_path = tmp_path / "usage.db"
        connection = store.connect(db_path)
        connection.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES ('2026-09-10T09:00:00+00:00', 'ok', 'github', 'o/b', 4, 'review', 'ollama/l3', "
            "0, 500, NULL, 'unavailable')"
        )
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=db_path)
        response = TestClient(application).get("/usage")
        assert response.status_code == 200
        by_repo = response.text.split("By repo</h2>", 1)[1].split("</table>", 1)[0]
        assert "not reported" in by_repo
        assert "<td>0</td>" not in by_repo

    def test_by_model_table_notes_the_fallback_cost_attribution_caveat(self, tmp_path):
        """The By-model table names the fallback-run cost-attribution caveat, with a real count"""
        from fastapi.testclient import TestClient

        from pr_dashboard import app as app_module

        db_path = tmp_path / "usage.db"
        connection = store.connect(db_path)
        _seed(connection)
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=db_path)
        response = TestClient(application).get("/usage")
        assert response.status_code == 200
        by_model = response.text.split("By model</h2>", 1)[1].split("</table>", 1)[0]
        assert "fallback" in by_model.lower()
        assert "1 runs" in by_model  # totals.fallback_runs from _seed's one fallback_used=1 row

    def test_dimension_sql_injection_attempt_is_rejected(self, tmp_path):
        """A malicious dimension value cannot reach SQL through the route"""
        from fastapi.testclient import TestClient

        from pr_dashboard import app as app_module

        db_path = tmp_path / "usage.db"
        connection = store.connect(db_path)
        _seed(connection)
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=db_path)
        client = TestClient(application)
        response = client.get("/usage", params={"dimension": "1; DROP TABLE runs --"})
        assert response.status_code in (400, 404, 422)
        # Probe with a *fresh* connection, opened after the request: the `connection` held
        # open above is on a WAL database and could keep reading a snapshot from before a
        # DROP even if one had executed through the route. A fresh connection proves the
        # table genuinely still exists in the database, not just in a stale read.
        probe = store.connect(db_path)
        table_exists = probe.execute(
            "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()["n"]
        assert table_exists == 1
        surviving = probe.execute("SELECT count(*) AS n FROM runs").fetchone()["n"]
        assert surviving == 4


class TestCorruptCost:
    def test_corrupt_stored_cost_is_unpriced_not_zero(self, tmp_path):
        """A non-NULL unparseable total_cost_usd reads as unpriced, never as a known $0"""
        connection = store.connect(tmp_path / "corrupt.db")
        connection.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES ('2026-09-10T09:00:00+00:00', 'ok', 'github', 'o/bad', 1, 'review', 'flash', "
            "0, 100, 'bad', 'complete')"
        )
        result = usage.totals(connection)
        assert result["runs"] == 1
        assert result["unpriced_runs"] == 1
        assert result["cost"] == Decimal("0")
        rows = usage.by_dimension(connection, "repo")
        assert rows[0]["cost"] is None
        series = usage.daily_tokens(connection, days=3650)
        assert series[0]["cost"] is None

    def test_corrupt_cost_on_usage_page_is_not_reported(self, tmp_path):
        """The usage page shows not reported when the only stored cost is corrupt TEXT"""
        from fastapi.testclient import TestClient

        from pr_dashboard import app as app_module

        db_path = tmp_path / "usage.db"
        connection = store.connect(db_path)
        connection.execute(
            "INSERT INTO runs (started_at, status, provider, repo_slug, pr_number, command, model_used, "
            "fallback_used, total_tokens, total_cost_usd, cost_status) "
            "VALUES ('2026-09-10T09:00:00+00:00', 'ok', 'github', 'o/bad', 1, 'review', 'flash', "
            "0, 100, 'bad', 'complete')"
        )
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=db_path)
        response = TestClient(application).get("/usage")
        assert response.status_code == 200
        assert "cost not reported" in response.text.lower()
        assert "cost 0 usd" not in response.text.lower()
