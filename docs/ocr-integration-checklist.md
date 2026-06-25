# OCR 接入清单

## 目标

本清单用于在宿主系统接入 ParseCore OCR 能力前，快速确认 provider 选择、配置项、探活方式、事件面和回滚条件，避免把“未触发 OCR”“OCR provider 不可用”“OCR 失败页增加”混在一起。

## 阶段 0：确定 provider 形态

1. 若宿主环境可直接安装 OCR 依赖，优先使用 `providers.ocr.provider = "rapidocr"`。
2. 若宿主已有统一 OCR 网关，使用 `providers.ocr.provider = "remote-http"`，并统一走 [ocr-gateway-contract.md](ocr-gateway-contract.md) 约定的请求/响应结构。
3. 只做解析链路联调时，不必同时切存储；若要把 OCR、embedding、pgvector 一起联调，直接从仓库根目录的 `parsecore.pgvector.fake-embedding.toml.example` 起步。

## 阶段 1：配置检查

1. `providers.ocr.enabled = true`。
2. `provider = "rapidocr"` 时，确认环境已安装 `rapidocr_onnxruntime` 对应依赖。
3. `provider = "remote-http"` 时，至少配置 `base_url`；若需要鉴权，补 `api_key_env`。
4. 若宿主 OCR 网关依赖环境或租户头，放在 `providers.ocr.options.headers`。
5. 宿主网关的自定义开关统一放在 `providers.ocr.options`，其中 `endpoint_path` 与 `headers` 由 ParseCore 当作传输层配置消费，其余键会透传到请求体 `options` 字段。

## 阶段 2：契约验证

1. 先用 [ocr-gateway-contract.md](ocr-gateway-contract.md) 对照请求体和响应体，确认网关支持 `image_base64 / mime_type / file_name? / options?` 输入，以及 `result[]` 或 `results[]` 输出。
2. 如联调环境可直连网关，运行真实 HTTP contract test：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m unittest tests.test_ocr.OcrProviderTests.test_remote_http_provider_matches_gateway_contract_over_real_http
```

3. 若只验证本地 provider，可运行完整 OCR 单测：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m unittest tests.test_ocr
```

## 阶段 3：运行态探活

1. 启动 ParseCore 后检查 `GET /health`，确认 `services.paddleocr = true`。
2. 对一份图片或会触发 PDF 坏页 OCR 的样本执行 `POST /parse` 或 `POST /v1/parse`。
3. 若只想对单次请求打开 OCR，可在请求里显式传 `enable_ocr = true`；若要覆盖默认值关闭 OCR，则显式传 `false`。
4. 读取 `GET /v1/parse/events?event_type=ocr_failed`，确认失败页不会持续增长。
5. 读取 `GET /v1/parse/prometheus`，关注 `parse_ocr_attempt_total`、`parse_ocr_fallback_total`、`parse_ocr_failed_total`。

## 阶段 4：定位口径

1. `services.paddleocr = false`：优先看 provider 是否启用、依赖是否安装、远程网关是否可达。
2. block metadata 出现 `ocr_attempted = true` 且带 `ocr_error_reason`：说明 OCR 已触发，但 provider 执行失败。
3. `layout_signals.ocr_attempted_pages > 0` 且 `ocr_failed_pages = 0`：说明 OCR 回退触发且成功。
4. `ocr_attempted_pages = 0`：说明当前样本未触发 OCR，不应误判为 OCR provider 异常。

## 阶段 5：回滚触发条件

1. `services.paddleocr` 持续为 `false`。
2. `parse_ocr_failed_total` 在固定样本或固定租户上持续攀升。
3. `ocr_failed` 事件里的 `error_reasons` 集中指向同一网关问题，例如超时、鉴权失败或响应格式不匹配。
4. 宿主侧需要 OCR 才能恢复文本的 PDF 页面，出现可感知的文本缺失或结构化字段缺失。

## 关联文档

- OCR HTTP 契约见 [ocr-gateway-contract.md](ocr-gateway-contract.md)
- 产品灰度收口见 [go-live-readiness.md](go-live-readiness.md)
- 本地 Provider 离线评估清单见 [local-provider-ir-upgrade-plan.md](local-provider-ir-upgrade-plan.md#离线评估清单)