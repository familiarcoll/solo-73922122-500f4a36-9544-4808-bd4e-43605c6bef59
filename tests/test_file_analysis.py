from __future__ import annotations

import gzip
import io
import json
import os
import random
from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.file_analysis as file_analysis
from app.analysis import analyze_backfill
from app.main import app
from app.sample import SAMPLE_REQUEST
from app.schemas import AnalyzeRequest

client = TestClient(app)

BEFORE = "2024-05-01T00:00:00Z"
AFTER = "2024-07-01T00:00:00Z"
URL = "/api/v1/backfill/analyze-files"

# Shuffled (unordered) row order of the built-in JSON sample.
RECORD_ORDER = (2, 0, 3, 1)
SALE_ORDER = (3, 1, 5, 0, 4, 2)


def make_gz(rows=(), raw_lines=()) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as stream:
        for row in rows:
            stream.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
        for line in raw_lines:
            stream.write(line if isinstance(line, bytes) else line.encode("utf-8"))
    return buf.getvalue()


def sample_files() -> tuple[bytes, bytes]:
    records = [SAMPLE_REQUEST["attribution_records"][i] for i in RECORD_ORDER]
    sales = [SAMPLE_REQUEST["sales"][i] for i in SALE_ORDER]
    return make_gz(records), make_gz(sales)


def post_files(
    attribution_gz: bytes,
    sales_gz: bytes,
    before: str = BEFORE,
    after: str = AFTER,
    **kwargs,
):
    return client.post(
        URL,
        files={
            "attribution_file": ("attribution.jsonl.gz", attribution_gz, "application/gzip"),
            "sales_file": ("sales.jsonl.gz", sales_gz, "application/gzip"),
        },
        data={"settlement_point_before": before, "settlement_point_after": after},
        **kwargs,
    )


def expected_lines(records: list[dict], sales: list[dict], before: str, after: str):
    """Ground truth from the unchanged in-memory JSON analysis path."""
    response = analyze_backfill(
        AnalyzeRequest.model_validate(
            {
                "attribution_records": records,
                "sales": sales,
                "settlement_points": [before, after],
            }
        )
    )
    before_by_id = {sale.sale_id: sale for sale in response.settlement_points[0].sales}
    after_by_id = {sale.sale_id: sale for sale in response.settlement_points[1].sales}
    changes_by_id = {change.sale_id: change for change in response.changes}
    return before_by_id, after_by_id, changes_by_id


def assert_lines_match_json_api(lines, records, sales, before, after):
    before_by_id, after_by_id, changes_by_id = expected_lines(records, sales, before, after)
    assert len(lines) == len(sales)
    for line in lines:
        sale_id = line["sale_id"]
        before_result = before_by_id[sale_id]
        after_result = after_by_id[sale_id]
        assert line["before"] == {
            "status": before_result.status,
            "manager": before_result.manager,
            "source_record_ids": before_result.source_record_ids,
        }
        assert line["after"] == {
            "status": after_result.status,
            "manager": after_result.manager,
            "source_record_ids": after_result.source_record_ids,
        }
        change = changes_by_id.get(sale_id)
        assert line["requires_backfill"] == (change is not None)
        assert line["should_reassign"] == (change.should_reassign if change else False)
    return changes_by_id


def test_unordered_sample_matches_json_api_and_keeps_sales_file_order():
    attribution_gz, sales_gz = sample_files()
    response = post_files(attribution_gz, sales_gz)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in response.headers["content-disposition"]

    lines = [json.loads(line) for line in response.text.splitlines()]
    expected_order = [SAMPLE_REQUEST["sales"][i]["id"] for i in SALE_ORDER]
    assert [line["sale_id"] for line in lines] == expected_order
    assert response.headers["x-total-sales"] == str(len(SAMPLE_REQUEST["sales"]))

    records = SAMPLE_REQUEST["attribution_records"]
    sales = SAMPLE_REQUEST["sales"]
    changes_by_id = assert_lines_match_json_api(lines, records, sales, BEFORE, AFTER)
    assert int(response.headers["x-requires-backfill-sales"]) == len(changes_by_id) == 4


