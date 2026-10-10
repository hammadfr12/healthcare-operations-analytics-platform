"""API contract tests.

These run against the real store, so they catch the class of bug that unit
tests with hand-built fixtures miss: SQL that does not bind against the schema
the pipeline actually produces. ``cancellation_reasons`` shipped querying
``metric_date`` on a table that has only ``appointment_date``, and nothing
caught it because nothing exercised the query against the real schema.
"""

from __future__ import annotations

import csv
import io

import pytest


class TestMeta:
    def test_health_does_not_require_the_store(self, client) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_reports_the_store_is_built(self, client) -> None:
        body = client.get("/api/ready").json()
        assert body["status"] == "ready"
        assert body["pipeline_status"] == "succeeded"

    def test_dimensions_enumerate_real_values(self, client) -> None:
        body = client.get("/api/dimensions").json()
        for key in ("departments", "facilities", "visit_types", "statuses"):
            assert body[key], f"{key} should not be empty"
        assert {d["value"] for d in body["departments"]} >= {"DEPT-01"}

    def test_bounds_are_ordered(self, client) -> None:
        body = client.get("/api/bounds").json()
        assert body["start_date"] <= body["end_date"]


class TestMetricRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/metrics/kpis",
            "/api/metrics/trend",
            "/api/metrics/status-mix",
            "/api/metrics/visit-type-mix",
            "/api/metrics/cancellation-reasons",
            "/api/metrics/patient-flow",
            "/api/metrics/wait-trend",
            "/api/metrics/departments",
        ],
    )
    def test_route_binds_against_the_real_schema(self, client, window, path) -> None:
        """The regression guard: every route must execute against the store."""
        assert client.get(path + window).status_code == 200

    def test_kpi_set_is_complete(self, client, window) -> None:
        body = client.get("/api/metrics/kpis" + window).json()
        keys = {metric["key"] for metric in body["metrics"]}
        assert keys == {
            "appointments_scheduled",
            "completed_visits",
            "cancellation_rate",
            "no_show_rate",
            "median_wait_minutes",
            "average_wait_minutes",
            "average_visit_duration_minutes",
            "appointment_lead_time_days",
            "capacity_utilisation",
            "lost_slot_rate",
        }

    def test_every_kpi_carries_its_definition(self, client, window) -> None:
        body = client.get("/api/metrics/kpis" + window).json()
        for metric in body["metrics"]:
            assert metric["definition"], f"{metric['key']} has no definition"

    def test_rate_metrics_expose_numerator_and_denominator(self, client, window) -> None:
        body = client.get("/api/metrics/kpis" + window).json()
        for metric in body["metrics"]:
            if metric["unit"] != "percent":
                continue
            assert metric["numerator"] is not None
            assert metric["denominator"] is not None
            assert metric["denominator"] > 0

    def test_rates_are_consistent_with_their_own_parts(self, client, window) -> None:
        """value must equal numerator/denominator, not a re-derived average."""
        body = client.get("/api/metrics/kpis" + window).json()
        for metric in body["metrics"]:
            if metric["unit"] != "percent" or not metric["available"]:
                continue
            expected = metric["numerator"] / metric["denominator"] * 100.0
            assert metric["value"] == pytest.approx(expected, abs=1e-9)

    def test_lost_slot_rate_never_exceeds_its_components(self, client, window) -> None:
        """The denominator is shared, so the roll-up cannot exceed the sum."""
        by_key = {
            m["key"]: m for m in client.get("/api/metrics/kpis" + window).json()["metrics"]
        }
        ceiling = by_key["cancellation_rate"]["value"] + by_key["no_show_rate"]["value"]
        assert by_key["lost_slot_rate"]["value"] <= ceiling + 1e-9

    def test_comparison_window_is_the_preceding_period(self, client, window) -> None:
        body = client.get("/api/metrics/kpis" + window).json()
        assert body["filters"]["comparison_end_date"] < body["filters"]["start_date"]

    def test_comparison_can_be_disabled(self, client, window) -> None:
        body = client.get("/api/metrics/kpis" + window + "&include_comparison=false").json()
        assert all(m["change_percent"] is None for m in body["metrics"])

    def test_department_ranking_guard_flags_small_samples(self, client, window) -> None:
        """A one-day window cannot reach the terminal-sample threshold."""
        body = client.get(
            "/api/metrics/departments?start_date=2026-10-10&end_date=2026-10-10"
        ).json()
        assert body["items"], "expected departments even in a one-day window"
        for item in body["items"]:
            if not item["rankable"]:
                assert item["rankable_reason"], "an unrankable row must say why"

    def test_trend_spine_includes_zero_activity_days(self, client) -> None:
        """A month with no data should still produce buckets, not gaps."""
        body = client.get(
            "/api/metrics/trend?start_date=2026-10-01&end_date=2026-10-10&granularity=day"
        ).json()
        assert len(body["points"]) == 10

    def test_granularity_is_validated(self, client, window) -> None:
        assert client.get("/api/metrics/trend" + window + "&granularity=hourly").status_code == 422


