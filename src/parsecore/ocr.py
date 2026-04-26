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

        options, adapter_options = _split_rapidocr_options(settings.options)
        inner = RapidOCR(**options) if options else RapidOCR()
        return RapidOcrEngineAdapter(inner, options=adapter_options)
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


def _normalize_rapidocr_options(options: Any) -> dict[str, Any]:
    if not isinstance(options, Mapping):
        return {}

    normalized = {str(key): value for key, value in options.items()}
    required_model_keys = {
        "det_": "det_model_path",
        "cls_": "cls_model_path",
        "rec_": "rec_model_path",
    }
    for prefix, model_key in required_model_keys.items():
        if any(key.startswith(prefix) and key != model_key for key in normalized):
            normalized.setdefault(model_key, "")
    return normalized


def _split_rapidocr_options(options: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(options, Mapping):
        return {}, {}

    rapidocr_options: dict[str, Any] = {}
    adapter_options: dict[str, Any] = {}
    for key, value in options.items():
        normalized_key = str(key)
        if normalized_key.startswith("parsecore_"):
            adapter_options[normalized_key] = value
        else:
            rapidocr_options[normalized_key] = value
    return _normalize_rapidocr_options(rapidocr_options), adapter_options


def _coerce_non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _select_evenly_spaced_indexes(total: int, sample_size: int) -> tuple[int, ...]:
    if total <= 0 or sample_size <= 0:
        return ()
    if sample_size >= total:
        return tuple(range(total))
    if sample_size == 1:
        return (0,)

    indexes = {
        int(round(position * (total - 1) / (sample_size - 1)))
        for position in range(sample_size)
    }
    if len(indexes) < sample_size:
        for index in range(total):
            indexes.add(index)
            if len(indexes) >= sample_size:
                break
    return tuple(sorted(indexes))


def _count_cls_hits(cls_res: list[list[Any]] | list[tuple[Any, Any]], *, cls_thresh: float) -> tuple[int, int]:
    rotate_positive_count = 0
    rotate_high_count = 0
    for label, score in cls_res:
        if "180" not in str(label):
            continue
        rotate_positive_count += 1
        try:
            score_value = float(score)
        except (TypeError, ValueError):
            score_value = 0.0
        if score_value > cls_thresh:
            rotate_high_count += 1
    return rotate_positive_count, rotate_high_count


def _build_rapidocr_provider_metrics(
    *,
    det_elapsed_s: float,
    cls_elapsed_s: float,
    rec_elapsed_s: float,
    crop_count: int,
    cls_rotate_positive_count: int,
    cls_rotate_high_count: int,
) -> dict[str, float | int]:
    return {
        "elapsed": round(det_elapsed_s + cls_elapsed_s + rec_elapsed_s, 6),
        "det_elapsed_s": round(det_elapsed_s, 6),
        "cls_elapsed_s": round(cls_elapsed_s, 6),
        "rec_elapsed_s": round(rec_elapsed_s, 6),
        "crop_count": int(crop_count),
        "cls_rotate_positive_count": int(cls_rotate_positive_count),
        "cls_rotate_high_count": int(cls_rotate_high_count),
    }


class RapidOcrEngineAdapter:
    _parsecore_rapidocr = True

    def __init__(self, engine: Any, *, options: Mapping[str, Any] | None = None) -> None:
        self._engine = engine
        adapter_options = dict(options or {})
        self._angle_cls_probe_crops = _coerce_non_negative_int(
            adapter_options.get("parsecore_angle_cls_probe_crops"),
            default=0,
        )
        self._angle_cls_probe_min_crops = _coerce_non_negative_int(
            adapter_options.get("parsecore_angle_cls_probe_min_crops"),
            default=0,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    def _should_probe_angle_cls(self, crop_count: int) -> bool:
        if self._angle_cls_probe_crops <= 0:
            return False
        if crop_count <= self._angle_cls_probe_crops:
            return False
        return crop_count >= max(self._angle_cls_probe_min_crops, self._angle_cls_probe_crops + 1)

    def _run_angle_cls(
        self,
        img_crop_list: list[Any],
    ) -> tuple[list[Any], float, int, int]:
        cls_thresh = float(getattr(self._engine.text_cls, "cls_thresh", 0.0))
        crop_count = len(img_crop_list)
        if not self._should_probe_angle_cls(crop_count):
            classified_img_crop_list, cls_res, cls_elapsed_s = self._engine.text_cls(img_crop_list)
            rotate_positive_count, rotate_high_count = _count_cls_hits(
                cls_res,
                cls_thresh=cls_thresh,
            )
            return list(classified_img_crop_list), float(cls_elapsed_s or 0.0), rotate_positive_count, rotate_high_count

        sampled_indexes = _select_evenly_spaced_indexes(crop_count, self._angle_cls_probe_crops)
        sampled_set = set(sampled_indexes)
        working_img_crop_list = list(img_crop_list)

        sampled_crops = [working_img_crop_list[index] for index in sampled_indexes]
        sampled_crops, sampled_cls_res, sampled_cls_elapsed_s = self._engine.text_cls(sampled_crops)
        for index, sampled_crop in zip(sampled_indexes, sampled_crops):
            working_img_crop_list[index] = sampled_crop
        rotate_positive_count, rotate_high_count = _count_cls_hits(
            sampled_cls_res,
            cls_thresh=cls_thresh,
        )
        total_cls_elapsed_s = float(sampled_cls_elapsed_s or 0.0)
        if rotate_positive_count <= 0:
            return working_img_crop_list, total_cls_elapsed_s, rotate_positive_count, rotate_high_count

        remaining_indexes = [index for index in range(crop_count) if index not in sampled_set]
        if not remaining_indexes:
            return working_img_crop_list, total_cls_elapsed_s, rotate_positive_count, rotate_high_count

        remaining_crops = [working_img_crop_list[index] for index in remaining_indexes]
        remaining_crops, remaining_cls_res, remaining_cls_elapsed_s = self._engine.text_cls(remaining_crops)
        for index, remaining_crop in zip(remaining_indexes, remaining_crops):
            working_img_crop_list[index] = remaining_crop
        remaining_positive_count, remaining_high_count = _count_cls_hits(
            remaining_cls_res,
            cls_thresh=cls_thresh,
        )
        return (
            working_img_crop_list,
            total_cls_elapsed_s + float(remaining_cls_elapsed_s or 0.0),
            rotate_positive_count + remaining_positive_count,
            rotate_high_count + remaining_high_count,
        )

    def __call__(self, source: Any) -> tuple[list[list[Any]] | None, dict[str, float | int]]:
        img = self._engine.load_img(source)
        h, w = img.shape[:2]
        if self._engine.width_height_ratio == -1:
            use_limit_ratio = False
        else:
            use_limit_ratio = w / h > self._engine.width_height_ratio

        det_elapsed_s = 0.0
        cls_elapsed_s = 0.0
        rec_elapsed_s = 0.0
        crop_count = 0
        cls_rotate_positive_count = 0
        cls_rotate_high_count = 0

        if not self._engine.use_text_det or h <= self._engine.min_height or use_limit_ratio:
            dt_boxes, img_crop_list = self._engine.get_boxes_img_without_det(img, h, w)
        else:
            dt_boxes, det_elapsed_s = self._engine.text_detector(img)
            if dt_boxes is None or len(dt_boxes) < 1:
                return None, _build_rapidocr_provider_metrics(
                    det_elapsed_s=float(det_elapsed_s or 0.0),
                    cls_elapsed_s=0.0,
                    rec_elapsed_s=0.0,
                    crop_count=0,
                    cls_rotate_positive_count=0,
                    cls_rotate_high_count=0,
                )

            if self._engine.print_verbose:
                print(f"dt_boxes num: {len(dt_boxes)}, elapse: {det_elapsed_s}")

            dt_boxes = self._engine.sorted_boxes(dt_boxes)
            img_crop_list = self._engine.get_crop_img_list(img, dt_boxes)

        crop_count = len(img_crop_list)
        if self._engine.use_angle_cls:
            (
                img_crop_list,
                cls_elapsed_s,
                cls_rotate_positive_count,
                cls_rotate_high_count,
            ) = self._run_angle_cls(img_crop_list)

            if self._engine.print_verbose:
                print(f"cls num: {len(img_crop_list)}, elapse: {cls_elapsed_s}")

        rec_res, rec_elapsed_s = self._engine.text_recognizer(img_crop_list)
        if self._engine.print_verbose:
            print(f"rec_res num: {len(rec_res)}, elapse: {rec_elapsed_s}")

        filter_boxes, filter_rec_res = self._engine.filter_boxes_rec_by_score(dt_boxes, rec_res)
        fina_result = [
            [dt.tolist() if hasattr(dt, "tolist") else dt, rec[0], str(rec[1])]
            for dt, rec in zip(filter_boxes, filter_rec_res)
        ]
        provider_metrics = _build_rapidocr_provider_metrics(
            det_elapsed_s=float(det_elapsed_s or 0.0),
            cls_elapsed_s=float(cls_elapsed_s or 0.0),
            rec_elapsed_s=float(rec_elapsed_s or 0.0),
            crop_count=crop_count,
            cls_rotate_positive_count=cls_rotate_positive_count,
            cls_rotate_high_count=cls_rotate_high_count,
        )
        if fina_result:
            return fina_result, provider_metrics
        return None, provider_metrics


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
    "RapidOcrEngineAdapter",
    "RemoteHttpOcrEngine",
    "build_ocr_engine",
    "is_ocr_provider_available",
]