def test_randomized_differential_against_json_api():
    rng = random.Random(20261111)
    # Few distinct registration instants -> ties and conflicts are exercised.
    instants = [
        f"2024-0{month}-15T0{hour}:00:00Z"
        for month in (1, 3, 5, 7, 9)
        for hour in range(2)
    ]
    records = []
    for index in range(150):
        end = None
        if rng.random() >= 0.3:
            end = date(2024, rng.randint(10, 12), rng.randint(1, 28)).isoformat()
        records.append(
            {
                "id": f"R{index:03d}",
                "customer": f"C{rng.randint(0, 24):02d}",
                "manager": f"M{rng.randint(0, 7)}",
                "effective_start": date(2024, rng.randint(1, 9), rng.randint(1, 28)).isoformat(),
                "effective_end": end,
                "registered_at": rng.choice(instants),
            }
        )
    sales = [
        {
            "id": f"S{index:03d}",
            # C25/C26 have no attribution records at all.
            "customer": f"C{rng.randint(0, 26):02d}",
            "sale_date": date(2024, rng.randint(1, 12), rng.randint(1, 28)).isoformat(),
        }
        for index in range(400)
    ]
    rng.shuffle(records)
    rng.shuffle(sales)
    before, after = "2024-04-01T00:00:00Z", "2024-08-01T00:00:00Z"

    response = post_files(make_gz(records), make_gz(sales), before, after)
    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert [line["sale_id"] for line in lines] == [sale["id"] for sale in sales]
    assert_lines_match_json_api(lines, records, sales, before, after)


def test_erp_field_aliases_are_accepted_in_files():
    records = [
        {
            "record_id": "R1",
            "customer_id": "C1",
            "manager_id": "M1",
            "start_date": "2024-01-01",
            "end_date": None,
            "registration_time": "2024-01-01T00:00:00Z",
        }
    ]
    sales = [{"sale_id": "S1", "customer_id": "C1", "sales_date": "2024-02-01"}]
    response = post_files(make_gz(records), make_gz(sales))
    assert response.status_code == 200
    (line,) = [json.loads(line) for line in response.text.splitlines()]
    assert line["before"]["manager"] == "M1"
    assert line["after"]["manager"] == "M1"
    assert line["requires_backfill"] is False


def test_empty_files_produce_empty_result():
    response = post_files(make_gz(), make_gz())
    assert response.status_code == 200
    assert response.text == ""
    assert response.headers["x-total-sales"] == "0"
    assert response.headers["x-requires-backfill-sales"] == "0"


def test_sales_without_records_are_pending_at_both_points():
    sales = [{"id": "S1", "customer": "NO-RECORDS", "sale_date": "2024-06-01"}]
    response = post_files(make_gz(), make_gz(sales))
    assert response.status_code == 200
    (line,) = [json.loads(line) for line in response.text.splitlines()]
    assert line["before"]["status"] == "pending_verification"
    assert line["after"]["status"] == "pending_verification"
    assert line["requires_backfill"] is False


def test_naive_settlement_points_are_treated_as_utc():
    attribution_gz, sales_gz = sample_files()
    response = post_files(attribution_gz, sales_gz, "2024-05-01T00:00:00", "2024-07-01T00:00:00")
    assert response.status_code == 200
    assert response.headers["x-total-sales"] == str(len(SAMPLE_REQUEST["sales"]))


def test_duplicate_record_ids_are_rejected():
    records = [
        {
            "id": "R1",
            "customer": "C1",
            "manager": "M1",
            "effective_start": "2024-01-01",
            "effective_end": None,
            "registered_at": "2024-01-01T00:00:00Z",
        },
        {
            "id": "R1",
            "customer": "C2",
            "manager": "M2",
            "effective_start": "2024-01-01",
            "effective_end": None,
            "registered_at": "2024-01-01T00:00:00Z",
        },
    ]
    response = post_files(make_gz(records), make_gz())
    assert response.status_code == 422
    assert "R1" in response.json()["detail"]


