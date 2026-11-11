from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    AttributionRecord,
    AttributionSegment,
    SaleAttribution,
    SaleChange,
    SettlementResult,
    Summary,
    Totals,
)


@dataclass(frozen=True)
class Decision:
    status: str
    manager: str | None
    reason: str | None
    conflicting_managers: tuple[str, ...]
    sources: tuple[AttributionRecord, ...]

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.status,
            self.manager,
            self.reason,
            self.conflicting_managers,
            tuple(record.id for record in self.sources),
        )


@dataclass(frozen=True)
class TimelineSegment:
    customer: str
    effective_start: date
    effective_end: date | None
    decision: Decision


def _ordered_sources(records: list[AttributionRecord]) -> tuple[AttributionRecord, ...]:
    """Newest registration first; record ID gives deterministic tie-breaking."""
    by_id = sorted(records, key=lambda record: record.id)
    return tuple(sorted(by_id, key=lambda record: record.registered_at, reverse=True))


def _select_decision(active: list[AttributionRecord]) -> Decision:
    """Select by latest registration time within an effective interval."""
    if not active:
        return Decision(
            status="pending_verification",
            manager=None,
            reason="uncovered",
            conflicting_managers=(),
            sources=(),
        )

    latest_time = max(record.registered_at for record in active)
    winners = [record for record in active if record.registered_at == latest_time]
    sources = _ordered_sources(winners)
    managers = sorted({record.manager for record in winners})

    # A tie at the latest registration instant is only a conflict if it names
    # more than one manager. Duplicate same-manager records are not a conflict.
    if len(managers) > 1:
        return Decision(
            status="pending_verification",
            manager=None,
            reason="conflict",
            conflicting_managers=tuple(managers),
            sources=sources,
        )

    return Decision(
        status="assigned",
        manager=managers[0],
        reason=None,
        conflicting_managers=(),
        sources=sources,
    )


def _active_at(records: list[AttributionRecord], point: date) -> list[AttributionRecord]:
    """Records whose [effective_start, effective_end) interval contains point."""
    return [
        record
        for record in records
        if record.effective_start <= point
        and (record.effective_end is None or point < record.effective_end)
    ]


def build_timeline_segments(
    customer: str, records: list[AttributionRecord]
) -> list[TimelineSegment]:
    """Build stable, non-overlapping segments from the layered ERP records.

    Every start/end boundary is considered. Within each resulting interval the
    record with the latest registration time wins; older records therefore
    remain visible outside a correction's overlapping range.
    """
    if not records:
        return []

    boundaries = sorted(
        {record.effective_start for record in records}
        | {record.effective_end for record in records if record.effective_end is not None}
    )
    if not boundaries:
        return []

    raw_intervals: list[tuple[date, date | None]] = [
        (boundaries[index], boundaries[index + 1])
        for index in range(len(boundaries) - 1)
        if boundaries[index] < boundaries[index + 1]
    ]

    # A segment after the final finite boundary exists only when an open-ended
    # record is still active there.
    last_boundary = boundaries[-1]
    if any(
        record.effective_end is None and record.effective_start <= last_boundary
        for record in records
    ):
        raw_intervals.append((last_boundary, None))

    segments: list[TimelineSegment] = []
    for interval_start, interval_end in raw_intervals:
        decision = _select_decision(_active_at(records, interval_start))
        segment = TimelineSegment(
            customer=customer,
            effective_start=interval_start,
            effective_end=interval_end,
            decision=decision,
        )
        if (
            segments
            and segments[-1].effective_end == segment.effective_start
            and segments[-1].decision.key == segment.decision.key
        ):
            previous = segments[-1]
            segments[-1] = TimelineSegment(
                customer=customer,
                effective_start=previous.effective_start,
                effective_end=segment.effective_end,
                decision=previous.decision,
            )
        else:
            segments.append(segment)

    return segments


def segment_for_date(
    segments: list[TimelineSegment], sale_date: date
) -> TimelineSegment | None:
    for segment in segments:
        if segment.effective_start <= sale_date and (
            segment.effective_end is None or sale_date < segment.effective_end
        ):
            return segment
    return None


def _sale_attribution(
    sale,
    settlement_point: datetime,
    segment: TimelineSegment | None,
) -> SaleAttribution:
    if segment is None:
        return SaleAttribution(
            sale_id=sale.id,
            customer=sale.customer,
            sale_date=sale.sale_date,
            settlement_point=settlement_point,
            status="pending_verification",
            manager=None,
            reason="uncovered",
            conflicting_managers=[],
            source_record_ids=[],
            source_records=[],
            segment_start=None,
            segment_end=None,
        )

    decision = segment.decision
    return SaleAttribution(
        sale_id=sale.id,
        customer=sale.customer,
        sale_date=sale.sale_date,
        settlement_point=settlement_point,
        status=decision.status,
        manager=decision.manager,
        reason=decision.reason,
        conflicting_managers=list(decision.conflicting_managers),
        source_record_ids=[record.id for record in decision.sources],
        source_records=list(decision.sources),
        segment_start=segment.effective_start,
        segment_end=segment.effective_end,
    )


