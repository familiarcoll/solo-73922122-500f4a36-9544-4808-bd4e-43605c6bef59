"""Streaming large-file backfill analysis backed by a temporary SQLite database.

Design constraints (see README):

* The uploaded gzip/JSONL streams are parsed line by line; neither the inputs,
  the ID sets, nor the results are ever loaded into memory in full.
* Per-file limits (lines per file, bytes per line, rows per customer) keep the
  per-customer working set bounded, so memory use does not grow with the
  number of customers.
* ID uniqueness and the per-customer row limit are enforced inside SQLite
  (unique index / GROUP BY), not with in-memory sets.
* Both gzip streams are read to EOF — which validates the gzip trailer — and
  every record is validated before any result is produced. Any failure
  aborts the request; no partial results are returned.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zlib
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from typing import BinaryIO, Iterator

from pydantic import ValidationError

from .analysis import build_timeline_segments, segment_for_date
from .schemas import AttributionRecord, Sale

MAX_LINES_PER_FILE = 1_000_000
MAX_LINE_BYTES = 16 * 1024
MAX_ROWS_PER_CUSTOMER_PER_FILE = 1000

_INSERT_BATCH_SIZE = 4096
_FETCH_BATCH_SIZE = 4096
_GZIP_BUFFER_BYTES = 256 * 1024

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_SCHEMA = """
CREATE TABLE attribution (
    id TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    manager TEXT NOT NULL,
    effective_start TEXT NOT NULL,
    effective_end TEXT,
    registered_at_us INTEGER NOT NULL
);
CREATE TABLE sales (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    customer TEXT NOT NULL,
    sale_date TEXT NOT NULL
);
CREATE TABLE results (
    seq INTEGER PRIMARY KEY,
    line TEXT NOT NULL
);
"""

_INDEXES = """
CREATE INDEX idx_attribution_customer ON attribution(customer);
CREATE INDEX idx_sales_customer ON sales(customer);
"""

_INSERT_SQL = {
    "attribution": (
        "INSERT INTO attribution"
        " (id, customer, manager, effective_start, effective_end, registered_at_us)"
        " VALUES (?, ?, ?, ?, ?, ?)"
    ),
    "sales": "INSERT INTO sales (seq, id, customer, sale_date) VALUES (?, ?, ?, ?)",
}

# Position of the record ID inside each insert tuple (for duplicate-ID errors).
_ID_POSITION = {"attribution": 0, "sales": 1}


class FileUploadError(Exception):
    """An uploaded file (or form field) cannot be accepted."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _PrependStream:
    """Push already-consumed bytes back in front of a binary stream."""

    def __init__(self, prefix: bytes, stream: BinaryIO) -> None:
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if not self._prefix:
            return self._stream.read(size)
        if size < 0:
            prefix, self._prefix = self._prefix, b""
            return prefix + self._stream.read()
        head, self._prefix = self._prefix[:size], self._prefix[size:]
        return head + self._stream.read(size - len(head))


def make_request_tmp_dir() -> str:
    """Create the per-request temporary directory (env-configurable location)."""
    root = os.environ.get("BACKFILL_TMP_DIR") or None
    return tempfile.mkdtemp(prefix="backfill-", dir=root)


def cleanup_tmp_dir(tmp_dir: str) -> None:
    """Idempotently remove the per-request temporary directory."""
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def _to_epoch_us(value: datetime) -> int:
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _from_epoch_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _iter_cursor(cursor: sqlite3.Cursor) -> Iterator[tuple]:
    while True:
        rows = cursor.fetchmany(_FETCH_BATCH_SIZE)
        if not rows:
            return
        yield from rows


def _iter_lines(stream: BinaryIO, file_label: str) -> Iterator[tuple[int, bytes]]:
    """Yield (line_number, content) without ever holding a whole file in memory.

    ``readline`` is size-capped so an over-long line is rejected without
    buffering it entirely. Reaching EOF also validates the gzip trailer.
    """
    line_no = 0
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 3)
        if raw == b"":
            return
        line_no += 1
        if line_no > MAX_LINES_PER_FILE:
            raise FileUploadError(
                400,
                f"{file_label}: more than {MAX_LINES_PER_FILE} lines",
            )
        content = raw
        if content.endswith(b"\n"):
            content = content[:-1]
        if content.endswith(b"\r"):
            content = content[:-1]
        if len(content) > MAX_LINE_BYTES:
            raise FileUploadError(
                400,
                f"{file_label}: line {line_no} is longer than"
                f" {MAX_LINE_BYTES} bytes (16 KiB)",
            )
        if not content.strip():
            continue
        yield line_no, content


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    remaining = exc.error_count() - len(parts)
    if remaining > 0:
        parts.append(f"(+{remaining} more)")
    return "; ".join(parts)


