# ParseCore 使用说明

## 适用读者

本文面向宿主产品开发、测试、实施和运维人员，说明如何把 ParseCore 作为解析中台使用。底层配置细节见 [configuration.md](configuration.md)，灰度部署见 [gray-deployment.md](gray-deployment.md)。

## 使用形态

ParseCore 当前支持四种使用形态：

| 形态 | 适合场景 | 入口 |
| --- | --- | --- |
| 嵌入式 SDK | Python 产品内直接调用解析能力 | `parsecore.bootstrap.build_runtime` |
| HTTP API | 多产品共享解析服务 | `parsecore.cli serve` / ASGI `create_app` |
| Queue Worker | 大文件、长任务、生产灰度 | `parsecore.cli worker` |
| CLI 工具 | 本地自检、压测、运维任务 | `python -m parsecore.cli ...` |

生产建议优先使用 `HTTP API + queue-worker + Postgres/pgvector`。本地开发或单机验证可以使用默认 SQLite/inline 配置。

## 快速启动

安装开发依赖：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[api,parsers,test]"
```

启动本地 API：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli serve --config parsecore.toml --host 127.0.0.1 --port 8090
```

检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8090/v1/runtime
Invoke-RestMethod http://127.0.0.1:8090/v1/parse/profiles
```

## 推荐解析流程

### 中小文档

中小 Word、Excel、PDF 可用同步入口：

```powershell
$body = @{
  doc_id = "demo-doc"
  file_path = "D:/app/uploads/demo.xlsx"
  media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
  options = @{ profile = "auto" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -ContentType "application/json" -Body $body http://127.0.0.1:8090/v1/parse
```

读取结构化结果：

```powershell
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc?projection=structured"
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc/quality"
```

### 大文件和超长 PDF

大文件推荐异步链路：

1. 上传到 `/v1/parse/uploads`。
2. 使用返回的 `parsecore_server_file_path` 创建 `/v1/parse/jobs`。
3. 轮询 `/v1/parse/jobs/{job_id}`。
4. 完成后读取 structured projection、records、导出包或 part 视图。

示例：

```powershell
$job = @{
  doc_id = "big-pdf"
  file_path = "D:/app/uploads/big.pdf"
  media_type = "application/pdf"
  options = @{ profile = "large-pdf-catalog" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -ContentType "application/json" -Body $job http://127.0.0.1:8090/v1/parse/jobs
```

规划 part：

```powershell
Invoke-RestMethod -Method Post -ContentType "application/json" -Body '{"target_pages_per_part":200}' "http://127.0.0.1:8090/v1/parse/documents/big-pdf/parts/plan"
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/big-pdf/parts"
```

异常 part 复跑：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/parse/documents/big-pdf/parts/rerun" -ContentType "application/json" -Body '{"failed_only":true}'
```

## Profile 选择

默认让宿主传 `profile=auto`。只有已知文件类型才显式覆盖：

| Profile | 使用场景 |
| --- | --- |
| `default` | 普通 Word/PDF/文本 |
| `table-heavy` | 表格密集文档 |
| `excel-ledger` | Excel 台账、清单、明细表 |
| `large-pdf` | 长页数 PDF 或同步入口超限 PDF |
| `large-pdf-catalog` | 目录、产品清单、批准目录类超大 PDF |
| `large-pdf-ledger` | PDF 台账、明细表、大批量记录 |
| `ocr-heavy` | OCR 密集 PDF |
| `scan-pdf` | 扫描件 PDF 或图片 |

`large-pdf-catalog` 和 `large-pdf-ledger` 默认走 fast text path，优先产出可追溯 records。若必须高保真表格块或 OCR，可显式传：

```json
{
  "profile": "large-pdf-ledger",
  "post_process": {
    "dual_channel": true,
    "layout_reading_order": true
  },
  "enable_ocr": true
}
```

## Records 查询

解析完成后，中台会持久化 `pages / lines / records`。主文档响应默认不携带这些大 views，records 应通过专用入口分页读取：

```text
GET /v1/parse/documents/{doc_id}/records?limit=100&offset=0
GET /v1/parse/documents/{doc_id}/records?query=TC001A
GET /v1/parse/documents/{doc_id}/records?page_start=2000&page_end=2300
GET /v1/parse/documents/{doc_id}/records?quality_signal=column_shift_suspected
GET /v1/parse/documents/{doc_id}/records?field.certificate_or_project_no=PMA0013-01-XN
```

records 可能来自表格行，也可能来自目录/台账文本行聚合。消费方应优先使用：

- `record_id`
- `source`
- `fields`
- `raw_text`
- `normalized_text`
- `page_start / page_end`
- `quality_signal_codes`

## 导出数据

同步导出适合中小结果集和排查：

```powershell
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=jsonl" -OutFile records.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=sqlite" -OutFile records.sqlite
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=xlsx" -OutFile records.xlsx
```

大结果集推荐异步导出包：

```powershell
$body = @{
  include = @("pages", "lines", "records", "quality_signals", "parse_units")
  formats = @{ records = "sqlite"; lines = "csv"; pages = "jsonl"; quality_signals = "jsonl"; parse_units = "tsv" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -ContentType "application/json" -Body $body "http://127.0.0.1:8090/v1/parse/documents/demo-doc/export-jobs"
```

支持的 dataset：

```text
pages | lines | tables | quality_signals | parse_units | records
```

支持的格式：

```text
jsonl | csv | tsv | sqlite | xlsx
```

## 质量信号与抽检

上线灰度时至少观察：

- `/v1/parse/documents/{doc_id}/quality`
- `/v1/parse/documents/{doc_id}/parts?state=warning|failed`
- `/v1/parse/metrics`
- `/v1/parse/events?limit=100`
- `/v1/parse/prometheus`

业务抽检建议优先查看：

- `quality_summary.total`
- `quality_summary.by_code`
- `column_shift_suspected`
- `record_field_missing`
- `table_ragged_rows`
- `ocr_failed_page`
- `truncated_table`

## 生产配置建议

生产或灰度建议：

- 使用 `parsecore.pgvector.toml.example` 或由它派生的生产配置。
- 使用 `queue-worker`，不要让 API 进程承接所有大文件解析。
- 使用 Postgres 持久化 job、blocks、chunks、document views 和 metrics。
- 设置 `runtime.api_key_env`，除 `/health` 外保护所有 HTTP 入口。
- 保留 `runtime.max_upload_bytes`，同步入口超限后切到 `/v1/parse/uploads + /v1/parse/jobs`。
- 为大 PDF 设置 `runtime.max_active_parts_per_doc = 2-4` 起步。
- 保存 `var/self-check`、导出包目录、数据库备份和灰度基线快照。

## 交付验收

交付给宿主产品前，建议完成：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pytest -q
git diff --check
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.toml
Invoke-RestMethod http://127.0.0.1:8090/health
```

对于真实业务样本，至少抽检：

1. 一个 Word 或普通 PDF。
2. 一个 Excel 表格文档。
3. 一个表格密集 PDF。
4. 一个大 PDF part 解析样本。
5. 一次 records SQLite 或 JSONL 导出。

验收通过后，本版可按“受控生产灰度”交付。
