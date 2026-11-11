# 销售归属回溯分析 API（FastAPI）

在客户经理归属记录被**追溯登记**后，对比两个结算时点的归属结果，输出：

1. 两个时点各自的归属分段（生效区间、经理、状态、来源记录、命中的销售）；
2. 归属经理或“待核实”状态发生变化的销售 ID；
3. 每个变化销售的前后结果，以及两边的来源记录，供数仓回填事实表。

面向整月 ERP 导出的大文件场景，另提供 `POST /api/v1/backfill/analyze-files`：
上传两份 gzip 压缩 JSONL，按销售文件原序流式返回 JSONL 附件，服务端借助
临时 SQLite 处理，**不全量载入内存**（见下文「大文件分析 API」）。

## 一键启动

```bash
docker compose up --build
```

启动后：

| 入口 | 地址 |
| --- | --- |
| Swagger 交互文档（可直接 Try it out，含文件上传入口） | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| OpenAPI JSON | http://localhost:8000/openapi.json |
| 获取可运行的示例请求 | http://localhost:8000/api/v1/backfill/sample-request |
| 直接执行示例并返回结果 | `POST http://localhost:8000/api/v1/backfill/demo` |
| 大文件分析（multipart 上传） | `POST http://localhost:8000/api/v1/backfill/analyze-files` |
| 健康检查 | http://localhost:8000/health |

Swagger 的 `POST /api/v1/backfill/analyze` 请求体已经内置 `retroactive_correction` 示例，点击 **Try it out → Execute** 即可实际调用。端口冲突时可改端口：

```bash
API_PORT=18000 docker compose up --build
```

不通过 Docker 也可运行：

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### `POST /api/v1/backfill/analyze`

请求体：