def _decode_and_validate(
    content: bytes, line_no: int, file_label: str, kind: str
) -> tuple:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileUploadError(
            400, f"{file_label}: line {line_no}: not valid UTF-8"
        ) from exc
    if line_no == 1:
        text = text.removeprefix("\ufeff")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FileUploadError(
            400, f"{file_label}: line {line_no}: invalid JSON ({exc.msg})"
        ) from exc

    model = AttributionRecord if kind == "attribution" else Sale
    try:
        item = model.model_validate(payload)
    except ValidationError as exc:
        raise FileUploadError(
            422,
            f"{file_label}: line {line_no}: record validation failed:"
            f" {_format_validation_error(exc)}",
        ) from exc

    if kind == "attribution":
        assert isinstance(item, AttributionRecord)
        return (
            item.id,
            item.customer,
            item.manager,
            item.effective_start.isoformat(),
            item.effective_end.isoformat() if item.effective_end is not None else None,
            _to_epoch_us(item.registered_at),
        )
    assert isinstance(item, Sale)
    return (item.id, item.customer, item.sale_date.isoformat())


def _raise_duplicate(
    conn: sqlite3.Connection, kind: str, batch: list[tuple], file_label: str
) -> None:
    """Find the offending ID after an IntegrityError, then fail."""
    id_position = _ID_POSITION[kind]
    for row in batch:
        try:
            conn.execute(_INSERT_SQL[kind], row)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            raise FileUploadError(
                422,
                f"{file_label}: duplicate id {row[id_position]!r};"
                " ids must be unique within the file",
            ) from None
    raise FileUploadError(422, f"{file_label}: duplicate ids are not allowed")


def _flush(
    conn: sqlite3.Connection, kind: str, batch: list[tuple], file_label: str
) -> None:
    try:
        conn.executemany(_INSERT_SQL[kind], batch)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        _raise_duplicate(conn, kind, batch, file_label)


def _ingest_one(
    conn: sqlite3.Connection, *, stream: BinaryIO, file_label: str, kind: str
) -> int:
    """Stream one gzip/JSONL upload into SQLite. Returns the record count."""
    # The gzip module silently accepts a zero-byte stream as "empty"; a real
    # gzip member is required, so check the magic bytes up front.
    magic = stream.read(2)
    if len(magic) < 2:
        raise FileUploadError(400, f"{file_label}: empty file; a gzip stream is required")
    if magic != b"\x1f\x8b":
        raise FileUploadError(400, f"{file_label}: not a gzip stream (bad magic bytes)")
    gz = gzip.GzipFile(fileobj=_PrependStream(magic, stream), mode="rb")
    buffered = io.BufferedReader(gz, buffer_size=_GZIP_BUFFER_BYTES)
    batch: list[tuple] = []
    count = 0
    try:
        for line_no, content in _iter_lines(buffered, file_label):
            row = _decode_and_validate(content, line_no, file_label, kind)
            if kind == "sales":
                row = (count,) + row
            batch.append(row)
            count += 1
            if len(batch) >= _INSERT_BATCH_SIZE:
                _flush(conn, kind, batch, file_label)
                batch.clear()
        if batch:
            _flush(conn, kind, batch, file_label)
    except FileUploadError:
        raise
    except (OSError, EOFError, zlib.error) as exc:
        # Bad gzip magic, corrupt deflate data, bad/missing trailer, ...
        raise FileUploadError(
            400, f"{file_label}: cannot read gzip stream ({exc})"
        ) from exc
    return count


def _check_customer_limit(
    conn: sqlite3.Connection, *, table: str, file_label: str
) -> None:
    limit = MAX_ROWS_PER_CUSTOMER_PER_FILE
    row = conn.execute(
        f"SELECT customer, COUNT(*) AS n FROM {table}"
        " GROUP BY customer HAVING n > ? ORDER BY n DESC LIMIT 1",
        (limit,),
    ).fetchone()
    if row is not None:
        raise FileUploadError(
            422,
            f"{file_label}: customer {row[0]!r} has {row[1]} rows;"
            f" the per-customer limit is {limit}",
        )


# Duck-typed stand-in for AttributionRecord in the compute path: the shared
# analysis functions only read these attributes, and reconstructing full
# pydantic models for already-validated rows would be wasted work.
_Record = namedtuple(
    "_Record",
    ["id", "customer", "manager", "effective_start", "effective_end", "registered_at"],
)


def _row_to_record(row: tuple) -> _Record:
    id_, customer, manager, start, end, registered_us = row
    return _Record(
        id=id_,
        customer=customer,
        manager=manager,
        effective_start=date.fromisoformat(start),
        effective_end=date.fromisoformat(end) if end is not None else None,
        registered_at=_from_epoch_us(registered_us),
    )


def _point_result(segments, sale_date: date) -> tuple[str, str | None, tuple[str, ...]]:
    segment = segment_for_date(segments, sale_date)
    if segment is None:
        return ("pending_verification", None, ())
    decision = segment.decision
    return (
        decision.status,
        decision.manager,
        tuple(record.id for record in decision.sources),
    )


