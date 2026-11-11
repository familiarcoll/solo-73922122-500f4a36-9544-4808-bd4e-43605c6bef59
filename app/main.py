from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI

from .analysis import analyze_backfill
from .sample import SAMPLE_REQUEST
from .schemas import AnalyzeRequest, AnalyzeResponse

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
