from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .analysis import analyze_backfill
from .file_analysis import (
    MAX_LINE_BYTES,
    MAX_LINES_PER_FILE,
    MAX_ROWS_PER_CUSTOMER_PER_FILE,
    FileUploadError,
    cleanup_tmp_dir,
    make_request_tmp_dir,
    run_file_analysis,
    stream_results_and_cleanup,
)
from .sample import SAMPLE_REQUEST
from .schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    BackfillResultLine,
    _normalise_utc,
)

DESCRIPTION = """
对比两个结算时点的 ERP 客户经理归属快照，找出需要回填数仓事实表的销售。

**时点规则：** 每个结算时点只使用 `registered_at <= settlement_point` 的归属记录；
生效区间为左闭右开 `[effective_start, effective_end)`。

**分层规则：** 区间内取 `registered_at` 最新的记录。局部更正只覆盖与旧记录
重叠的部分，重叠范围之外旧记录继续有效。

**待核实规则：** 销售日期无可见记录覆盖时为 `uncovered`；同一重叠区间内
最新登记时间相同但指向不同经理时为 `conflict`。两种情况均返回
`pending_verification`。

`POST /api/v1/backfill/demo` 无需请求体，直接执行内置示例。

`POST /api/v1/backfill/analyze-files` 面向整月 ERP 导出：上传两份 gzip 压缩的
UTF-8 JSONL（归属记录 + 销售明细）及两个结算时点，按销售文件原序流式返回
JSONL 附件。单文件最多 100 万行、单行最多 16 KiB、每客户每文件最多 1000 行；
服务端借助临时 SQLite 处理，不全量载入内存。
"""

app = FastAPI(
    title="Sales Attribution Backfill Analysis API",
    version="1.0.0",
    description=DESCRIPTION,
    tags=[
        {"name": "analysis", "description": "追溯登记后的两个时点归属对比。"},
        {"name": "operations", "description": "服务信息、健康检查与示例请求。"},
    ],
)

SAMPLE_EXAMPLE = {
    "summary": "2024-06-15 追溯登记后的两个结算时点示例",
    "description": (
        "ATTR-002 与 ATTR-003 同登记时间，且在 2024-05-01..2024-06-01 重叠，"
        "重叠部分为冲突；ATTR-004 在第二个结算时点之后登记，该时点不可见。"
    ),
    "value": SAMPLE_REQUEST,
}


@app.get("/", tags=["operations"], summary="服务入口")
def root() -> dict:
    return {
        "service": "Sales Attribution Backfill Analysis API",
        "version": "1.0.0",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "sample_request": "/api/v1/backfill/sample-request",
        "demo": "POST /api/v1/backfill/demo",
        "large_files": "POST /api/v1/backfill/analyze-files",
    }


