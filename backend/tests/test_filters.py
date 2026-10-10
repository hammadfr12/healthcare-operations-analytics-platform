"""Filter validation and predicate construction.

Two layers are tested separately, because they have different inputs:

* ``FilterSet`` is a plain frozen dataclass — pure logic, tested directly.
* ``get_filters`` is the FastAPI dependency, whose parameters default to
  ``Query(...)`` sentinels. It is only meaningful when FastAPI resolves it from
  a request, so its validation and defaults are exercised through the API
  client in ``test_api.py::TestFilterErrorPaths``. Here it is called only with
  every argument supplied explicitly.

The rejection paths are covered in detail because ``get_filters`` once crashed
with an AttributeError on every rejection — the ``status`` query parameter
shadows the ``status`` module import — so a bad date range returned a 500
instead of a 422.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.filters import DEFAULT_WINDOW_DAYS, MAX_WINDOW_DAYS, FilterSet, get_filters

START = date(2026, 7, 13)
END = date(2026, 10, 10)


def make_filter_set(**overrides) -> FilterSet:
    """A FilterSet with every field supplied, so no Query sentinel leaks in."""
    fields = {
        "start_date": START,
        "end_date": END,
        "department_ids": (),
        "visit_types": (),
        "statuses": (),
        "facility_ids": (),
    }
    fields.update(overrides)
    return FilterSet(**fields)


class TestValidation:
    def test_inverted_range_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            get_filters(start_date=date(2026, 10, 10), end_date=date(2026, 1, 1))
        assert exc.value.status_code == 422

    def test_oversized_window_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            get_filters(start_date=date(2000, 1, 1), end_date=date(2026, 10, 10))
        assert exc.value.status_code == 422

    def test_window_at_the_limit_is_accepted(self) -> None:
        end = date(2026, 10, 10)
        start = end - timedelta(days=MAX_WINDOW_DAYS - 1)
        assert get_filters(start_date=start, end_date=end).window_days == MAX_WINDOW_DAYS

    def test_one_day_over_the_limit_is_rejected(self) -> None:
        end = date(2026, 10, 10)
        start = end - timedelta(days=MAX_WINDOW_DAYS)
        with pytest.raises(HTTPException):
            get_filters(start_date=start, end_date=end)

    def test_single_day_window_is_valid(self) -> None:
        day = date(2026, 10, 10)
        assert get_filters(start_date=day, end_date=day).window_days == 1


class TestComparisonWindow:
    def test_comparison_is_the_preceding_equal_length_window(self) -> None:
        filters = make_filter_set(start_date=date(2026, 4, 1), end_date=date(2026, 6, 30))
        assert filters.window_days == 91
        assert filters.comparison_end_date == date(2026, 3, 31)
        assert filters.comparison_start_date == date(2025, 12, 31)

    def test_comparison_does_not_overlap_the_current_window(self) -> None:
        filters = make_filter_set(start_date=date(2026, 4, 1), end_date=date(2026, 6, 30))
        assert filters.comparison_end_date < filters.start_date

    def test_comparison_length_matches_the_current_window(self) -> None:
        filters = make_filter_set(start_date=date(2026, 4, 1), end_date=date(2026, 6, 30))
        comparison_days = (filters.comparison_end_date - filters.comparison_start_date).days + 1
        assert comparison_days == filters.window_days


class TestDimensionCleaning:
    def test_blanks_and_duplicates_are_dropped_order_preserved(self) -> None:
        filters = get_filters(
            start_date=START,
            end_date=END,
            department_id=["DEPT-02", "  ", "DEPT-01", "DEPT-02"],
        )
        assert filters.department_ids == ("DEPT-02", "DEPT-01")

    def test_whitespace_is_stripped(self) -> None:
        filters = get_filters(start_date=START, end_date=END, visit_type=["  new  "])
        assert filters.visit_types == ("new",)

    def test_none_selection_becomes_an_empty_tuple(self) -> None:
        filters = get_filters(start_date=START, end_date=END, department_id=None)
        assert filters.department_ids == ()


class TestPredicates:
    def test_every_value_is_a_bound_parameter(self) -> None:
        """No caller-supplied value may appear in the SQL text itself."""
        hostile = "DEPT-01'; DROP TABLE core.fct_appointments; --"
        filters = make_filter_set(department_ids=(hostile,))
        where, params = filters.appointment_predicates()

        assert hostile not in where
        assert hostile in params
        assert where.count("?") == len(params)

    def test_fact_appointment_predicates_date_on_appointment_date(self) -> None:
        """fct_appointments has no metric_date column; using the mart builder
        there is a binder error, so the column name is asserted explicitly."""
        where, _ = make_filter_set().fact_appointment_predicates("a")
        assert "a.appointment_date" in where
        assert "metric_date" not in where

    def test_fact_encounter_predicates_date_on_encounter_date(self) -> None:
        where, _ = make_filter_set().fact_encounter_predicates("e")
        assert "e.encounter_date" in where
        assert "metric_date" not in where

    def test_mart_predicates_date_on_metric_date(self) -> None:
        where, _ = make_filter_set().appointment_predicates("m")
        assert "m.metric_date" in where

    def test_alias_is_applied_to_dimension_predicates(self) -> None:
        where, _ = make_filter_set(department_ids=("DEPT-01",)).appointment_predicates("m")
        assert "m.department_id IN (?)" in where

    def test_no_dimension_selection_means_no_dimension_clause(self) -> None:
        where, params = make_filter_set().appointment_predicates()
        assert "department_id" not in where
        assert params == [START, END]

    def test_placeholder_count_matches_parameter_count(self) -> None:
        filters = make_filter_set(
            department_ids=("DEPT-01", "DEPT-02"),
            visit_types=("new",),
            statuses=("cancelled",),
            facility_ids=("FAC-01",),
        )
        where, params = filters.appointment_predicates()
        assert where.count("?") == len(params)

    def test_encounter_and_appointment_predicates_agree(self) -> None:
        """Both marts carry the same filterable dimensions by design."""
        filters = make_filter_set(department_ids=("DEPT-01",), visit_types=("new",))
        assert filters.appointment_predicates() == filters.encounter_predicates()

    def test_serialisable_form_round_trips_dates_as_strings(self) -> None:
        body = make_filter_set().as_dict
        assert body["start_date"] == "2026-07-13"
        assert body["end_date"] == "2026-10-10"
        assert body["comparison_end_date"] == "2026-07-12"


class TestDefaults:
    def test_default_window_length_is_documented(self) -> None:
        assert DEFAULT_WINDOW_DAYS == 90

    def test_max_window_is_documented(self) -> None:
        assert MAX_WINDOW_DAYS == 1200