def test_duplicate_sale_ids_are_rejected():
    sales = [
        {"id": "S1", "customer": "C1", "sale_date": "2024-01-01"},
        {"id": "S1", "customer": "C2", "sale_date": "2024-01-02"},
    ]
    response = post_files(make_gz(), make_gz(sales))
    assert response.status_code == 422
    assert "S1" in response.json()["detail"]


def test_oversized_line_is_rejected():
    prefix = '{"id":"S1","customer":"'
    suffix = '","sale_date":"2024-01-01"}'
    padding = "x" * (file_analysis.MAX_LINE_BYTES - len(prefix) - len(suffix) + 1)
    response = post_files(make_gz(), make_gz(raw_lines=[prefix + padding + suffix + "\n"]))
    assert response.status_code == 400
    assert "16" in response.json()["detail"] or "KiB" in response.json()["detail"]


def test_max_size_line_is_accepted():
    prefix = '{"id":"S1","customer":"'
    suffix = '","sale_date":"2024-01-01"}'
    padding = "x" * (file_analysis.MAX_LINE_BYTES - len(prefix) - len(suffix))
    response = post_files(make_gz(), make_gz(raw_lines=[prefix + padding + suffix + "\n"]))
    assert response.status_code == 200
    assert response.headers["x-total-sales"] == "1"


def test_too_many_lines_are_rejected(monkeypatch):
    monkeypatch.setattr(file_analysis, "MAX_LINES_PER_FILE", 3)
    sales = [
        {"id": f"S{index}", "customer": "C1", "sale_date": "2024-01-01"}
        for index in range(4)
    ]
    response = post_files(make_gz(), make_gz(sales))
    assert response.status_code == 400
    assert "lines" in response.json()["detail"]


def test_per_customer_row_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(file_analysis, "MAX_ROWS_PER_CUSTOMER_PER_FILE", 2)
    sales = [
        {"id": f"S{index}", "customer": "C1", "sale_date": "2024-01-01"}
        for index in range(3)
    ]
    response = post_files(make_gz(), make_gz(sales))
    assert response.status_code == 422
    assert "C1" in response.json()["detail"]


def test_per_customer_row_limit_allows_exact_limit(monkeypatch):
    monkeypatch.setattr(file_analysis, "MAX_ROWS_PER_CUSTOMER_PER_FILE", 2)
    sales = [
        {"id": f"S{index}", "customer": "C1", "sale_date": "2024-01-01"}
        for index in range(2)
    ]
    response = post_files(make_gz(), make_gz(sales))
    assert response.status_code == 200
    assert response.headers["x-total-sales"] == "2"


def test_non_gzip_upload_is_rejected():
    response = post_files(b"this is not gzip at all", make_gz())
    assert response.status_code == 400


def test_zero_byte_upload_is_rejected():
    # A valid gzip member with zero records is fine (see test_empty_files_*),
    # but a zero-byte file is not a gzip stream at all.
    response = post_files(b"", make_gz())
    assert response.status_code == 400
    assert "gzip" in response.json()["detail"]


def test_truncated_gzip_is_rejected():
    attribution_gz, sales_gz = sample_files()
    response = post_files(attribution_gz[:-10], sales_gz)
    assert response.status_code == 400


def test_corrupt_gzip_trailer_is_rejected():
    attribution_gz, sales_gz = sample_files()
    corrupted = attribution_gz[:-3] + bytes([attribution_gz[-3] ^ 0xFF]) + attribution_gz[-2:]
    response = post_files(corrupted, sales_gz)
    assert response.status_code == 400


def test_trailing_garbage_after_gzip_stream_is_rejected():
    attribution_gz, sales_gz = sample_files()
    response = post_files(attribution_gz + b"trailing-garbage", sales_gz)
    assert response.status_code == 400


def test_invalid_utf8_is_rejected():
    sales_gz = make_gz(raw_lines=[b'{"id":"S1","customer":"\xff\xfe","sale_date":"2024-01-01"}\n'])
    response = post_files(make_gz(), sales_gz)
    assert response.status_code == 400
    assert "UTF-8" in response.json()["detail"]


def test_invalid_json_line_is_rejected():
    response = post_files(make_gz(), make_gz(raw_lines=[b'{"id": "S1", broken\n']))
    assert response.status_code == 400
    assert "JSON" in response.json()["detail"]


