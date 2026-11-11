from fastapi.testclient import TestClient

from app.main import app
from app.sample import SAMPLE_REQUEST

client = TestClient(app)


def test_health_and_service_entry_point():
    assert client.get("/health").json() == {"status": "ok"}
    body = client.get("/").json()
    assert body["docs"] == "/docs"


def test_docs_and_openapi_are_available():
    assert client.get("/docs").status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/api/v1/backfill/analyze" in openapi["paths"]
    request_example = openapi["paths"]["/api/v1/backfill/analyze"]["post"]["requestBody"][
        "content"
    ]["application/json"]["examples"]
    assert "retroactive_correction" in request_example
    assert "settlement_points" in request_example["retroactive_correction"]["value"]


def test_sample_request_endpoint_returns_valid_payload():
    response = client.get("/api/v1/backfill/sample-request")
    assert response.status_code == 200
    body = response.json()
    assert len(body["attribution_records"]) == len(SAMPLE_REQUEST["attribution_records"])
    assert len(body["settlement_points"]) == 2


def test_demo_endpoint_is_runnable_without_a_body():
    response = client.post("/api/v1/backfill/demo")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["changed_sales_count"] == 4
    assert body["changes"][0]["sale_id"] == "SALE-002"
    assert body["changes"][1]["after"]["reason"] == "conflict"


def test_analyze_endpoint_with_example_payload():
    response = client.post("/api/v1/backfill/analyze", json=SAMPLE_REQUEST)
    assert response.status_code == 200
    body = response.json()
    assert [point["label"] for point in body["settlement_points"]] == ["before", "after"]
    assert body["summary"]["should_reassign_sale_ids"] == [
        "SALE-002",
        "SALE-004",
        "SALE-005",
    ]


def test_analyze_endpoint_validation_error_has_readable_detail():
    payload = dict(SAMPLE_REQUEST)
    payload["settlement_points"] = [SAMPLE_REQUEST["settlement_points"][0]]
    response = client.post("/api/v1/backfill/analyze", json=payload)
    assert response.status_code == 422
    assert "at least 2" in str(response.json()["detail"]).lower()
