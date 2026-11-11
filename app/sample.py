"""A runnable sample payload used by the API docs and demo endpoint."""

SAMPLE_REQUEST = {
    "attribution_records": [
        {
            "id": "ATTR-001",
            "customer": "CUST-001",
            "manager": "M-ALPHA",
            "effective_start": "2024-01-01",
            "effective_end": "2024-07-01",
            "registered_at": "2024-01-02T08:00:00Z",
        },
        {
            "id": "ATTR-002",
            "customer": "CUST-001",
            "manager": "M-BETA",
            "effective_start": "2024-04-01",
            "effective_end": "2024-10-01",
            "registered_at": "2024-06-15T10:00:00Z",
        },
        {
            "id": "ATTR-003",
            "customer": "CUST-001",
            "manager": "M-GAMMA",
            "effective_start": "2024-05-01",
            "effective_end": "2024-06-01",
            "registered_at": "2024-06-15T10:00:00Z",
        },
        {
            "id": "ATTR-004",
            "customer": "CUST-001",
            "manager": "M-DELTA",
            "effective_start": "2024-06-01",
            "effective_end": "2024-12-31",
            "registered_at": "2024-08-01T00:00:00Z",
        },
    ],
    "sales": [
        {
            "id": "SALE-001",
            "customer": "CUST-001",
            "sale_date": "2024-02-10",
        },
        {
            "id": "SALE-002",
            "customer": "CUST-001",
            "sale_date": "2024-04-15",
        },
        {
            "id": "SALE-003",
            "customer": "CUST-001",
            "sale_date": "2024-05-20",
        },
        {
            "id": "SALE-004",
            "customer": "CUST-001",
            "sale_date": "2024-06-20",
        },
        {
            "id": "SALE-005",
            "customer": "CUST-001",
            "sale_date": "2024-09-15",
        },
        {
            "id": "SALE-006",
            "customer": "CUST-002",
            "sale_date": "2024-06-01",
        },
    ],
    "settlement_points": [
        "2024-05-01T00:00:00Z",
        "2024-07-01T00:00:00Z",
    ],
}