class TestFilterErrorPaths:
    """These returned 500 before the status/parameter shadowing fix."""

    def test_inverted_range_returns_422_not_500(self, client) -> None:
        response = client.get("/api/metrics/kpis?start_date=2026-10-10&end_date=2026-01-01")
        assert response.status_code == 422
        assert "must not be after" in response.json()["detail"]

    def test_oversized_window_returns_422_not_500(self, client) -> None:
        response = client.get("/api/metrics/kpis?start_date=2000-01-01&end_date=2026-10-10")
        assert response.status_code == 422
        assert "maximum supported" in response.json()["detail"]

    def test_malformed_date_returns_422(self, client) -> None:
        assert client.get("/api/metrics/kpis?start_date=not-a-date").status_code == 422

    def test_status_filter_still_works_after_the_rename(self, client, window) -> None:
        """The parameter is still called `status`; only the module was aliased."""
        response = client.get(window + "&status=cancelled")
        assert response.status_code == 200

    def test_filter_selection_echoes_back(self, client) -> None:
        body = client.get(
            "/api/metrics/kpis?start_date=2026-07-13&end_date=2026-10-10&department_id=DEPT-01"
        ).json()
        assert body["filters"]["department_ids"] == ["DEPT-01"]


class TestQualityRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/quality/runs",
            "/api/quality/latest",
            "/api/quality/issues",
            "/api/quality/issue-categories",
            "/api/quality/row-counts",
        ],
    )
    def test_route_responds(self, client, path) -> None:
        assert client.get(path).status_code == 200

    def test_latest_run_matches_the_first_of_the_history(self, client) -> None:
        latest = client.get("/api/quality/latest").json()
        history = client.get("/api/quality/runs").json()["items"]
        assert latest["run_id"] == history[0]["run_id"]

    def test_row_counts_cover_every_table(self, client) -> None:
        items = client.get("/api/quality/row-counts").json()["items"]
        assert len(items) >= 5
        assert all(item["row_count"] >= 0 for item in items)


class TestExports:
    def _rows(self, response) -> list[list[str]]:
        return list(csv.reader(io.StringIO(response.text)))

    def test_kpi_export_has_one_row_per_metric(self, client, window) -> None:
        response = client.get("/api/export/kpis.csv" + window)
        rows = self._rows(response)
        assert rows[0][0] == "key"
        assert len(rows) - 1 == 10

    def test_export_carries_its_filter_context(self, client, window) -> None:
        response = client.get("/api/export/kpis.csv" + window)
        assert "start_date" in response.headers["x-export-filters"]

    def test_export_sets_a_download_filename(self, client, window) -> None:
        response = client.get("/api/export/departments.csv" + window)
        assert "attachment" in response.headers["content-disposition"]
        assert "2026-07-13" in response.headers["content-disposition"]

    @pytest.mark.parametrize("table", ["appointments", "encounters", "capacity", "flow"])
    def test_mart_export_is_filtered(self, client, window, table) -> None:
        rows = self._rows(client.get(f"/api/export/marts/{table}.csv" + window))
        assert len(rows) > 1
        date_column = rows[0].index("metric_date")
        dates = [row[date_column] for row in rows[1:]]
        assert min(dates) >= "2026-07-13"
        assert max(dates) <= "2026-10-10"

    def test_unknown_table_is_404_and_lists_valid_options(self, client) -> None:
        response = client.get("/api/export/marts/nope.csv")
        assert response.status_code == 404
        assert "appointments" in response.json()["detail"]

    def test_table_parameter_cannot_inject_sql(self, client) -> None:
        """The parameter is resolved against an allow-list, never interpolated."""
        response = client.get("/api/export/marts/core.fct_appointments.csv")
        assert response.status_code == 404


class TestAdminGating:
    def test_refresh_is_disabled_by_default(self, client) -> None:
        response = client.post("/api/admin/refresh")
        assert response.status_code == 403
        assert "HOA_ADMIN_ENABLED" in response.json()["detail"]


class TestErrorHygiene:
    def test_internal_errors_do_not_leak_detail_by_default(self) -> None:
        """A 500 body must not carry a stack trace or a SQL string."""
        from fastapi.testclient import TestClient

        from app.api.app import create_app

        app = create_app()

        @app.get("/api/_boom")
        def boom() -> None:
            raise RuntimeError("SELECT secret FROM internal.table")

        response = TestClient(app, raise_server_exceptions=False).get("/api/_boom")
        assert response.status_code == 500
        assert "SELECT secret" not in response.text
        assert "Traceback" not in response.text
