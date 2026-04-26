from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
from importlib.util import find_spec
from typing import Any, Mapping
import urllib.error
import urllib.request

from .config import OcrProviderSettings


class OcrConfigurationError(RuntimeError):
    pass


class OcrRequestError(RuntimeError):
    pass


def _normalized_provider_name(provider: str) -> str:
    normalized = (provider or "rapidocr").strip().lower()
    return normalized or "rapidocr"


def is_ocr_provider_available(settings: OcrProviderSettings) -> bool:
    if not settings.enabled:
        return False
    provider = _normalized_provider_name(settings.provider)
    if provider in {"rapidocr", "rapidocr-onnxruntime", "rapidocr_onnxruntime"}:
        return find_spec("rapidocr_onnxruntime") is not None
    if provider in {"http", "http-json", "remote-http", "remote-http-json"}:
        if not settings.base_url:
            return False
        if not settings.api_key_env:
            return True
        return bool(os.environ.get(settings.api_key_env, "").strip())
    return False


def build_ocr_engine(settings: OcrProviderSettings) -> Any:
    if not settings.enabled:
        raise OcrConfigurationError(
            "OCR provider is disabled; set providers.ocr.enabled = true to use image OCR"
        )

    provider = _normalized_provider_name(settings.provider)
    if provider in {"rapidocr", "rapidocr-onnxruntime", "rapidocr_onnxruntime"}:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "rapidocr_onnxruntime is required for OCR provider 'rapidocr'; "
                "install via `pip install rapidocr_onnxruntime`"
            ) from exc

        options = dict(settings.options)
        return RapidOCR(**options) if options else RapidOCR()
    if provider in {"http", "http-json", "remote-http", "remote-http-json"}:
        return RemoteHttpOcrEngine(settings)

    raise OcrConfigurationError(f"Unsupported OCR provider: {settings.provider!r}")


class RemoteHttpOcrEngine:
    def __init__(self, settings: OcrProviderSettings) -> None:
        if not settings.base_url:
            raise OcrConfigurationError(
                "providers.ocr.base_url is required when provider is 'remote-http'"
            )
        self._settings = settings
        self._api_key = ""
        if settings.api_key_env:
            self._api_key = os.environ.get(settings.api_key_env, "").strip()
            if not self._api_key:
                raise OcrConfigurationError(
                    f"environment variable {settings.api_key_env} is empty; cannot call OCR provider"
                )

    def __call__(self, source: Any) -> tuple[list[tuple[Any, str, float]], float]:
        image_bytes, mime_type, file_name = _serialize_ocr_source(source)
        transport_options, provider_options = _split_ocr_options(self._settings.options)
        endpoint = self._settings.base_url.rstrip("/") + str(
            transport_options.get("endpoint_path", "/ocr")
        )
        payload: dict[str, Any] = {
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            "mime_type": mime_type,
        }
        if file_name:
            payload["file_name"] = file_name
        if provider_options:
            payload["options"] = provider_options

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        extra_headers = transport_options.get("headers")
        if isinstance(extra_headers, Mapping):
            headers.update({str(key): str(value) for key, value in extra_headers.items()})

        last_error: Exception | None = None
        attempts = max(1, int(self._settings.max_retries) + 1)
        for _attempt in range(attempts):
            request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(  # noqa: S310 - explicit https endpoint
                    request, timeout=self._settings.timeout_seconds
                ) as response:
                    raw = response.read().decode("utf-8")
                payload = json.loads(raw)
                return _normalize_remote_ocr_response(payload)
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                OcrRequestError,
                ValueError,
            ) as exc:
                last_error = exc
                continue
        raise OcrRequestError(f"OCR call failed after {attempts} attempts: {last_error}")


def _split_ocr_options(options: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(options, Mapping):
        return {}, {}
    transport: dict[str, Any] = {}
    provider_options: dict[str, Any] = {}
    for key, value in options.items():
        normalized_key = str(key)
        if normalized_key in {"endpoint_path", "headers"}:
            transport[normalized_key] = value
        else:
            provider_options[normalized_key] = value
    return transport, provider_options


def _serialize_ocr_source(source: Any) -> tuple[bytes, str, str | None]:
    if isinstance(source, str):
        with open(source, "rb") as handle:
            data = handle.read()
        mime_type, _encoding = mimetypes.guess_type(source)
        file_name = os.path.basename(source) or None
        return data, mime_type or "application/octet-stream", file_name

    try:
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise OcrConfigurationError(
            "Pillow and numpy are required to serialize in-memory OCR images for remote-http provider"
        ) from exc

    image = Image.fromarray(np.asarray(source))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), "image/png", None


def _normalize_remote_ocr_response(
    payload: dict[str, Any],
) -> tuple[list[tuple[Any, str, float]], float]:
    regions = payload.get("result")
    if regions is None:
        regions = payload.get("results")
    if regions is None:
        data = payload.get("data")
        if isinstance(data, dict):
            regions = data.get("result") or data.get("results") or data.get("items")
        else:
            regions = data
    if not isinstance(regions, list):
        raise OcrRequestError("OCR response missing result list")

    normalized: list[tuple[Any, str, float]] = []
    for entry in regions:
        if isinstance(entry, dict):
            box = entry.get("bbox") or entry.get("box") or entry.get("polygon")
            text = entry.get("text")
            confidence = entry.get("confidence", entry.get("score", 0.0))
            if box is None or not isinstance(text, str):
                continue
            normalized.append((box, text, float(confidence)))
            continue
        try:
            box, text, confidence = entry
        except (TypeError, ValueError):
            continue
        if not isinstance(text, str):
            continue
        normalized.append((box, text, float(confidence)))

    elapsed_raw = payload.get("elapsed", payload.get("elapsed_seconds", 0.0))
    try:
        elapsed = float(elapsed_raw)
    except (TypeError, ValueError):
        elapsed = 0.0
    return normalized, elapsed


__all__ = [
    "OcrConfigurationError",
    "OcrRequestError",
    "RemoteHttpOcrEngine",
    "build_ocr_engine",
    "is_ocr_provider_available",
]