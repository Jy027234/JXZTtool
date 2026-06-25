# 灰度推荐配置

## 默认灰度档

推荐以 `parsecore.pgvector.toml.example` 作为灰度基线：

- `execution_mode = "queue-worker"`
- `database_url = "postgresql://parsecore:parsecore@parsecore-postgres:5432/parsecore"`
- `index.mode = "pgvector"`
- `max_upload_bytes = 52428800`
- RapidOCR 本地 provider
- PDF dual-channel 和 bad-page OCR fallback 开启

没有真实 embedding key 时，使用 `parsecore.pgvector.fake-embedding.toml.example` 完成本地持久化和索引链路验证。

## 启动

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.toml.example"
docker compose --profile pgvector up -d --build
```

本地无外部 embedding key：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.fake-embedding.toml.example"
docker compose --profile pgvector up -d --build
```

## 灰度前检查

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check --skip-regression
```

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.pgvector.toml.example
```

若有 Postgres 可用，设置 `PARSECORE_TEST_POSTGRES_URL` 后运行：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m unittest tests.test_postgres_stores
```

## 灰度中观察

- `GET /health`
- `GET /v1/runtime`
- `GET /v1/parse/metrics?sample_size=200`
- `GET /v1/parse/events?limit=100`
- `GET /v1/parse/indexes/metrics`
- `GET /v1/parse/prometheus`

重点观察：

- `failed` / `dead_lettered` 任务是否增长
- `too_many_inflight_jobs` 是否频繁出现
- `ocr_failed` 是否出现新集中样本
- `high_precision` 覆盖率是否异常低
- 上传超限是否集中在特定租户或客户端

若灰度节点同时承担 nightly perf 跟踪，建议在同一台 runner 或主机上额外挂两类持久目录：

- 样本目录：通过 `PARSECORE_REGRESSION_FIXTURE_ROOT` 指向 perf PDF 根目录
- perf 历史目录：通过 `PARSECORE_PERF_HISTORY_DIR` 指向持久目录，供 workflow 自动复用 `latest.perf.json`

这样 nightly perf job 会自动形成“本次 vs 上次”的趋势对比，而不是每次只看孤立数值。

## 灰度性能基线

灰度第一个稳定窗口建议固化一份运行态基线，至少保存：

- `failure_rate`
- `durations_s.p50 / p90 / p99 / max`
- `active_jobs`
- `too_many_inflight_jobs`、`ocr_failed`、`embedding_skipped` 等事件计数
- `high_precision` 覆盖率与查询效果指标

运行中的 API 可直接生成快照：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/gray_baseline_snapshot.py --base-url http://127.0.0.1:8090 --since-hours 24 --sample-size 200 --out var/self-check/gray-baseline.json
```

如果开启了 `runtime.api_key_env`：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/gray_baseline_snapshot.py --base-url http://127.0.0.1:8090 --api-key $env:PARSECORE_API_KEY --out var/self-check/gray-baseline.json
```

建议保存三个阶段的基线：

1. 灰度前压测或预热后。
2. 灰度 10% 流量稳定 30 到 60 分钟后。
3. 灰度 50% 或计划流量峰值后。

后续性能判断优先和这些基线比较，而不是只看单次绝对值。

## 回滚

保守回滚到 SQLite queue-worker：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.queue.toml"
docker compose up -d --build
```

需要临时关闭上传限制时，将对应配置中的 `max_upload_bytes` 改为 `0` 后重启 API。需要临时关闭 OCR fallback 时，把 PDF parser 的 `ocr_bad_pages` 改为 `false` 后重启 API / worker。

### 专项回滚：Local Provider Routing

当 Provider 灰度导致解析质量下降、耗时异常、依赖错误或 route primary 与实测 best provider 偏差扩大时，先关闭执行路由，不要立即删除 Provider 配置：

```toml
[providers.local_parser_routing]
enabled = false
fallback_to_default = true
include_disabled = false
```

关闭后重启 API / worker。`/providers/route-plan` 仍可继续只读评估候选，实际解析会回到 `[[parsers]]` 的默认顺序。

### 专项回滚：候选 Provider 配置

若只需要撤回某个候选 Provider，把对应 `[[providers.local_parsers]]` 调整为 evaluation-only：

```toml
enabled = false
route_mode = "evaluate"
gate_status = "pending"
```

若该 Provider 已被加入 `[[parsers]]` 执行列表，同时删除或注释对应 parser 块，避免 route fallback 仍命中它。调整后运行：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.toml
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli payload-contract-check
```

### 专项回滚：Profile 灰度

如果某个 profile 灰度异常，优先让宿主停止显式传该 profile，回到 `profile=auto` 或 `default`。服务端配置中应保持候选 profile 不作为默认全量入口；必要时把相关 parser/provider 的 `profiles` 列表临时移除该 profile。

### 专项回滚：Reader 接入

如果宿主阅读页接入 `projection=reader` 后出现渲染问题，优先在宿主侧降级为旧阅读页或 `projection=structured`，ParseCore 侧保留 `reader` 投影继续用于诊断。不要删除 reader 字段或降低 schema version；后续通过固定样本和截图验收修复 reader block/table/figure 渲染问题。

### 专项回滚：Part Rerun

如果局部 rerun 导致质量退化，先停止自动执行 `rerun_warning_parts`，保留 `inspect -> compare` 只读路径。对已有异常文档优先查看 `/parts` 的 `rerun_comparison` 和 `/quality.attention_summary.contracts`，确认是 provider 变更、coverage gap 扩大，还是 chunk/embedding 未闭环，再决定是否整文档 reparse。
