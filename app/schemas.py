from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator


def utc_now_iso(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC instant."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_utc(value: datetime) -> datetime:
    """Treat naive ERP timestamps as UTC; normalise aware timestamps to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalise_date(value: object) -> object:
    """Accept a datetime where a date is expected (ERP exports often do this)."""
    if isinstance(value, datetime):
        return value.date()
    return value


def _apply_aliases(data: object, aliases: dict[str, tuple[str, ...]]) -> object:
    """Accept common ERP naming variants without advertising them in OpenAPI.

    The documented contract remains the canonical names below. Existing ERP
    exports often use e.g. ``record_id`` or ``sales_date``; accepting those
    keeps integration simple while still rejecting genuinely unknown fields.
    """
    if not isinstance(data, dict):
        return data

    # Fast path: nothing to rewrite when no alias is present (canonical exports).
    if not any(
        alternative in data
        for alternatives in aliases.values()
        for alternative in alternatives
    ):
        return data

    normalised = dict(data)
    for canonical, alternatives in aliases.items():
        if canonical not in normalised:
            for alternative in alternatives:
                if alternative in normalised:
                    normalised[canonical] = normalised[alternative]
                    break
        for alternative in alternatives:
            normalised.pop(alternative, None)
    return normalised


class AttributionRecord(BaseModel):
    """One ERP attribution (account-manager assignment) record."""

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, coerce_numbers_to_str=True
    )

    id: str = Field(min_length=1, description="Unique attribution record ID.")
    customer: str = Field(min_length=1, description="Customer / account ID.")
    manager: str = Field(min_length=1, description="Account manager ID.")
    effective_start: date = Field(description="Start of the effective interval (inclusive).")
    effective_end: date | None = Field(
        default=None,
        description="End of the effective interval (exclusive). Null means open-ended.",
    )
    registered_at: datetime = Field(description="UTC registration instant.")

    @model_validator(mode="before")
    @classmethod
    def accept_erp_field_names(cls, data: object) -> object:
        return _apply_aliases(
            data,
            {
                "id": ("record_id", "attribution_id"),
                "customer": ("customer_id", "account_id"),
                "manager": ("manager_id", "account_manager", "salesperson"),
                "effective_start": ("start_date", "effective_from", "valid_from"),
                "effective_end": ("end_date", "effective_to", "valid_to"),
                "registered_at": (
                    "registration_time",
                    "registered_at_utc",
                    "recorded_at",
                ),
            },
        )

    @field_validator("effective_start", "effective_end", mode="before")
    @classmethod
    def dates_from_datetime(cls, value: object) -> object:
        return _normalise_date(value)

    @field_validator("registered_at")
    @classmethod
    def registered_at_is_utc(cls, value: datetime) -> datetime:
        return _normalise_utc(value)

    @field_serializer("registered_at")
    def serialize_registered_at(self, value: datetime) -> str:
        return utc_now_iso(value)

    @model_validator(mode="after")
    def interval_is_non_empty(self) -> "AttributionRecord":
        if self.effective_end is not None and self.effective_end <= self.effective_start:
            raise ValueError("effective_end must be later than effective_start (left-closed, right-open interval)")
        return self


class Sale(BaseModel):
    """One row from the sales detail export."""

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, coerce_numbers_to_str=True
    )

    id: str = Field(min_length=1, description="Unique sales-detail ID.")
    customer: str = Field(min_length=1, description="Customer / account ID.")
    sale_date: date = Field(description="Business date of the sale.")

    @model_validator(mode="before")
    @classmethod
    def accept_erp_field_names(cls, data: object) -> object:
        return _apply_aliases(
            data,
            {
                "id": ("sale_id", "sales_id"),
                "customer": ("customer_id", "account_id"),
                "sale_date": ("sales_date", "date"),
            },
        )

    @field_validator("sale_date", mode="before")
    @classmethod
    def date_from_datetime(cls, value: object) -> object:
        return _normalise_date(value)


class AnalyzeRequest(BaseModel):
    """Request for a two-settlement-point retroactive attribution comparison."""

    model_config = ConfigDict(extra="forbid")

    attribution_records: list[AttributionRecord] = Field(
        description="ERP attribution records, including retroactively registered corrections."
    )
    sales: list[Sale] = Field(description="Sales detail rows to classify and compare.")
    settlement_points: list[datetime] = Field(
        min_length=2,
        max_length=2,
        description=(
            "Exactly two settlement points in chronological order [before, after]. "
            "At each point, only records with registered_at <= the point are used. "
            "Naive timestamps are interpreted as UTC."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_export_field_names(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        normalised = _apply_aliases(
            data,
            {
                "attribution_records": (
                    "assignments",
                    "attribution_history",
                    "records",
                ),
                "sales": ("sales_details", "sales_records", "sale_details"),
                "settlement_points": (
                    "settlement_times",
                    "settlements",
                    "as_of_times",
                ),
            },
        )
        points = normalised.get("settlement_points")
        if isinstance(points, dict):
            before = points.get("before", points.get("from", points.get("start")))
            after = points.get("after", points.get("to", points.get("end")))
            if before is not None and after is not None:
                normalised["settlement_points"] = [before, after]
        return normalised

    @field_validator("settlement_points")
    @classmethod
    def settlement_points_are_utc(cls, values: list[datetime]) -> list[datetime]:
        return [_normalise_utc(value) for value in values]

    @model_validator(mode="after")
    def unique_ids(self) -> "AnalyzeRequest":
        record_ids = [record.id for record in self.attribution_records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("attribution_records contains duplicate record ids")
        sale_ids = [sale.id for sale in self.sales]
        if len(sale_ids) != len(set(sale_ids)):
            raise ValueError("sales contains duplicate sale ids")
        return self


Status = Literal["assigned", "pending_verification"]
PendingReason = Literal["conflict", "uncovered"]
SettlementLabel = Literal["before", "after"]


class Totals(BaseModel):
    sale_count: int
    assigned_count: int
    pending_verification_count: int
    conflict_count: int
    uncovered_count: int
    segment_count: int


class AttributionSegment(BaseModel):
    """A half-open effective interval with one stable attribution result."""

    customer: str
    effective_start: date
    effective_end: date | None
    status: Status
    manager: str | None
    reason: PendingReason | None
    conflicting_managers: list[str] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    source_records: list[AttributionRecord] = Field(default_factory=list)
    sale_ids: list[str] = Field(default_factory=list)
    sales_count: int = 0


class SaleAttribution(BaseModel):
    """The result for one sale at one settlement point."""

    sale_id: str
    customer: str
    sale_date: date
    settlement_point: datetime
    status: Status
    manager: str | None
    reason: PendingReason | None
    conflicting_managers: list[str] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    source_records: list[AttributionRecord] = Field(default_factory=list)
    segment_start: date | None = None
    segment_end: date | None = None

    @field_serializer("settlement_point")
    def serialize_settlement_point(self, value: datetime) -> str:
        return utc_now_iso(value)


class SettlementResult(BaseModel):
    label: SettlementLabel
    settlement_point: datetime
    attribution_segments: list[AttributionSegment]
    sales: list[SaleAttribution]
    totals: Totals

    @field_serializer("settlement_point")
    def serialize_settlement_point(self, value: datetime) -> str:
        return utc_now_iso(value)


class SaleChange(BaseModel):
    """A sale whose manager assignment or pending-verification state changed."""

    sale_id: str
    customer: str
    sale_date: date
    change_type: Literal[
        "manager_changed",
        "pending_to_assigned",
        "assigned_to_pending",
    ]
    changed_fields: list[Literal["status", "manager"]]
    requires_backfill: bool = True
    should_reassign: bool
    before: SaleAttribution
    after: SaleAttribution


class Summary(BaseModel):
    settlement_points: list[datetime]
    total_sales: int
    changed_sales_count: int
    should_reassign_count: int
    requires_backfill_count: int
    before_totals: Totals
    after_totals: Totals
    should_reassign_sale_ids: list[str]
    requires_backfill_sale_ids: list[str]

    @field_serializer("settlement_points")
    def serialize_settlement_points(self, values: list[datetime]) -> list[str]:
        return [utc_now_iso(value) for value in values]


class AnalyzeResponse(BaseModel):
    settlement_points: list[SettlementResult]
    changes: list[SaleChange]
    summary: Summary


class BackfillPointResult(BaseModel):
    """One side (before/after) of a per-sale line in the JSONL file response."""

    status: Status
    manager: str | None
    source_record_ids: list[str] = Field(default_factory=list)


class BackfillResultLine(BaseModel):
    """One JSONL line of the large-file analysis attachment.

    Lines are emitted in the original order of the uploaded sales file.
    """

    sale_id: str
    before: BackfillPointResult
    after: BackfillPointResult
    requires_backfill: bool = Field(
        description="True when status or manager changed between the two settlement points."
    )
    should_reassign: bool = Field(
        description=(
            "True when the sale needs backfill and the 'after' point is assigned; "
            "sales that became pending_verification need manual verification first."
        )
    )