```json
{
  "attribution_records": [
    {
      "id": "ATTR-001",
      "customer": "CUST-001",
      "manager": "M-ALPHA",
      "effective_start": "2024-01-01",
      "effective_end": "2024-07-01",
      "registered_at": "2024-01-02T08:00:00Z"
    }
  ],
  "sales": [
    {
      "id": "SALE-001",
      "customer": "CUST-001",
      "sale_date": "2024-02-10"
    }
  ],
  "settlement_points": [
    "2024-05-01T00:00:00Z",
    "2024-07-01T00:00:00Z"
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `attribution_records[].id` | 归属记录唯一 ID；全局不可重复 |
| `attribution_records[].customer` | 客户 / 账户 ID |
| `attribution_records[].manager` | 客户经理 ID |
| `attribution_records[].effective_start` | 生效区间起点，**包含** |
| `attribution_records[].effective_end` | 生效区间终点，**不包含**；可为 `null` 表示长期有效 |
| `attribution_records[].registered_at` | UTC 登记时间；无时区的时间按 UTC 处理 |
| `sales[].id` | 销售明细唯一 ID；全局不可重复 |
| `sales[].customer` | 客户 / 账户 ID |
| `sales[].sale_date` | 销售业务日期 |
| `settlement_points` | **恰好两个**结算时点，按时间先后传入 `[before, after]` |

`effective_end <= effective_start`、重复 ID、结算时点数量不为 2 时返回 HTTP 422。接口默认拒绝未声明字段，避免 ERP 字段漂移被静默忽略。

为方便直接接入 ERP 导出，输入还兼容常见列名（如 `record_id`、`customer_id`、`manager_id`、`start_date`、`end_date`、`registration_time`、`sale_id`、`sales_date`、`settlement_times`）；OpenAPI 中展示并推荐使用上表的规范字段名。数值型 ID 会自动转成字符串。

### 计算规则

1. **按结算时点快照**：在某个结算时点，只使用 `registered_at <= settlement_point` 的归属记录。登记时间晚于该时点的记录完全不参与，即使其生效日期很早。
2. **左闭右开生效区间**：销售日期 `d` 只有在 `effective_start <= d < effective_end`（`effective_end = null` 时无上界）才算被记录覆盖。
3. **登记时间最新者胜出**：把所有起止边界切成区间；每个区间内取 `registered_at` 最大的记录。这样局部更正只覆盖与旧记录重叠的部分，旧记录在重叠范围之外继续有效。
4. **同登记时间冲突**：若同一区间内最新登记时间相同、却指向不同 `manager`，整段标为 `status = pending_verification`、`reason = conflict`，并返回 `conflicting_managers` 与并列来源记录。登记时间相同但经理相同的多条记录不算冲突。
5. **未覆盖**：销售日期没有可见记录覆盖，标为 `status = pending_verification`、`reason = uncovered`。
6. **变化识别**：同一个 `sales[].id` 的两个时点结果只要 `status` 或 `manager` 不同，就进入 `changes`。两个时点都是待核实（即便 `reason` 从 uncovered 变为 conflict）不视为状态变化。
7. **回填建议**：
   - `requires_backfill = true`：该销售进入 `changes`，事实表来源需要更新；
   - `should_reassign = true`：新结果已明确归属，并且经理变更或从待核实变为已归属；
   - 从已归属变为待核实时，`should_reassign = false`，但仍 `requires_backfill = true`（需要先核实，不能自动改派）。

### 响应结构

```json
{
  "settlement_points": [
    {
      "label": "before",
      "settlement_point": "2024-05-01T00:00:00Z",
      "attribution_segments": [
        {
          "customer": "CUST-001",
          "effective_start": "2024-01-01",
          "effective_end": "2024-07-01",
          "status": "assigned",
          "manager": "M-ALPHA",
          "reason": null,
          "conflicting_managers": [],
          "source_record_ids": ["ATTR-001"],
          "source_records": ["..."],
          "sale_ids": ["SALE-001"],
          "sales_count": 1
        }
      ],
      "sales": [
        {
          "sale_id": "SALE-001",
          "customer": "CUST-001",
          "sale_date": "2024-02-10",
          "settlement_point": "2024-05-01T00:00:00Z",
          "status": "assigned",
          "manager": "M-ALPHA",
          "reason": null,
          "conflicting_managers": [],
          "source_record_ids": ["ATTR-001"],
          "source_records": ["..."],
          "segment_start": "2024-01-01",
          "segment_end": "2024-07-01"
        }
      ],
      "totals": {
        "sale_count": 1,
        "assigned_count": 1,
        "pending_verification_count": 0,
        "conflict_count": 0,
        "uncovered_count": 0,
        "segment_count": 1
      }
    },
    {
      "label": "after",
      "settlement_point": "2024-07-01T00:00:00Z",
      "attribution_segments": ["..."],
      "sales": ["..."],
      "totals": {"...": "..."}
    }
  ],
  "changes": [
    {
      "sale_id": "SALE-001",
      "customer": "CUST-001",
      "sale_date": "2024-02-10",
      "change_type": "manager_changed",
      "changed_fields": ["manager"],
      "requires_backfill": true,
      "should_reassign": true,
      "before": {"status": "assigned", "manager": "M-ALPHA", "source_record_ids": ["ATTR-001"], "source_records": ["..."]},
      "after": {"status": "assigned", "manager": "M-BETA", "source_record_ids": ["ATTR-002"], "source_records": ["..."]}
    }
  ],
  "summary": {
    "settlement_points": ["2024-05-01T00:00:00Z", "2024-07-01T00:00:00Z"],
    "total_sales": 1,
    "changed_sales_count": 1,
    "should_reassign_count": 1,
    "requires_backfill_count": 1,
    "before_totals": {"...": "..."},
    "after_totals": {"...": "..."},
    "should_reassign_sale_ids": ["SALE-001"],
    "requires_backfill_sale_ids": ["SALE-001"]
  }
}
```

`attribution_segments` 是**按客户、时间顺序**切出的完整有效区间；区间内结果稳定，因此同一条记录驱动的相邻区间会被合并。`sale_ids` 是该区间命中的销售 ID，便于按分段回填。`sales` 同时给出逐销售结果，直接以销售 ID 为主键使用。`source_records` 和两时点的 `before/after.source_records` 是完整来源记录，而不仅是 ID。

`change_type` 取值：

- `manager_changed`：两个时点均已归属，经理不同；
- `pending_to_assigned`：待核实变为已归属；
- `assigned_to_pending`：已归属变为待核实（冲突或未覆盖）。

## 大文件分析 API

### `POST /api/v1/backfill/analyze-files`

面向数仓工程师处理整月 ERP 导出的场景：multipart 上传两份 **gzip 压缩的
UTF-8 JSONL** 及两个结算时点，按**销售文件原序**流式返回 JSONL 附件。

表单字段（`/docs` 中可直接选择文件、填写时点并执行）：

| 字段 | 说明 |
| --- | --- |
| `attribution_file` | 归属记录文件（`.jsonl.gz`），每行一个 `AttributionRecord`，字段与 JSON 接口一致（含 ERP 别名兼容） |
| `sales_file` | 销售明细文件（`.jsonl.gz`），每行一个 `Sale` |
| `settlement_point_before` | 前一个结算时点（ISO-8601；无时区按 UTC） |
| `settlement_point_after` | 后一个结算时点 |

**输入限制**（超限即整个请求失败）：

- 每文件最多 1,000,000 行；
- 每行最多 16 KiB（16,384 字节，不含换行符）；
- 每客户在每文件中最多 1,000 行；
- ID 在各自文件内全局唯一（销售 ID 与归属记录 ID 分属两个命名空间）；
- 文件内行序任意（乱序可正常处理）。

**输出**：`application/x-ndjson` 附件（`backfill-results.jsonl`），每行一个
销售的前后对比，顺序与上传的销售文件完全一致：

```json
{"sale_id":"SALE-002","before":{"status":"assigned","manager":"M-ALPHA","source_record_ids":["ATTR-001"]},"after":{"status":"assigned","manager":"M-BETA","source_record_ids":["ATTR-002"]},"requires_backfill":true,"should_reassign":true}
```

响应头附带 `X-Total-Sales`（总行数）与 `X-Requires-Backfill-Sales`（需回填行数）。
归属判定规则与 JSON 接口完全一致；`requires_backfill` 等价于该销售是否进入
JSON 接口的 `changes`。

**资源与可靠性保证**：

- 不全量载入输入、ID 集合或结果：上传流逐行解析写入临时 SQLite，ID 唯一性
  由 SQLite 唯一索引保证，每客户行数上限由 `GROUP BY` 校验，结果写回 SQLite
  后按 `seq` 流式输出；内存占用不随客户总数增长（每客户最多 1000+1000 行的
  工作集）。
- 两份压缩流**完整读完并通过 gzip 尾部校验、全部记录通过校验、全部计算完成**
  后才返回成功响应；任一步失败（gzip 损坏/截断、非 UTF-8、JSON 语法错误、
  字段校验失败、ID 重复、行数/行长/客户行数超限）都返回 4xx 错误，不提供
  部分结果。
- 上传失败、处理失败、下载完成或客户端断开后，本请求的临时文件都会被清理。
- 临时目录默认在系统临时目录下，可用环境变量 `BACKFILL_TMP_DIR` 指定。

**乱序样例**（`examples/files/`，数据与内置示例相同但行序打乱）：

```bash
curl -sS -X POST http://localhost:8000/api/v1/backfill/analyze-files \
  -F "attribution_file=@examples/files/attribution-records.jsonl.gz" \
  -F "sales_file=@examples/files/sales-details.jsonl.gz" \
  -F "settlement_point_before=2024-05-01T00:00:00Z" \
  -F "settlement_point_after=2024-07-01T00:00:00Z" \
  -OJ

