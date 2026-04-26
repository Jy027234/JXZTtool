# OCR 网关契约

## 目标

本文件固定 `providers.ocr.provider = "remote-http"` 时，ParseCore 与宿主 OCR 网关之间的最小 HTTP 契约。

适用范围：

- `image-ocr` parser
- PDF 坏页 OCR 回退
- 需要把 OCR 模型与 ParseCore 解析容器解耦的宿主产品

## 请求契约

### 目标地址

- 方法：`POST`
- URL：`{base_url}{endpoint_path}`
- 默认 `endpoint_path`：`/ocr`
- `endpoint_path` 通过 `providers.ocr.options.endpoint_path` 配置

### 请求头

- `Content-Type: application/json`
- 若配置了 `api_key_env`，ParseCore 会自动加 `Authorization: Bearer <token>`
- `providers.ocr.options.headers` 中的键值会原样并入 HTTP 头，适合透传租户、环境或路由标签

### 请求体

请求体是 JSON，对宿主 OCR 网关的稳定字段如下：

```json
{
  "image_base64": "<base64>",
  "mime_type": "image/png",
  "file_name": "sample.png",
  "options": {
    "det_use_dilation": true
  }
}
```

字段说明：

- `image_base64`：必填，图片或 PDF 坏页渲染后的图像内容
- `mime_type`：必填，文件路径输入时按扩展名推断；PDF 坏页回退固定为 `image/png`
- `file_name`：可选，只有源输入是文件路径时才会带出
- `options`：可选，除 `endpoint_path` / `headers` 外，其余 `providers.ocr.options.*` 都会放进这里

## 响应契约

ParseCore 当前接受以下任一响应结构：

```json
{
  "result": [
    {
      "bbox": [[0, 0], [10, 0], [10, 8], [0, 8]],
      "text": "Detected text",
      "confidence": 0.97
    }
  ],
  "elapsed": 0.42
}
```

也兼容：

- 顶层 `results`
- `data.result`
- `data.results`
- `data.items`

单个 OCR 区域也兼容这些字段别名：

- 框坐标：`bbox` / `box` / `polygon`
- 置信度：`confidence` / `score`

耗时字段也兼容：

- `elapsed`
- `elapsed_seconds`

## 失败语义

当前版本里，ParseCore 会把 OCR 失败映射为以下可观测信号：

- 配置错误：`provider_configuration_error`
- 请求或响应失败：`provider_request_failed`
- provider 不可用：`provider_unavailable`
- OCR 成功返回但无可用文本：`empty_ocr_text`

这些信息会出现在：

- block metadata：`ocr_attempted` / `ocr_attempt_reason` / `ocr_error_reason`
- `/v1/parse/events`：`ocr_attempted` / `ocr_fallback` / `ocr_failed`
- `/v1/parse/prometheus`：`parse_ocr_attempt_total` / `parse_ocr_fallback_total` / `parse_ocr_failed_total`

## 推荐配置

示例配置见 [parsecore.remote-http.toml.example](../parsecore.remote-http.toml.example)。

最小片段如下：

```toml
[providers.ocr]
enabled = true
provider = "remote-http"
base_url = "https://ocr.example.com"
api_key_env = "PARSECORE_OCR_API_KEY"
timeout_seconds = 10.0
max_retries = 2
options = { endpoint_path = "/ocr/v1", headers = { "X-OCR-Tenant" = "tenant-a" }, det_use_dilation = true }
```

## 宿主侧验收清单

1. 环境变量 `PARSECORE_OCR_API_KEY` 已注入 ParseCore 进程或容器。
2. `GET /health` 返回的 `services.paddleocr` 为 `true`。
3. 用一张图片或一份坏页 PDF 触发真实 OCR。
4. `GET /v1/parse/events?event_type=ocr_failed` 能在失败时看见 `error_reasons`。
5. `GET /v1/parse/prometheus` 能看见 `parse_ocr_attempt_total`、`parse_ocr_fallback_total`、`parse_ocr_failed_total`。

## 可执行契约校验

仓库内已有一条真实 HTTP contract test，会启动一个本地 HTTP server 而不是 mock `urlopen`：

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m unittest tests.test_ocr.OcrProviderTests.test_remote_http_provider_matches_gateway_contract_over_real_http
```

这条测试会验证：

- 请求路径
- `Authorization` 与自定义 headers
- 请求体里的 `image_base64 / mime_type / file_name / options`
- 响应体的 OCR 结果与耗时归一化