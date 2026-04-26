from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
import unittest
from types import MappingProxyType
from pathlib import Path
from unittest.mock import patch

from parsecore.config import OcrProviderSettings
from parsecore.ocr import OcrConfigurationError, build_ocr_engine, is_ocr_provider_available


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class OcrProviderTests(unittest.TestCase):
    def test_remote_http_provider_requires_base_url(self) -> None:
        settings = OcrProviderSettings(
            enabled=True,
            provider="remote-http",
        )

        with self.assertRaises(OcrConfigurationError):
            build_ocr_engine(settings)

    def test_remote_http_provider_availability_checks_base_url_and_api_key(self) -> None:
        disabled = OcrProviderSettings(
            enabled=False,
            provider="remote-http",
            base_url="https://example.invalid",
        )
        self.assertFalse(is_ocr_provider_available(disabled))

        missing_url = OcrProviderSettings(enabled=True, provider="remote-http")
        self.assertFalse(is_ocr_provider_available(missing_url))

        with patch.dict(os.environ, {}, clear=True):
            needs_key = OcrProviderSettings(
                enabled=True,
                provider="remote-http",
                base_url="https://example.invalid",
                api_key_env="PARSECORE_OCR_API_KEY",
            )
            self.assertFalse(is_ocr_provider_available(needs_key))

        with patch.dict(os.environ, {"PARSECORE_OCR_API_KEY": "test-key"}, clear=True):
            needs_key = OcrProviderSettings(
                enabled=True,
                provider="remote-http",
                base_url="https://example.invalid",
                api_key_env="PARSECORE_OCR_API_KEY",
            )
            self.assertTrue(is_ocr_provider_available(needs_key))

    def test_remote_http_provider_posts_base64_payload_and_normalizes_response(self) -> None:
        settings = OcrProviderSettings(
            enabled=True,
            provider="remote-http",
            base_url="https://example.invalid",
            timeout_seconds=5.0,
            max_retries=0,
            options={"endpoint_path": "/ocr/v1", "det_use_dilation": True},
        )

        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "sample.png"
            image_path.write_bytes(b"fake-image")
            captured: list[tuple[str, bytes, dict[str, str], float]] = []

            def _fake_urlopen(request, timeout=0):
                captured.append((request.full_url, request.data, dict(request.headers), timeout))
                return _FakeHttpResponse(
                    {
                        "result": [
                            {
                                "bbox": [[0, 0], [10, 0], [10, 8], [0, 8]],
                                "text": "Detected text",
                                "confidence": 0.97,
                            }
                        ],
                        "elapsed": 0.42,
                    }
                )

            with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
                engine = build_ocr_engine(settings)
                result, elapsed = engine(str(image_path))

        self.assertEqual(len(captured), 1)
        endpoint, raw_body, headers, timeout = captured[0]
        self.assertEqual(endpoint, "https://example.invalid/ocr/v1")
        self.assertEqual(timeout, 5.0)
        self.assertEqual(headers["Content-type"], "application/json")

        payload = json.loads(raw_body.decode("utf-8"))
        self.assertEqual(payload["mime_type"], "image/png")
        self.assertEqual(payload["file_name"], "sample.png")
        self.assertEqual(payload["options"], {"det_use_dilation": True})
        self.assertEqual(base64.b64decode(payload["image_base64"]), b"fake-image")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], "Detected text")
        self.assertAlmostEqual(result[0][2], 0.97)
        self.assertAlmostEqual(elapsed, 0.42)

    def test_remote_http_provider_accepts_frozen_mapping_options_from_loaded_config(self) -> None:
        settings = OcrProviderSettings(
            enabled=True,
            provider="remote-http",
            base_url="https://example.invalid",
            timeout_seconds=5.0,
            max_retries=0,
            options=MappingProxyType(
                {
                    "endpoint_path": "/ocr/v2",
                    "headers": MappingProxyType({"X-OCR-Tenant": "tenant-a"}),
                    "det_use_dilation": True,
                }
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "sample.png"
            image_path.write_bytes(b"fake-image")
            captured: list[tuple[str, bytes, dict[str, str], float]] = []

            def _fake_urlopen(request, timeout=0):
                captured.append((request.full_url, request.data, dict(request.headers), timeout))
                return _FakeHttpResponse(
                    {
                        "result": [
                            {
                                "bbox": [[0, 0], [10, 0], [10, 8], [0, 8]],
                                "text": "Detected text",
                                "confidence": 0.97,
                            }
                        ],
                    }
                )

            with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
                engine = build_ocr_engine(settings)
                engine(str(image_path))

        self.assertEqual(len(captured), 1)
        endpoint, raw_body, headers, timeout = captured[0]
        self.assertEqual(endpoint, "https://example.invalid/ocr/v2")
        self.assertEqual(timeout, 5.0)
        self.assertEqual(headers["X-ocr-tenant"], "tenant-a")

        payload = json.loads(raw_body.decode("utf-8"))
        self.assertEqual(payload["options"], {"det_use_dilation": True})

    def test_remote_http_provider_matches_gateway_contract_over_real_http(self) -> None:
        captured: dict[str, object] = {}

        class _GatewayHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
                raw_length = self.headers.get("Content-Length", "0")
                content_length = int(raw_length)
                body = self.rfile.read(content_length)
                captured["path"] = self.path
                captured["headers"] = {key: value for key, value in self.headers.items()}
                captured["body"] = json.loads(body.decode("utf-8"))

                response = json.dumps(
                    {
                        "data": {
                            "items": [
                                {
                                    "polygon": [[0, 0], [10, 0], [10, 8], [0, 8]],
                                    "text": "Gateway text",
                                    "score": 0.93,
                                }
                            ]
                        },
                        "elapsed_seconds": 0.21,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args) -> None:  # noqa: A003 - stdlib signature
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with patch.dict(os.environ, {"PARSECORE_OCR_API_KEY": "test-key"}, clear=True):
                settings = OcrProviderSettings(
                    enabled=True,
                    provider="remote-http",
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    api_key_env="PARSECORE_OCR_API_KEY",
                    timeout_seconds=5.0,
                    max_retries=0,
                    options={
                        "endpoint_path": "/ocr/v1",
                        "headers": {"X-OCR-Tenant": "tenant-a"},
                        "det_use_dilation": True,
                    },
                )

                with tempfile.TemporaryDirectory() as tmp:
                    image_path = Path(tmp) / "sample.png"
                    image_path.write_bytes(b"fake-image")
                    engine = build_ocr_engine(settings)
                    result, elapsed = engine(str(image_path))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

        self.assertEqual(captured["path"], "/ocr/v1")
        headers = captured["headers"]
        assert isinstance(headers, dict)
        normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
        self.assertEqual(normalized_headers["authorization"], "Bearer test-key")
        self.assertEqual(normalized_headers["x-ocr-tenant"], "tenant-a")
        self.assertEqual(normalized_headers["content-type"], "application/json")

        payload = captured["body"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["mime_type"], "image/png")
        self.assertEqual(payload["file_name"], "sample.png")
        self.assertEqual(payload["options"], {"det_use_dilation": True})
        self.assertEqual(base64.b64decode(payload["image_base64"]), b"fake-image")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], "Gateway text")
        self.assertAlmostEqual(result[0][2], 0.93)
        self.assertAlmostEqual(elapsed, 0.21)


if __name__ == "__main__":
    unittest.main()