# 与预期输出对比（应无差异）
diff backfill-results.jsonl examples/files/expected-results.jsonl
```

`examples/files/` 下同时保留了未压缩的 `.jsonl` 便于查看，`expected-results.jsonl`
为上述调用的预期输出（6 行，顺序与 `sales-details.jsonl` 一致）。

## 示例

下面的示例已包含在 Swagger 中：

- `ATTR-002`、`ATTR-003` 同登记时间且区间在 `2024-05-01..2024-06-01` 重叠，该段为冲突；
- `ATTR-004` 登记于 `2024-08-01`，在第二个结算时点 `2024-07-01` **不可见**；
- `SALE-005` 从 `uncovered` 变为 `M-BETA`；
- 预期 `summary.should_reassign_sale_ids = ["SALE-002", "SALE-004", "SALE-005"]`。

实际调用：

```bash
curl -sS -X POST http://localhost:8000/api/v1/backfill/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/sample-request.json | python3 -m json.tool

# 不需要请求体，直接跑内置示例
curl -sS -X POST http://localhost:8000/api/v1/backfill/demo | python3 -m json.tool
```

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

测试覆盖按登记时间截断、局部覆盖、左闭右开边界、同时间冲突、未覆盖、变化分类、输入校验，以及 Swagger 示例和 `/demo` 的实际调用；大文件接口另覆盖乱序输入、与 JSON 接口的随机化差分对比、各类文件/行/客户限额、gzip 尾部校验失败、以及临时文件在成功、失败与客户端断开时的清理。

## 项目结构

```text
app/
  main.py           # FastAPI 路由、OpenAPI 示例、示例/演示/大文件入口
  schemas.py        # 请求/响应模型与校验
  analysis.py       # 归属分层、分段、时点对比（两个接口共用）
  file_analysis.py  # 大文件流式摄取、临时 SQLite、逐客户计算、流式输出
  sample.py         # 内置示例
tests/              # 单元测试、API 测试与大文件接口测试
examples/           # 可直接 curl 的示例请求与乱序 gzip 样例
Dockerfile
docker-compose.yml
```
