from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.analysis import analyze_backfill
from app.schemas import AnalyzeRequest


def record(
    id: str,
    manager: str,
    start: str,
    end: str | None,
    registered: str,
    customer: str = "C1",
) -> dict:
    return {
        "id": id,
        "customer": customer,
        "manager": manager,
        "effective_start": start,
        "effective_end": end,
        "registered_at": registered,
    }


def sale(id: str, sale_date: str, customer: str = "C1") -> dict:
    return {"id": id, "customer": customer, "sale_date": sale_date}


def analyze(records: list[dict], sales: list[dict], before: str, after: str):
    return analyze_backfill(
        AnalyzeRequest.model_validate(
            {
                "attribution_records": records,
                "sales": sales,
                "settlement_points": [before, after],
            }
        )
    )


def result_for(point, sale_id: str):
    return next(item for item in point.sales if item.sale_id == sale_id)


def test_retroactive_correction_only_overrides_overlap_and_cutoff_is_inclusive():
    response = analyze(
        records=[
            record("R-OLD", "M1", "2024-01-01", "2024-12-31", "2024-01-01T00:00:00Z"),
            record("R-NEW", "M2", "2024-06-01", "2024-09-01", "2024-07-01T00:00:00Z"),
        ],
        sales=[
            sale("S-JAN", "2024-02-10"),
            sale("S-JUN", "2024-06-01"),
            sale("S-SEP", "2024-09-01"),
        ],
        before="2024-06-30T23:59:59Z",
        after="2024-07-01T00:00:00Z",  # inclusive: R-NEW is visible at exactly this instant
    )

    before, after = response.settlement_points
    assert result_for(before, "S-JAN").status == "assigned"
    assert result_for(before, "S-JAN").manager == "M1"
    assert result_for(before, "S-JUN").manager == "M1"
    assert result_for(before, "S-SEP").manager == "M1"

    # The correction overrides only its own overlap (June 1 through August 31).
    assert result_for(after, "S-JAN").manager == "M1"
    assert result_for(after, "S-JUN").manager == "M2"
    # effective_end is exclusive: at 2024-09-01 the old record is visible again.
    assert result_for(after, "S-SEP").manager == "M1"
    assert [change.sale_id for change in response.changes] == ["S-JUN"]
    assert response.changes[0].before.manager == "M1"
    assert response.changes[0].after.manager == "M2"
    assert response.changes[0].before.source_record_ids == ["R-OLD"]
    assert response.changes[0].after.source_record_ids == ["R-NEW"]


def test_record_registered_after_settlement_is_never_visible_at_that_point():
    response = analyze(
        records=[
            record("R1", "M1", "2024-01-01", None, "2024-01-01T00:00:00Z"),
            record("R2", "M2", "2024-01-01", None, "2024-08-01T00:00:00Z"),
        ],
        sales=[sale("S1", "2024-03-01")],
        before="2024-07-01T00:00:00Z",
        after="2024-08-01T00:00:00Z",
    )
    before, after = response.settlement_points
    assert result_for(before, "S1").manager == "M1"
    assert result_for(after, "S1").manager == "M2"
    assert response.changes[0].change_type == "manager_changed"
    assert response.changes[0].after.source_record_ids == ["R2"]


def test_same_latest_registration_with_different_managers_is_conflict_only_on_overlap():
    response = analyze(
        records=[
            record("A", "M1", "2024-01-01", "2024-12-31", "2024-01-01T00:00:00Z"),
            record("B", "M2", "2024-03-01", "2024-08-01", "2024-06-01T00:00:00Z"),
            record("C", "M3", "2024-05-01", "2024-10-01", "2024-06-01T00:00:00Z"),
        ],
        sales=[
            sale("BEFORE-B", "2024-02-01"),
            sale("OVERLAP", "2024-06-01"),
            sale("C-ONLY", "2024-09-01"),
        ],
        before="2024-06-01T00:00:00Z",
        after="2024-07-01T00:00:00Z",
    )
    after = response.settlement_points[1]
    assert result_for(after, "BEFORE-B").manager == "M1"
    assert result_for(after, "OVERLAP").status == "pending_verification"
    assert result_for(after, "OVERLAP").reason == "conflict"
    assert result_for(after, "OVERLAP").conflicting_managers == ["M2", "M3"]
    assert result_for(after, "OVERLAP").source_record_ids == ["B", "C"]
    assert result_for(after, "C-ONLY").manager == "M3"

    segments = after.attribution_segments
    conflict = next(segment for segment in segments if segment.status == "pending_verification")
    assert (conflict.effective_start, conflict.effective_end) == (date(2024, 5, 1), date(2024, 8, 1))
    assert conflict.source_record_ids == ["B", "C"]


