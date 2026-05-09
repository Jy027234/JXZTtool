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