def _compute_settlement_point(
    *,
    request: AnalyzeRequest,
    settlement_point: datetime,
    label: str,
    customers: list[str],
) -> SettlementResult:
    # The as-of rule: a record registered after this settlement point never
    # affects the result at this point, even if its effective dates are earlier.
    eligible_records = [
        record
        for record in request.attribution_records
        if record.registered_at <= settlement_point
    ]
    records_by_customer: dict[str, list[AttributionRecord]] = {}
    for record in eligible_records:
        records_by_customer.setdefault(record.customer, []).append(record)

    timelines: dict[str, list[TimelineSegment]] = {
        customer: build_timeline_segments(customer, records_by_customer.get(customer, []))
        for customer in customers
    }

    segments_with_sales: list[tuple[TimelineSegment, list[str]]] = [
        (segment, [])
        for customer in customers
        for segment in timelines[customer]
    ]

    sale_results: list[SaleAttribution] = []
    for sale in request.sales:
        customer_segments = timelines.get(sale.customer, [])
        segment = segment_for_date(customer_segments, sale.sale_date)
        result = _sale_attribution(sale, settlement_point, segment)
        sale_results.append(result)

        if segment is not None:
            # Sale IDs are unique (validated), so a linear lookup is unambiguous.
            for candidate, sale_ids in segments_with_sales:
                if candidate == segment:
                    sale_ids.append(sale.id)
                    break

    attribution_segments = [
        AttributionSegment(
            customer=segment.customer,
            effective_start=segment.effective_start,
            effective_end=segment.effective_end,
            status=segment.decision.status,
            manager=segment.decision.manager,
            reason=segment.decision.reason,
            conflicting_managers=list(segment.decision.conflicting_managers),
            source_record_ids=[record.id for record in segment.decision.sources],
            source_records=list(segment.decision.sources),
            sale_ids=sale_ids,
            sales_count=len(sale_ids),
        )
        for segment, sale_ids in segments_with_sales
    ]

    totals = Totals(
        sale_count=len(sale_results),
        assigned_count=sum(result.status == "assigned" for result in sale_results),
        pending_verification_count=sum(
            result.status == "pending_verification" for result in sale_results
        ),
        conflict_count=sum(result.reason == "conflict" for result in sale_results),
        uncovered_count=sum(result.reason == "uncovered" for result in sale_results),
        segment_count=len(attribution_segments),
    )

    return SettlementResult(
        label=label,
        settlement_point=settlement_point,
        attribution_segments=attribution_segments,
        sales=sale_results,
        totals=totals,
    )


def _change_type(before: SaleAttribution, after: SaleAttribution) -> str:
    if before.status == "assigned" and after.status == "assigned":
        return "manager_changed"
    if before.status == "pending_verification" and after.status == "assigned":
        return "pending_to_assigned"
    return "assigned_to_pending"


def analyze_backfill(request: AnalyzeRequest) -> AnalyzeResponse:
    # Customers are sorted so segment ordering is stable rather than dependent
    # on ERP export order.
    customers = sorted(
        {record.customer for record in request.attribution_records}
        | {sale.customer for sale in request.sales}
    )

    before_point, after_point = request.settlement_points
    before = _compute_settlement_point(
        request=request,
        settlement_point=before_point,
        label="before",
        customers=customers,
    )
    after = _compute_settlement_point(
        request=request,
        settlement_point=after_point,
        label="after",
        customers=customers,
    )

    before_by_sale = {result.sale_id: result for result in before.sales}
    changes: list[SaleChange] = []

    for sale in request.sales:
        before_result = before_by_sale[sale.id]
        after_result = next(
            result for result in after.sales if result.sale_id == sale.id
        )

        if (
            before_result.status == after_result.status
            and before_result.manager == after_result.manager
        ):
            continue

        changed_fields: list[str] = []
        if before_result.status != after_result.status:
            changed_fields.append("status")
        if before_result.manager != after_result.manager:
            changed_fields.append("manager")

        # A sale only has a manager to move to after the correction. When the
        # correction removes the assignment, it still needs a warehouse review
        # but cannot be auto-reassigned.
        should_reassign = after_result.status == "assigned" and (
            before_result.status != "assigned"
            or before_result.manager != after_result.manager
        )

        changes.append(
            SaleChange(
                sale_id=sale.id,
                customer=sale.customer,
                sale_date=sale.sale_date,
                change_type=_change_type(before_result, after_result),
                changed_fields=changed_fields,
                requires_backfill=True,
                should_reassign=should_reassign,
                before=before_result,
                after=after_result,
            )
        )

    should_reassign_ids = [change.sale_id for change in changes if change.should_reassign]
    backfill_ids = [change.sale_id for change in changes]

    return AnalyzeResponse(
        settlement_points=[before, after],
        changes=changes,
        summary=Summary(
            settlement_points=request.settlement_points,
            total_sales=len(request.sales),
            changed_sales_count=len(changes),
            should_reassign_count=len(should_reassign_ids),
            requires_backfill_count=len(backfill_ids),
            before_totals=before.totals,
            after_totals=after.totals,
            should_reassign_sale_ids=should_reassign_ids,
            requires_backfill_sale_ids=backfill_ids,
        ),
    )