@app.get("/health", tags=["operations"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.get(
    "/api/v1/backfill/sample-request",
    response_model=AnalyzeRequest,
    tags=["operations"],
    summary="获取可直接运行的示例请求",
)
def sample_request() -> AnalyzeRequest:
    """返回与 Swagger 示例相同的请求体。

    可直接复制到 `POST /api/v1/backfill/analyze`，或调用 demo 接口执行。
    """
    return AnalyzeRequest.model_validate(SAMPLE_REQUEST)


@app.post(
    "/api/v1/backfill/demo",
    response_model=AnalyzeResponse,
    tags=["operations"],
    summary="执行内置示例并返回分析结果",
)
def demo() -> AnalyzeResponse:
    """执行内置示例，用于验证部署是否可用。"""
    return analyze_backfill(AnalyzeRequest.model_validate(SAMPLE_REQUEST))


@app.post(
    "/api/v1/backfill/analyze",
    response_model=AnalyzeResponse,
    tags=["analysis"],
    summary="对比两个结算时点并列出回填候选销售",
    response_description=(
        "两个时点的归属分段、逐销售结果，以及归属或待核实状态发生变化的销售。"
    ),
)
@app.post(
    "/analyze",
    response_model=AnalyzeResponse,
    include_in_schema=False,
)
@app.post(
    "/api/v1/analyze",
    response_model=AnalyzeResponse,
    include_in_schema=False,
)
@app.post(
    "/api/backfill/analyze",
    response_model=AnalyzeResponse,
    include_in_schema=False,
)
def analyze(
    request: Annotated[
        AnalyzeRequest,
        Body(
            openapi_examples={"retroactive_correction": SAMPLE_EXAMPLE},
        ),
    ],
) -> AnalyzeResponse:
    return analyze_backfill(request)


_NDJSON_RESPONSE_SCHEMA = {
    "description": (
        "JSONL 附件，按销售文件原序每行一个销售的前后归属对比。"
        "仅当两份 gzip 流完整读完（含尾部校验）、全部记录通过校验并完成计算后才返回。"
    ),
    "content": {
        "application/x-ndjson": {
            "schema": BackfillResultLine.model_json_schema(),
            "example": (
                '{"sale_id":"SALE-002","before":{"status":"assigned","manager":"M-ALPHA",'
                '"source_record_ids":["ATTR-001"]},"after":{"status":"assigned",'
                '"manager":"M-BETA","source_record_ids":["ATTR-002"]},'
                '"requires_backfill":true,"should_reassign":true}'
            ),
        }
    },
    "headers": {
        "X-Total-Sales": {"schema": {"type": "integer"}, "description": "输出总行数（销售数）"},
        "X-Requires-Backfill-Sales": {
            "schema": {"type": "integer"},
            "description": "需要回填的销售行数",
        },
    },
}


@app.post(
    "/api/v1/backfill/analyze-files",
    tags=["analysis"],
    summary="上传 gzip JSONL 大文件，流式返回逐销售回填结果",
    response_class=StreamingResponse,
    description=(
        "整月 ERP 导出的大文件入口。限制：每文件最多 "
        f"{MAX_LINES_PER_FILE:,} 行、每行最多 {MAX_LINE_BYTES:,} 字节（16 KiB）、"
        f"每客户每文件最多 {MAX_ROWS_PER_CUSTOMER_PER_FILE:,} 行；ID 在各自文件内全局唯一。"
        "处理过程借助临时 SQLite，不全量载入输入、ID 集合或结果；"
        "请求结束（成功、失败或客户端断开）后临时文件即被清理。"
    ),
    responses={
        200: _NDJSON_RESPONSE_SCHEMA,
        400: {"description": "gzip 损坏、非 UTF-8、行超长、行数超限或 JSON 解析失败。"},
        422: {"description": "记录字段校验失败、ID 重复或单客户行数超限。"},
    },
)
def analyze_files(
    attribution_file: Annotated[
        UploadFile,
        File(description="归属记录：gzip 压缩的 UTF-8 JSONL，每行一条 AttributionRecord。"),
    ],
    sales_file: Annotated[
        UploadFile,
        File(description="销售明细：gzip 压缩的 UTF-8 JSONL，每行一条 Sale。"),
    ],
    settlement_point_before: Annotated[
        datetime,
        Form(description="前一个结算时点（ISO-8601；无时区按 UTC）。"),
    ],
    settlement_point_after: Annotated[
        datetime,
        Form(description="后一个结算时点（ISO-8601；无时区按 UTC）。"),
    ],
) -> StreamingResponse:
    before = _normalise_utc(settlement_point_before)
    after = _normalise_utc(settlement_point_after)
    tmp_dir = make_request_tmp_dir()
    try:
        db_path, total_sales, requires_backfill_sales = run_file_analysis(
            attribution_stream=attribution_file.file,
            sales_stream=sales_file.file,
            before=before,
            after=after,
            tmp_dir=tmp_dir,
        )
    except FileUploadError as exc:
        cleanup_tmp_dir(tmp_dir)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception:
        cleanup_tmp_dir(tmp_dir)
        raise

    return StreamingResponse(
        stream_results_and_cleanup(db_path, tmp_dir),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="backfill-results.jsonl"',
            "X-Total-Sales": str(total_sales),
            "X-Requires-Backfill-Sales": str(requires_backfill_sales),
        },
        # Belt and braces: the generator cleans up on completion/close; the
        # background task also runs after the response finishes or the client
        # disconnects. Cleanup is idempotent.
        background=BackgroundTask(cleanup_tmp_dir, tmp_dir),
    )