def test_same_registration_and_same_manager_is_not_conflict():
    response = analyze(
        records=[
            record("A1", "M1", "2024-01-01", "2024-12-31", "2024-01-01T00:00:00Z"),
            record("A2", "M1", "2024-06-01", "2024-09-01", "2024-01-01T00:00:00Z"),
        ],
        sales=[sale("S1", "2024-07-01")],
        before="2024-03-01T00:00:00Z",
        after="2024-03-01T00:00:00Z",
    )
    result = result_for(response.settlement_points[0], "S1")
    assert result.status == "assigned"
    assert result.manager == "M1"
    assert result.source_record_ids == ["A1", "A2"]


def test_uncovered_sales_are_pending_and_can_become_assigned():
    response = analyze(
        records=[
            record("R1", "M1", "2024-01-01", "2024-06-01", "2024-01-01T00:00:00Z"),
            record("R2", "M2", "2024-06-01", "2024-12-01", "2024-06-02T00:00:00Z"),
        ],
        sales=[sale("S1", "2024-07-01")],
        before="2024-06-01T00:00:00Z",
        after="2024-07-01T00:00:00Z",
    )
    before, after = response.settlement_points
    assert result_for(before, "S1").status == "pending_verification"
    assert result_for(before, "S1").reason == "uncovered"
    assert result_for(before, "S1").source_record_ids == []
    assert result_for(after, "S1").status == "assigned"
    assert response.changes[0].change_type == "pending_to_assigned"
    assert response.changes[0].should_reassign is True


def test_sales_with_no_customer_records_are_pending():
    response = analyze(
        records=[],
        sales=[sale("S1", "2024-07-01", "NO-RECORDS")],
        before="2024-01-01T00:00:00Z",
        after="2025-01-01T00:00:00Z",
    )
    for point in response.settlement_points:
        assert result_for(point, "S1").status == "pending_verification"
        assert result_for(point, "S1").reason == "uncovered"
    assert response.changes == []


def test_equal_settlement_points_produce_no_changes():
    response = analyze(
        records=[record("R1", "M1", "2024-01-01", None, "2024-01-01T00:00:00Z")],
        sales=[sale("S1", "2024-05-01")],
        before="2024-07-01T00:00:00Z",
        after="2024-07-01T00:00:00Z",
    )
    assert response.settlement_points[0].sales[0].manager == "M1"
    assert response.settlement_points[1].sales[0].manager == "M1"
    assert response.changes == []
    assert response.summary.changed_sales_count == 0


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate record ids"):
        AnalyzeRequest.model_validate(
            {
                "attribution_records": [
                    record("A", "M1", "2024-01-01", None, "2024-01-01T00:00:00Z"),
                    record("A", "M2", "2024-01-01", None, "2024-02-01T00:00:00Z"),
                ],
                "sales": [],
                "settlement_points": ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z"],
            }
        )


def test_invalid_effective_interval_is_rejected():
    with pytest.raises(ValidationError, match="effective_end must be later"):
        AnalyzeRequest.model_validate(
            {
                "attribution_records": [
                    record("A", "M1", "2024-01-01", "2024-01-01", "2024-01-01T00:00:00Z")
                ],
                "sales": [],
                "settlement_points": ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z"],
            }
        )


def test_naive_timestamps_are_treated_as_utc():
    request = AnalyzeRequest.model_validate(
        {
            "attribution_records": [
                record("A", "M1", "2024-01-01", None, "2024-01-01T00:00:00")
            ],
            "sales": [sale("S1", "2024-01-02")],
            "settlement_points": ["2024-02-01T00:00:00", "2024-03-01T00:00:00"],
        }
    )
    assert request.attribution_records[0].registered_at.tzinfo == timezone.utc
    assert request.settlement_points[0].tzinfo == timezone.utc


def test_common_erp_field_aliases_are_accepted_but_unknown_fields_are_rejected():
    response = analyze(
        records=[
            {
                "record_id": "R1",
                "customer_id": "C1",
                "manager_id": "M1",
                "start_date": "2024-01-01",
                "end_date": None,
                "registration_time": "2024-01-01T00:00:00Z",
            }
        ],
        sales=[{"sale_id": "S1", "customer_id": "C1", "sales_date": "2024-02-01"}],
        before="2024-03-01T00:00:00Z",
        after="2024-03-01T00:00:00Z",
    )
    assert response.settlement_points[0].sales[0].manager == "M1"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnalyzeRequest.model_validate(
            {
                "attribution_records": [],
                "sales": [],
                "settlement_points": ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z"],
                "unexpected_export_column": "ignored?",
            }
        )