def _side_fragment(
    cache: dict, result: tuple[str, str | None, tuple[str, ...]]
) -> str:
    """Rendered before/after JSON object; cached per customer (bounded)."""
    fragment = cache.get(result)
    if fragment is None:
        fragment = json.dumps(
            {
                "status": result[0],
                "manager": result[1],
                "source_record_ids": list(result[2]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cache[result] = fragment
    return fragment


def _render_line(
    cache: dict,
    sale_id: str,
    before: tuple[str, str | None, tuple[str, ...]],
    after: tuple[str, str | None, tuple[str, ...]],
) -> tuple[str, bool]:
    requires_backfill = before[0] != after[0] or before[1] != after[1]
    should_reassign = requires_backfill and after[0] == "assigned"
    line = (
        '{"sale_id":'
        + json.dumps(sale_id, ensure_ascii=False)
        + ',"before":'
        + _side_fragment(cache, before)
        + ',"after":'
        + _side_fragment(cache, after)
        + ',"requires_backfill":'
        + ("true" if requires_backfill else "false")
        + ',"should_reassign":'
        + ("true" if should_reassign else "false")
        + "}"
    )
    return line, requires_backfill


def _compute_results(
    conn: sqlite3.Connection, before: datetime, after: datetime
) -> tuple[int, int]:
    """Compute one JSONL line per sale into the results table.

    Only one customer's records and sales (each capped at
    ``MAX_ROWS_PER_CUSTOMER_PER_FILE``) are held in memory at a time.
    """
    total = 0
    backfill_count = 0
    batch: list[tuple] = []
    customers = conn.execute("SELECT DISTINCT customer FROM sales ORDER BY customer")
    for (customer,) in _iter_cursor(customers):
        records = [
            _row_to_record(row)
            for row in conn.execute(
                "SELECT id, customer, manager, effective_start, effective_end,"
                " registered_at_us FROM attribution WHERE customer = ?",
                (customer,),
            )
        ]
        # The as-of rule: records registered after a settlement point are
        # invisible at that point, exactly as in the JSON endpoint.
        records_before = [r for r in records if r.registered_at <= before]
        records_after = [r for r in records if r.registered_at <= after]
        segments_before = build_timeline_segments(customer, records_before)
        if len(records_before) == len(records_after):
            # One visible set is always a subset of the other, so equal
            # sizes mean identical timelines at both points.
            segments_after = segments_before
        else:
            segments_after = build_timeline_segments(customer, records_after)
        fragment_cache: dict = {}
        sales = conn.execute(
            "SELECT seq, id, sale_date FROM sales WHERE customer = ? ORDER BY seq",
            (customer,),
        )
        for seq, sale_id, sale_date_text in _iter_cursor(sales):
            sale_date = date.fromisoformat(sale_date_text)
            line, requires_backfill = _render_line(
                fragment_cache,
                sale_id,
                _point_result(segments_before, sale_date),
                _point_result(segments_after, sale_date),
            )
            batch.append((seq, line))
            total += 1
            backfill_count += requires_backfill
        if len(batch) >= _INSERT_BATCH_SIZE:
            conn.executemany("INSERT INTO results (seq, line) VALUES (?, ?)", batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO results (seq, line) VALUES (?, ?)", batch)
        conn.commit()
    return total, backfill_count


def run_file_analysis(
    *,
    attribution_stream: BinaryIO,
    sales_stream: BinaryIO,
    before: datetime,
    after: datetime,
    tmp_dir: str,
) -> tuple[str, int, int]:
    """Ingest, validate and compute. Returns (db_path, totals...).

    Both gzip streams are read to EOF (trailer check), every record is
    validated, and all results are computed before this returns. Any
    failure raises :class:`FileUploadError` (or propagates) and no results
    are produced. The caller owns ``tmp_dir`` cleanup afterwards.
    """
    db_path = os.path.join(tmp_dir, "work.db")
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        _ingest_one(
            conn, stream=attribution_stream, file_label="attribution_file", kind="attribution"
        )
        _ingest_one(conn, stream=sales_stream, file_label="sales_file", kind="sales")
        conn.executescript(_INDEXES)
        _check_customer_limit(conn, table="attribution", file_label="attribution_file")
        _check_customer_limit(conn, table="sales", file_label="sales_file")
        totals = _compute_results(conn, before, after)
        return db_path, totals[0], totals[1]
    finally:
        conn.close()


def stream_results_and_cleanup(db_path: str, tmp_dir: str) -> Iterator[str]:
    """Yield JSONL lines in sales-file order, then remove the temp dir.

    The ``finally`` runs on normal completion and when the response
    generator is closed (client disconnect).
    """
    try:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("SELECT line FROM results ORDER BY seq")
            for (line,) in _iter_cursor(cursor):
                yield line + "\n"
        finally:
            conn.close()
    finally:
        cleanup_tmp_dir(tmp_dir)