def test_record_validation_failure_is_rejected():
    records = [
        {
            "id": "R1",
            "customer": "C1",
            "manager": "M1",
            "effective_start": "2024-05-01",
            "effective_end": "2024-05-01",  # empty interval
            "registered_at": "2024-01-01T00:00:00Z",
        }
    ]
    response = post_files(make_gz(records), make_gz())
    assert response.status_code == 422
    assert "effective_end" in response.json()["detail"]


def test_unknown_fields_are_rejected():
    sales = [{"id": "S1", "customer": "C1", "sale_date": "2024-01-01", "surprise": 1}]
    response = post_files(make_gz(), make_gz(sales))
    assert response.status_code == 422


def test_missing_form_fields_are_rejected():
    attribution_gz, sales_gz = sample_files()
    response = client.post(
        URL,
        files={
            "attribution_file": ("a.gz", attribution_gz, "application/gzip"),
            "sales_file": ("s.gz", sales_gz, "application/gzip"),
        },
    )
    assert response.status_code == 422


def test_invalid_settlement_point_is_rejected():
    attribution_gz, sales_gz = sample_files()
    response = post_files(attribution_gz, sales_gz, before="not-a-date")
    assert response.status_code == 422


def test_file_endpoint_is_documented_with_multipart_upload():
    spec = client.get("/openapi.json").json()
    operation = spec["paths"][URL]["post"]
    content = operation["requestBody"]["content"]
    assert "multipart/form-data" in content
    schema = content["multipart/form-data"]["schema"]
    if "$ref" in schema:
        component = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][component]
    properties = schema["properties"]
    assert properties["attribution_file"]["type"] == "string"
    assert properties["attribution_file"]["format"] == "binary"
    assert "sales_file" in properties
    assert "settlement_point_before" in properties
    assert "application/x-ndjson" in operation["responses"]["200"]["content"]


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKFILL_TMP_DIR", str(tmp_path))
    return tmp_path


def test_tmp_dir_is_cleaned_after_success(tmp_root):
    attribution_gz, sales_gz = sample_files()
    response = post_files(attribution_gz, sales_gz)
    assert response.status_code == 200
    assert response.text  # the stream was fully consumed
    assert list(tmp_root.iterdir()) == []


def test_tmp_dir_is_cleaned_after_processing_failure(tmp_root):
    attribution_gz, sales_gz = sample_files()
    response = post_files(attribution_gz[:-10], sales_gz)
    assert response.status_code == 400
    assert list(tmp_root.iterdir()) == []


def test_tmp_dir_is_cleaned_when_client_disconnects(tmp_root):
    attribution_gz, sales_gz = sample_files()
    with client.stream(
        "POST",
        URL,
        files={
            "attribution_file": ("a.gz", attribution_gz, "application/gzip"),
            "sales_file": ("s.gz", sales_gz, "application/gzip"),
        },
        data={"settlement_point_before": BEFORE, "settlement_point_after": AFTER},
    ) as response:
        assert response.status_code == 200
        next(response.iter_lines())  # read one line, then abandon the download
    assert list(tmp_root.iterdir()) == []


def test_stream_generator_cleans_up_when_closed(tmp_path):
    import app.file_analysis as fa
    from datetime import datetime, timezone

    tmp_dir = fa.make_request_tmp_dir()
    try:
        db_path, total, _ = fa.run_file_analysis(
            attribution_stream=io.BytesIO(make_gz()),
            sales_stream=io.BytesIO(
                make_gz([{"id": "S1", "customer": "C1", "sale_date": "2024-01-01"}])
            ),
            before=datetime(2024, 2, 1, tzinfo=timezone.utc),
            after=datetime(2024, 3, 1, tzinfo=timezone.utc),
            tmp_dir=tmp_dir,
        )
        assert total == 1
        generator = fa.stream_results_and_cleanup(db_path, tmp_dir)
        assert json.loads(next(generator))["sale_id"] == "S1"
        generator.close()
        assert not os.path.exists(tmp_dir)
    finally:
        fa.cleanup_tmp_dir(tmp_dir)
