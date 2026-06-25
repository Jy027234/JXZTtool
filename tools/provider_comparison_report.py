"""Compare local ParseCore providers on fixed samples using IR/coverage output."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import tracemalloc
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.api_payloads import _document_projection, _document_providers_projection  # noqa: E402
from parsecore.config import (  # noqa: E402
    OcrProviderSettings,
    load_settings,
    local_provider_registry_payload,
    local_provider_route_plan_payload,
)
from parsecore.models import Block, BlockType, ParseJob, ParseJobState, ParseRequest  # noqa: E402
from parsecore.parsers import build_parser  # noqa: E402
from parsecore.pdf_parts import create_pdf_part_file  # noqa: E402
from parsecore.stubs import FakeEmbeddingProvider, ParagraphChunkBuilder  # noqa: E402


SCHEMA_VERSION = "2026-06-provider-comparison-report"
REMOTE_OCR_PROVIDERS = {"remote-http", "http-json"}
FIXTURE_ROOT_ENV = "PARSECORE_REGRESSION_FIXTURE_ROOT"
DEFAULT_ADMISSION_GATE_CHECKS = ["samples", "license", "performance", "observability"]
MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
}


@dataclass(frozen=True)
class SampleSpec:
    path: Path
    name: str | None = None
    profile: str | None = None
    providers: tuple[str, ...] = ()
    source: str = "sample"
    page_start: int | None = None
    page_end: int | None = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare local ParseCore providers on fixed samples")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--sample", action="append", default=[], help="Sample file; can be repeated")
    parser.add_argument("--suite", help="Suite JSON listing samples, fixtures, cases, or baseline entries")
    parser.add_argument(
        "--fixture-root",
        help=f"Portable fixture root for fixture_relative_path entries (defaults to env {FIXTURE_ROOT_ENV})",
    )
    parser.add_argument("--provider", action="append", help="Provider/parser id; can be repeated")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--page-start", type=int, help="Optional 1-based PDF page start for all direct --sample inputs")
    parser.add_argument("--page-end", type=int, help="Optional 1-based PDF page end for all direct --sample inputs")
    parser.add_argument("--out-json", help="Optional JSON output path")
    parser.add_argument("--out-md", help="Optional Markdown output path")
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Write per-sample progress messages to stderr without changing JSON stdout",
    )
    return parser


def _round(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _media_type_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in MEDIA_TYPES:
        return MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed


def _provider_ids(value: Sequence[str] | None) -> list[str]:
    result: list[str] = []
    source: Sequence[str]
    if isinstance(value, str):
        source = [value]
    else:
        source = value or []
    for item in source:
        for part in str(item or "").split(","):
            normalized = part.strip()
            if normalized and normalized not in result:
                result.append(normalized)
    return result


def _resolve_path(base_dir: Path, raw_path: str | Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(raw_path or "").strip()))
    path = Path(expanded)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    number = int(value)
    if number <= 0:
        raise ValueError("invalid_page_range")
    return number


def _page_range(
    raw: Any = None,
    *,
    page_start: Any = None,
    page_end: Any = None,
) -> tuple[int, int] | None:
    start = _optional_positive_int(page_start)
    end = _optional_positive_int(page_end)
    if raw is not None:
        if isinstance(raw, Mapping):
            start = _optional_positive_int(raw.get("start", raw.get("page_start", start)))
            end = _optional_positive_int(raw.get("end", raw.get("page_end", end)))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            start = _optional_positive_int(raw[0])
            end = _optional_positive_int(raw[1])
        else:
            raise ValueError("invalid_page_range")
    if start is None and end is None:
        return None
    if start is None:
        start = end
    if end is None:
        end = start
    if start is None or end is None or start > end:
        raise ValueError("invalid_page_range")
    return start, end


def _page_range_payload(page_range: tuple[int, int] | None) -> dict[str, int] | None:
    if page_range is None:
        return None
    return {"start": page_range[0], "end": page_range[1]}


def _fixture_root_path(raw_root: str | Path | None, *, env_name: str = FIXTURE_ROOT_ENV) -> Path | None:
    configured = str(raw_root or "").strip() or os.environ.get(env_name, "")
    expanded = os.path.expandvars(os.path.expanduser(configured.strip()))
    if not expanded:
        return None
    return Path(expanded)


def _entry_provider_ids(entry: dict[str, Any]) -> tuple[str, ...]:
    raw = entry.get("providers")
    if raw is None:
        raw = entry.get("provider_ids")
    if raw is None:
        raw = entry.get("provider")
    return tuple(_provider_ids(raw))


def _entry_profile(entry: dict[str, Any]) -> str | None:
    value = str(entry.get("profile") or "").strip()
    return value or None


def _entry_page_range(entry: dict[str, Any]) -> tuple[int, int] | None:
    return _page_range(
        entry.get("page_range"),
        page_start=entry.get("page_start"),
        page_end=entry.get("page_end"),
    )


def _entry_path_value(entry: dict[str, Any]) -> str:
    for key in (
        "path",
        "file_path",
        "fixture",
        "fixture_path",
        "document",
        "pdf",
        "sample",
    ):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return ""


def _resolve_sample_entry_path(
    entry: dict[str, Any],
    *,
    base_dir: Path,
    fixture_root: Path | None,
) -> Path:
    raw_path = _entry_path_value(entry)
    relative_path = str(entry.get("fixture_relative_path") or "").strip()
    if relative_path and fixture_root is not None:
        candidate = (fixture_root / Path(relative_path)).resolve()
        if candidate.exists() or not raw_path:
            return candidate
    if raw_path:
        path = _resolve_path(base_dir, raw_path)
        if path.exists() or fixture_root is None:
            return path
        root_candidate = (fixture_root / Path(raw_path).name).resolve()
        if root_candidate.exists():
            return root_candidate
        return path
    if relative_path:
        return _resolve_path(base_dir, relative_path)
    raise ValueError(f"Suite sample entry is missing a path: {entry}")


def _sample_spec_from_entry(
    entry: dict[str, Any],
    *,
    base_dir: Path,
    fixture_root: Path | None,
    source: str,
    default_name: str | None = None,
) -> SampleSpec:
    path = _resolve_sample_entry_path(entry, base_dir=base_dir, fixture_root=fixture_root)
    name = str(entry.get("name") or entry.get("fixture_name") or default_name or "").strip()
    return SampleSpec(
        path=path,
        name=name or None,
        profile=_entry_profile(entry),
        providers=_entry_provider_ids(entry),
        source=source,
        page_start=(page_range[0] if (page_range := _entry_page_range(entry)) else None),
        page_end=(page_range[1] if page_range else None),
    )


def _suite_entries(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Suite JSON must be an object or list")
    for key in ("samples", "fixtures", "cases", "entries"):
        entries = payload.get(key)
        if entries is not None:
            if not isinstance(entries, list):
                raise ValueError(f"Suite field {key!r} must be a list")
            return entries
    return []


def _load_baseline_fixture_specs(
    entry: dict[str, Any],
    *,
    suite_dir: Path,
    fixture_root: Path | None,
) -> list[SampleSpec]:
    baseline_path = _resolve_path(suite_dir, str(entry.get("baseline") or ""))
    baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_fixture_root = fixture_root or _fixture_root_path(
        None,
        env_name=str(baseline_payload.get("fixture_root_env") or FIXTURE_ROOT_ENV),
    )
    fixtures = list(baseline_payload.get("fixtures") or [])
    specs: list[SampleSpec] = []
    for index, fixture in enumerate(fixtures, start=1):
        if isinstance(fixture, str):
            fixture_entry: dict[str, Any] = {"fixture": fixture}
        elif isinstance(fixture, dict):
            fixture_entry = dict(fixture)
        else:
            continue
        if entry.get("providers") is not None and fixture_entry.get("providers") is None:
            fixture_entry["providers"] = entry.get("providers")
        if entry.get("provider_ids") is not None and fixture_entry.get("provider_ids") is None:
            fixture_entry["provider_ids"] = entry.get("provider_ids")
        if entry.get("provider") is not None and fixture_entry.get("provider") is None:
            fixture_entry["provider"] = entry.get("provider")
        if entry.get("profile") is not None and fixture_entry.get("profile") is None:
            fixture_entry["profile"] = entry.get("profile")
        if entry.get("page_range") is not None and fixture_entry.get("page_range") is None:
            fixture_entry["page_range"] = entry.get("page_range")
        if entry.get("page_start") is not None and fixture_entry.get("page_start") is None:
            fixture_entry["page_start"] = entry.get("page_start")
        if entry.get("page_end") is not None and fixture_entry.get("page_end") is None:
            fixture_entry["page_end"] = entry.get("page_end")
        default_name = str(entry.get("name") or baseline_path.stem)
        if len(fixtures) > 1:
            default_name = f"{default_name}:{index}"
        specs.append(
            _sample_spec_from_entry(
                fixture_entry,
                base_dir=baseline_path.parent,
                fixture_root=baseline_fixture_root,
                source=f"baseline:{baseline_path.name}",
                default_name=default_name,
            )
        )
    return specs


def _load_suite_samples(
    suite: str | Path,
    *,
    fixture_root: Path | None,
) -> tuple[list[SampleSpec], dict[str, Any]]:
    suite_path = Path(suite)
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    specs: list[SampleSpec] = []
    for index, item in enumerate(_suite_entries(payload), start=1):
        if isinstance(item, str):
            specs.append(
                SampleSpec(
                    path=_resolve_path(suite_path.parent, item),
                    name=Path(item).name,
                    source=f"suite:{suite_path.name}",
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        if item.get("disabled"):
            continue
        if item.get("baseline") and not _entry_path_value(item) and not item.get("fixture_relative_path"):
            specs.extend(
                _load_baseline_fixture_specs(
                    item,
                    suite_dir=suite_path.parent,
                    fixture_root=fixture_root,
                )
            )
            continue
        specs.append(
            _sample_spec_from_entry(
                item,
                base_dir=suite_path.parent,
                fixture_root=fixture_root,
                source=f"suite:{suite_path.name}",
                default_name=f"sample-{index}",
            )
        )
    return specs, _gate_policy(payload.get("gate_policy"))


def _gate_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    policy: dict[str, Any] = {}

    def _non_negative_limit(raw: Any) -> int:
        return max(0, int(raw))

    max_reading_order_warning_runs = value.get("max_provider_reading_order_warning_runs")
    if max_reading_order_warning_runs is not None:
        policy["max_provider_reading_order_warning_runs"] = _non_negative_limit(max_reading_order_warning_runs)
    max_quality_warning_runs = value.get("max_provider_quality_warning_runs")
    if max_quality_warning_runs is not None:
        policy["max_provider_quality_warning_runs"] = _non_negative_limit(max_quality_warning_runs)
    max_route_primary_mismatches = value.get("max_samples_best_provider_differs_from_route_primary")
    if max_route_primary_mismatches is not None:
        policy["max_samples_best_provider_differs_from_route_primary"] = _non_negative_limit(
            max_route_primary_mismatches
        )
    max_provider_version_drift = value.get("max_providers_with_multiple_provider_versions")
    if max_provider_version_drift is not None:
        policy["max_providers_with_multiple_provider_versions"] = _non_negative_limit(
            max_provider_version_drift
        )
    max_adapter_version_drift = value.get("max_providers_with_multiple_adapter_versions")
    if max_adapter_version_drift is not None:
        policy["max_providers_with_multiple_adapter_versions"] = _non_negative_limit(
            max_adapter_version_drift
        )
    max_providers_requiring_config_update = value.get("max_providers_requiring_config_update")
    if max_providers_requiring_config_update is not None:
        policy["max_providers_requiring_config_update"] = _non_negative_limit(
            max_providers_requiring_config_update
        )
    max_route_mode_drift = value.get("max_providers_with_route_mode_drift")
    if max_route_mode_drift is not None:
        policy["max_providers_with_route_mode_drift"] = _non_negative_limit(max_route_mode_drift)
    max_gate_status_drift = value.get("max_providers_with_gate_status_drift")
    if max_gate_status_drift is not None:
        policy["max_providers_with_gate_status_drift"] = _non_negative_limit(max_gate_status_drift)
    max_gate_checks_drift = value.get("max_providers_with_gate_checks_drift")
    if max_gate_checks_drift is not None:
        policy["max_providers_with_gate_checks_drift"] = _non_negative_limit(max_gate_checks_drift)
    max_route_ready_drift = value.get("max_providers_with_route_ready_drift")
    if max_route_ready_drift is not None:
        policy["max_providers_with_route_ready_drift"] = _non_negative_limit(max_route_ready_drift)
    return policy


def _local_ocr_settings(settings: Any) -> OcrProviderSettings:
    ocr = settings.providers.ocr
    provider = str(ocr.provider or "").strip().lower()
    if provider not in REMOTE_OCR_PROVIDERS:
        return ocr
    return OcrProviderSettings(
        enabled=False,
        provider=ocr.provider,
        base_url="",
        api_key_env="",
        timeout_seconds=ocr.timeout_seconds,
        max_retries=0,
        options=ocr.options,
    )


def _provider_ids_from_route_plan(route_plan: dict[str, Any]) -> list[str]:
    selection = route_plan.get("selection") if isinstance(route_plan.get("selection"), dict) else {}
    ids = [
        str(provider_id)
        for provider_id in selection.get("eligible_provider_ids", [])
        if str(provider_id)
    ]
    for candidate in route_plan.get("candidates", []) or []:
        if not isinstance(candidate, dict):
            continue
        provider_id = str(candidate.get("id") or "")
        if provider_id and provider_id not in ids:
            ids.append(provider_id)
    return ids


def _candidate_provider_ids(
    *,
    settings: Any,
    path: Path,
    profile: str,
    explicit: Sequence[str] | None,
    route_plan: dict[str, Any] | None = None,
) -> list[str]:
    requested = _provider_ids(explicit)
    if requested:
        return requested
    media_type = _media_type_for(path)
    resolved_route_plan = route_plan or local_provider_route_plan_payload(
        settings.providers,
        media_type=media_type,
        file_name=path.name,
        profile=profile,
        include_disabled=True,
    )
    candidates = _provider_ids_from_route_plan(resolved_route_plan)
    if candidates:
        return list(dict.fromkeys(candidates))
    return [
        str(parser.name)
        for parser in settings.parsers
        if path.suffix.lower() in {extension.lower() for extension in parser.extensions}
        or (media_type or "").lower() in {item.lower() for item in parser.media_types}
    ]


def _parser_settings_by_name(settings: Any) -> dict[str, Any]:
    return {str(parser.name): parser for parser in settings.parsers}


def _parse_job(*, doc_id: str, path: Path, media_type: str | None, profile: str) -> ParseJob:
    return _parse_job_with_page_range(
        doc_id=doc_id,
        path=path,
        media_type=media_type,
        profile=profile,
        page_range=None,
    )


def _parse_job_with_page_range(
    *,
    doc_id: str,
    path: Path,
    media_type: str | None,
    profile: str,
    page_range: tuple[int, int] | None,
) -> ParseJob:
    options = {"profile": profile, "requested_profile": profile}
    if page_range is not None:
        options.update(
            {
                "page_start": page_range[0],
                "page_end": page_range[1],
                "page_count": page_range[1] - page_range[0] + 1,
                "page_offset": page_range[0] - 1,
            }
        )
    return ParseJob(
        job_id=f"provider-compare-{doc_id}",
        doc_id=doc_id,
        file_path=str(path),
        state=ParseJobState.DONE,
        media_type=media_type,
        options=options,
        tenant_id="provider-comparison",
        quota_key="compare",
    )


def _annotate_first_block(
    blocks: Sequence[Block],
    *,
    elapsed_s: float,
    peak_kb: float,
) -> tuple[Block, ...]:
    if not blocks:
        return tuple()
    result = list(blocks)
    first = result[0]
    metadata = dict(first.metadata or {})
    metadata.setdefault("provider_elapsed_s", elapsed_s)
    metadata.setdefault("peak_kb", peak_kb)
    result[0] = replace(first, metadata=metadata)
    return tuple(result)


def _shift_page_range_value(value: Any, *, page_offset: int) -> Any:
    if isinstance(value, Mapping):
        start = _optional_positive_int(value.get("start", value.get("page_start")))
        end = _optional_positive_int(value.get("end", value.get("page_end")))
        if start is None:
            return value
        if end is None:
            end = start
        return {"start": start + page_offset, "end": end + page_offset}
    if isinstance(value, list) and len(value) >= 2:
        start = _optional_positive_int(value[0])
        end = _optional_positive_int(value[1])
        if start is None:
            return value
        if end is None:
            end = start
        return [start + page_offset, end + page_offset]
    if isinstance(value, tuple) and len(value) >= 2:
        start = _optional_positive_int(value[0])
        end = _optional_positive_int(value[1])
        if start is None:
            return value
        if end is None:
            end = start
        return (start + page_offset, end + page_offset)
    return value


def _normalize_page_range_blocks(
    *,
    blocks: Sequence[Block],
    doc_id: str,
    page_range: tuple[int, int],
    source_path: Path,
) -> tuple[Block, ...]:
    page_offset = page_range[0] - 1
    range_tag = f"pages-{page_range[0]}-{page_range[1]}"
    normalized: list[Block] = []
    for index, block in enumerate(blocks, start=1):
        metadata = dict(block.metadata or {})
        if metadata.get("page") is not None:
            metadata["page"] = _optional_positive_int(metadata.get("page")) + page_offset
        if metadata.get("page_start") is not None:
            metadata["page_start"] = _optional_positive_int(metadata.get("page_start")) + page_offset
        if metadata.get("page_end") is not None:
            metadata["page_end"] = _optional_positive_int(metadata.get("page_end")) + page_offset
        if metadata.get("page_span") is not None:
            metadata["page_span"] = _shift_page_range_value(metadata.get("page_span"), page_offset=page_offset)
        metadata["source_page_range"] = {"start": page_range[0], "end": page_range[1]}
        metadata["source_document_path"] = str(source_path)
        metadata["page_offset"] = page_offset
        old_id = str(block.block_id or f"blk-{index}")
        normalized.append(
            replace(
                block,
                block_id=f"{range_tag}:{old_id}",
                doc_id=doc_id,
                metadata=metadata,
            )
        )
    return tuple(normalized)


def _materialize_sample_input(
    *,
    source_path: Path,
    page_range: tuple[int, int] | None,
    temp_dir: Path,
    sample_index: int,
) -> Path:
    if page_range is None:
        return source_path
    if source_path.suffix.lower() != ".pdf":
        raise ValueError("page_range_requires_pdf")
    target = temp_dir / f"{sample_index:03d}-{source_path.stem}-pages-{page_range[0]}-{page_range[1]}.pdf"
    create_pdf_part_file(str(source_path), str(target), page_range[0], page_range[1])
    return target


def _snapshot(
    *,
    settings: Any,
    job: ParseJob,
    blocks: Sequence[Block],
    chunks: Sequence[Any],
) -> dict[str, Any]:
    return {
        "job": job,
        "doc_id": job.doc_id,
        "blocks": tuple(blocks),
        "chunks": tuple(chunks),
        "provider_registry": local_provider_registry_payload(settings.providers),
    }


def _run_provider(
    *,
    settings: Any,
    parser_settings: dict[str, Any],
    provider_id: str,
    path: Path,
    parse_path: Path,
    media_type: str | None,
    profile: str,
    sample_index: int,
    page_range: tuple[int, int] | None,
) -> dict[str, Any]:
    parser_config = parser_settings.get(provider_id)
    if parser_config is None:
        return {
            "provider_id": provider_id,
            "status": "skipped",
            "reason": "parser_not_configured",
        }
    parser = build_parser(
        parser_config.name,
        media_types=parser_config.media_types,
        extensions=parser_config.extensions,
        options=parser_config.options,
        ocr_provider_settings=_local_ocr_settings(settings),
    )
    if not parser.supports(media_type=media_type, suffix=path.suffix):
        return {
            "provider_id": provider_id,
            "status": "skipped",
            "reason": "unsupported_media_type_or_extension",
            "media_type": media_type,
            "suffix": path.suffix.lower(),
        }

    doc_id = f"compare-{sample_index}-{provider_id.replace('.', '-').replace('_', '-')}"
    request = ParseRequest(
        doc_id=doc_id,
        file_path=str(parse_path),
        media_type=media_type,
        options={"profile": profile},
        tenant_id="provider-comparison",
        quota_key="compare",
    )
    started = time.perf_counter()
    tracemalloc.start()
    try:
        raw_blocks = tuple(parser.parse(request))
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    except Exception as exc:
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        elapsed_s = _round(time.perf_counter() - started)
        tracemalloc.stop()
        return {
            "provider_id": provider_id,
            "status": "failed",
            "elapsed_s": elapsed_s,
            "peak_kb": _round(peak_bytes / 1024),
            "error": str(exc),
        }
    elapsed_s = _round(time.perf_counter() - started)
    tracemalloc.stop()
    peak_kb = _round(peak_bytes / 1024)
    normalized_blocks = raw_blocks
    if page_range is not None:
        normalized_blocks = _normalize_page_range_blocks(
            blocks=normalized_blocks,
            doc_id=doc_id,
            page_range=page_range,
            source_path=path,
        )
    blocks = _annotate_first_block(normalized_blocks, elapsed_s=elapsed_s, peak_kb=peak_kb)
    raw_chunks = tuple(ParagraphChunkBuilder().build(doc_id=doc_id, blocks=blocks))
    chunks = tuple(FakeEmbeddingProvider().embed(doc_id=doc_id, chunks=raw_chunks))
    job = _parse_job_with_page_range(
        doc_id=doc_id,
        path=path,
        media_type=media_type,
        profile=profile,
        page_range=page_range,
    )
    snapshot = _snapshot(settings=settings, job=job, blocks=blocks, chunks=chunks)
    ir = _document_projection(snapshot, projection="ir")
    coverage = _document_projection(snapshot, projection="coverage")
    provider_report = _document_providers_projection(snapshot)
    provider_identity = _provider_identity_payload(
        provider_id=provider_id,
        provider_report=provider_report,
        ir=ir,
    )
    rankings = (provider_report.get("comparison_report") or {}).get("rankings") or []
    ranking = rankings[0] if rankings else {}
    table_blocks = [block for block in blocks if block.type == BlockType.TABLE]
    return {
        "provider_id": provider_id,
        "provider_version": provider_identity.get("provider_version"),
        "adapter_version": provider_identity.get("adapter_version"),
        "status": "done",
        "elapsed_s": elapsed_s,
        "peak_kb": peak_kb,
        "blocks": len(blocks),
        "chunks": len(chunks),
        "tables": len(table_blocks),
        "ir_summary": {
            "pages": len(ir.get("pages") or []),
            "blocks": len(ir.get("blocks") or []),
            "knowledge_units": len(ir.get("knowledge_units") or []),
        },
        "coverage_summary": (coverage.get("coverage") or {}).get("summary") or {},
        "rag_coverage_quality": coverage.get("rag_coverage_quality") or {},
        "provider_report": provider_report,
        "provider_score": ranking.get("score"),
        "recommendation": ranking.get("recommendation"),
    }


def _sample_report(
    *,
    settings: Any,
    parser_settings: dict[str, Any],
    path: Path,
    sample_index: int,
    sample_name: str | None,
    sample_source: str,
    profile: str,
    providers: Sequence[str] | None,
    page_range: tuple[int, int] | None,
    temp_dir: Path,
) -> dict[str, Any]:
    selection_mode = "explicit" if providers else "route_plan"
    media_type = _media_type_for(path)
    parse_path = _materialize_sample_input(
        source_path=path,
        page_range=page_range,
        temp_dir=temp_dir,
        sample_index=sample_index,
    )
    route_plan = local_provider_route_plan_payload(
        settings.providers,
        media_type=media_type,
        file_name=path.name,
        profile=profile,
        include_disabled=True,
    )
    provider_ids = _candidate_provider_ids(
        settings=settings,
        path=path,
        profile=profile,
        explicit=providers,
        route_plan=route_plan,
    )
    results = [
        _run_provider(
            settings=settings,
            parser_settings=parser_settings,
            provider_id=provider_id,
            path=path,
            parse_path=parse_path,
            media_type=media_type,
            profile=profile,
            sample_index=sample_index,
            page_range=page_range,
        )
        for provider_id in provider_ids
    ]
    completed = [item for item in results if item.get("status") == "done"]
    rankings = sorted(
        completed,
        key=lambda item: (
            -float(item.get("provider_score") if item.get("provider_score") is not None else 0.0),
            float(item.get("elapsed_s") or 0.0),
            str(item.get("provider_id") or ""),
        ),
    )
    return {
        "sample_name": sample_name or path.name,
        "source": sample_source,
        "file_name": path.name,
        "document": str(path),
        "page_range": _page_range_payload(page_range),
        "media_type": media_type,
        "profile": profile,
        "route_plan": route_plan,
        "routing_policy": route_plan.get("routing_policy"),
        "route_selection": route_plan.get("selection") or {},
        "provider_selection_mode": selection_mode,
        "requested_provider_ids": provider_ids,
        "providers": results,
        "ranking": [
            {
                "provider_id": item.get("provider_id"),
                "score": item.get("provider_score"),
                "elapsed_s": item.get("elapsed_s"),
                "recommendation": item.get("recommendation"),
            }
            for item in rankings
        ],
        "best_provider_id": rankings[0].get("provider_id") if rankings else None,
    }


def _provider_identity_payload(
    *,
    provider_id: str,
    provider_report: Mapping[str, Any],
    ir: Mapping[str, Any],
) -> dict[str, str]:
    provider_entries = provider_report.get("providers")
    if isinstance(provider_entries, Sequence) and not isinstance(provider_entries, (str, bytes, bytearray)):
        for entry in provider_entries:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("provider_id") or "") != provider_id:
                continue
            return {
                "provider_version": str(entry.get("provider_version") or "") or None,
                "adapter_version": str(entry.get("adapter_version") or "") or None,
            }
    ir_entries = ir.get("providers")
    if isinstance(ir_entries, Sequence) and not isinstance(ir_entries, (str, bytes, bytearray)):
        for entry in ir_entries:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("provider_id") or "") != provider_id:
                continue
            return {
                "provider_version": str(entry.get("provider_version") or "") or None,
                "adapter_version": str(entry.get("adapter_version") or "") or None,
            }
    return {
        "provider_version": None,
        "adapter_version": None,
    }


def _provider_identity_summary(sample_reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    for sample in sample_reports:
        sample_name = str(sample.get("sample_name") or sample.get("file_name") or "")
        for provider in sample.get("providers", []):
            if not isinstance(provider, Mapping):
                continue
            provider_id = str(provider.get("provider_id") or "").strip()
            if not provider_id:
                continue
            entry = providers.setdefault(
                provider_id,
                {
                    "provider_id": provider_id,
                    "run_count": 0,
                    "completed_run_count": 0,
                    "_status_counts": {},
                    "_provider_versions": set(),
                    "_adapter_versions": set(),
                    "_sample_names": set(),
                },
            )
            entry["run_count"] += 1
            status = str(provider.get("status") or "unknown").strip() or "unknown"
            entry["_status_counts"][status] = int(entry["_status_counts"].get(status, 0)) + 1
            if status == "done":
                entry["completed_run_count"] += 1
            provider_version = str(provider.get("provider_version") or "").strip()
            if provider_version:
                entry["_provider_versions"].add(provider_version)
            adapter_version = str(provider.get("adapter_version") or "").strip()
            if adapter_version:
                entry["_adapter_versions"].add(adapter_version)
            if sample_name:
                entry["_sample_names"].add(sample_name)

    finalized: dict[str, dict[str, Any]] = {}
    providers_with_multiple_provider_versions = 0
    providers_with_multiple_adapter_versions = 0
    for provider_id, entry in providers.items():
        provider_versions = sorted(str(item) for item in entry.pop("_provider_versions", set()))
        adapter_versions = sorted(str(item) for item in entry.pop("_adapter_versions", set()))
        sample_names = sorted(str(item) for item in entry.pop("_sample_names", set()))
        status_counts = {
            str(status): int(count)
            for status, count in sorted(
                dict(entry.pop("_status_counts", {})).items(),
                key=lambda item: item[0],
            )
        }
        if len(provider_versions) > 1:
            providers_with_multiple_provider_versions += 1
        if len(adapter_versions) > 1:
            providers_with_multiple_adapter_versions += 1
        finalized[provider_id] = {
            **entry,
            "status_counts": status_counts,
            "provider_versions": provider_versions,
            "adapter_versions": adapter_versions,
            "sample_names": sample_names,
        }

    return {
        "provider_count": len(finalized),
        "providers_with_multiple_provider_versions": providers_with_multiple_provider_versions,
        "providers_with_multiple_adapter_versions": providers_with_multiple_adapter_versions,
        "providers": finalized,
    }


def _configured_provider_admissions(settings: Any) -> dict[str, dict[str, Any]]:
    registry = local_provider_registry_payload(settings.providers)
    configured: dict[str, dict[str, Any]] = {}
    for provider in registry.get("local_parsers", []) or []:
        if not isinstance(provider, Mapping):
            continue
        provider_id = str(provider.get("id") or "").strip()
        if not provider_id:
            continue
        admission = provider.get("admission") if isinstance(provider.get("admission"), Mapping) else {}
        configured[provider_id] = {
            "route_mode": str(admission.get("route_mode") or "route"),
            "gate_status": str(admission.get("gate_status") or "passed"),
            "gate_checks": [str(item) for item in admission.get("gate_checks", []) or [] if str(item)],
            "route_ready": bool(admission.get("route_ready", False)),
        }
    return configured


def _provider_has_quality_warning(provider: Mapping[str, Any]) -> bool:
    rag_quality = provider.get("rag_coverage_quality") if isinstance(provider.get("rag_coverage_quality"), dict) else {}
    quality_gate = str(rag_quality.get("gate") or "accept")
    quality_flags = [str(flag) for flag in rag_quality.get("flags", []) if str(flag)]
    reading_order_axis = _provider_reading_order_axis(provider)
    reading_order_status = str(reading_order_axis.get("status") or "")
    return quality_gate != "accept" or bool(quality_flags) or reading_order_status == "warning"


def _provider_has_reading_order_warning(provider: Mapping[str, Any]) -> bool:
    reading_order_axis = _provider_reading_order_axis(provider)
    return str(reading_order_axis.get("status") or "") == "warning"


def _normalized_gate_checks(values: Sequence[str] | None) -> list[str]:
    ordered: list[str] = []
    for item in values or ():
        normalized = str(item).strip()
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return ordered


def _standard_admission_gate_checks(values: Sequence[str] | None) -> list[str]:
    normalized = _normalized_gate_checks(values)
    if normalized:
        return normalized
    return list(DEFAULT_ADMISSION_GATE_CHECKS)


def _admission_config_patch(provider_id: str, recommended: Mapping[str, Any]) -> list[str]:
    gate_checks = _standard_admission_gate_checks(
        recommended.get("gate_checks") if isinstance(recommended, Mapping) else []
    )
    gate_checks_literal = ", ".join(json.dumps(item, ensure_ascii=False) for item in gate_checks)
    return [
        "[[providers.local_parsers]]",
        f'id = {json.dumps(provider_id, ensure_ascii=False)}',
        f'route_mode = {json.dumps(str(recommended.get("route_mode") or ""), ensure_ascii=False)}',
        f'gate_status = {json.dumps(str(recommended.get("gate_status") or ""), ensure_ascii=False)}',
        f"gate_checks = [{gate_checks_literal}]",
    ]


def _admission_drift_details(
    *,
    current: Mapping[str, Any] | None,
    recommended: Mapping[str, Any],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if not isinstance(current, Mapping):
        current = {}
    current_gate_checks = _normalized_gate_checks(current.get("gate_checks") if isinstance(current, Mapping) else [])
    recommended_gate_checks = _normalized_gate_checks(recommended.get("gate_checks") if isinstance(recommended, Mapping) else [])
    drift_fields: list[str] = []
    drift_details: dict[str, dict[str, Any]] = {}

    def add_drift(field: str, current_value: Any, recommended_value: Any) -> None:
        drift_fields.append(field)
        drift_details[field] = {
            "current": current_value,
            "recommended": recommended_value,
        }

    current_route_mode = str(current.get("route_mode") or "")
    recommended_route_mode = str(recommended.get("route_mode") or "")
    if current_route_mode != recommended_route_mode:
        add_drift("route_mode", current_route_mode or None, recommended_route_mode or None)

    current_gate_status = str(current.get("gate_status") or "")
    recommended_gate_status = str(recommended.get("gate_status") or "")
    if current_gate_status != recommended_gate_status:
        add_drift("gate_status", current_gate_status or None, recommended_gate_status or None)

    if current_gate_checks != recommended_gate_checks:
        add_drift("gate_checks", current_gate_checks, recommended_gate_checks)

    current_route_ready = bool(current.get("route_ready", False))
    recommended_route_ready = bool(recommended.get("route_ready", False))
    if current_route_ready != recommended_route_ready:
        add_drift("route_ready", current_route_ready, recommended_route_ready)

    return drift_fields, drift_details


def _provider_admission_summary(
    *,
    settings: Any,
    sample_reports: Sequence[dict[str, Any]],
    gate_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    configured = _configured_provider_admissions(settings)
    identity_summary = _provider_identity_summary(sample_reports)
    providers: dict[str, dict[str, Any]] = {}

    for sample in sample_reports:
        sample_name = str(sample.get("sample_name") or sample.get("file_name") or "")
        provider_selection_mode = str(sample.get("provider_selection_mode") or "route_plan")
        route_selection = sample.get("route_selection") if isinstance(sample.get("route_selection"), Mapping) else {}
        route_primary = str(route_selection.get("primary_provider_id") or "")
        best_provider_id = str(sample.get("best_provider_id") or "")
        for provider in sample.get("providers", []) or []:
            if not isinstance(provider, Mapping):
                continue
            provider_id = str(provider.get("provider_id") or "").strip()
            if not provider_id:
                continue
            entry = providers.setdefault(
                provider_id,
                {
                    "provider_id": provider_id,
                    "run_count": 0,
                    "completed_run_count": 0,
                    "failed_run_count": 0,
                    "skipped_run_count": 0,
                    "unsupported_skip_count": 0,
                    "actionable_skipped_run_count": 0,
                    "best_provider_wins": 0,
                    "route_primary_count": 0,
                    "route_primary_best_match_count": 0,
                    "route_primary_best_mismatch_count": 0,
                    "quality_warning_runs": 0,
                    "reading_order_warning_runs": 0,
                    "sample_names": set(),
                    "current_admission": configured.get(provider_id),
                },
            )
            entry["run_count"] += 1
            if sample_name:
                entry["sample_names"].add(sample_name)
            status = str(provider.get("status") or "unknown")
            if status == "done":
                entry["completed_run_count"] += 1
            elif status == "failed":
                entry["failed_run_count"] += 1
            elif status == "skipped":
                entry["skipped_run_count"] += 1
                reason = str(provider.get("reason") or "")
                if reason == "unsupported_media_type_or_extension":
                    entry["unsupported_skip_count"] += 1
                if (
                    provider_selection_mode == "explicit"
                    or reason not in {"parser_not_configured", "unsupported_media_type_or_extension"}
                ):
                    entry["actionable_skipped_run_count"] += 1
            if best_provider_id and provider_id == best_provider_id:
                entry["best_provider_wins"] += 1
            if route_primary and provider_id == route_primary:
                entry["route_primary_count"] += 1
                if best_provider_id == provider_id:
                    entry["route_primary_best_match_count"] += 1
                elif best_provider_id:
                    entry["route_primary_best_mismatch_count"] += 1
            if status == "done" and _provider_has_quality_warning(provider):
                entry["quality_warning_runs"] += 1
            if status == "done" and _provider_has_reading_order_warning(provider):
                entry["reading_order_warning_runs"] += 1

    identity_providers = identity_summary.get("providers") if isinstance(identity_summary.get("providers"), Mapping) else {}
    finalized: dict[str, dict[str, Any]] = {}
    for provider_id, entry in sorted(providers.items()):
        identity = identity_providers.get(provider_id) if isinstance(identity_providers.get(provider_id), Mapping) else {}
        provider_versions = [str(item) for item in identity.get("provider_versions", []) or [] if str(item)]
        adapter_versions = [str(item) for item in identity.get("adapter_versions", []) or [] if str(item)]
        sample_names = sorted(str(item) for item in entry.pop("sample_names", set()))
        current_admission = entry.get("current_admission")
        current_admission_payload = (
            {
                "route_mode": str(current_admission.get("route_mode") or ""),
                "gate_status": str(current_admission.get("gate_status") or ""),
                "gate_checks": [str(item) for item in current_admission.get("gate_checks", []) or [] if str(item)],
                "route_ready": bool(current_admission.get("route_ready", False)),
            }
            if isinstance(current_admission, Mapping)
            else None
        )
        current_gate_checks = _standard_admission_gate_checks(
            current_admission_payload.get("gate_checks") if isinstance(current_admission_payload, Mapping) else []
        )
        only_unsupported_skips = (
            int(entry["run_count"]) > 0
            and int(entry["completed_run_count"]) <= 0
            and int(entry["failed_run_count"]) <= 0
            and int(entry["actionable_skipped_run_count"]) <= 0
            and int(entry["unsupported_skip_count"]) == int(entry["run_count"])
        )

        reason_codes: list[str] = []
        if int(entry["failed_run_count"]) > 0:
            reason_codes.append("provider_runs_failed")
        if int(entry["actionable_skipped_run_count"]) > 0:
            reason_codes.append("provider_runs_skipped")
        if only_unsupported_skips:
            reason_codes.append("no_relevant_samples")
        elif int(entry["completed_run_count"]) <= 0:
            reason_codes.append("no_completed_runs")
        if int(entry["quality_warning_runs"]) > 0:
            reason_codes.append("provider_quality_warning")
        if int(entry["reading_order_warning_runs"]) > 0:
            reason_codes.append("provider_reading_order_warning")
        if len(provider_versions) > 1:
            reason_codes.append("provider_version_drift")
        if len(adapter_versions) > 1:
            reason_codes.append("provider_adapter_version_drift")
        if int(entry["best_provider_wins"]) > 0:
            reason_codes.append("best_provider_win")
        if int(entry["route_primary_count"]) > 0:
            reason_codes.append("route_primary_configured")
        if int(entry["route_primary_best_mismatch_count"]) > 0:
            reason_codes.append("route_primary_best_mismatch")

        recommended_gate_status = "passed"
        recommended_route_mode = "route"
        recommended_action = "keep_route"
        if (
            int(entry["failed_run_count"]) > 0
            or len(provider_versions) > 1
            or len(adapter_versions) > 1
        ):
            recommended_gate_status = "failed"
            recommended_route_mode = "evaluate"
            recommended_action = "block_until_fixed"
        elif only_unsupported_skips and current_admission_payload is not None:
            recommended_gate_status = str(current_admission_payload.get("gate_status") or "passed")
            recommended_route_mode = str(current_admission_payload.get("route_mode") or "route")
            recommended_action = "keep_current_admission"
        elif (
            int(entry["completed_run_count"]) <= 0
            or int(entry["actionable_skipped_run_count"]) > 0
            or int(entry["quality_warning_runs"]) > 0
            or int(entry["reading_order_warning_runs"]) > 0
        ):
            recommended_gate_status = "pending"
            recommended_route_mode = "evaluate"
            recommended_action = "keep_evaluate"
        elif int(entry["route_primary_count"]) > 0:
            recommended_gate_status = "passed"
            recommended_route_mode = "route"
            recommended_action = (
                "review_priority_order"
                if int(entry["route_primary_best_mismatch_count"]) > 0
                else "keep_route"
            )
        elif int(entry["best_provider_wins"]) > 0:
            recommended_gate_status = "passed"
            recommended_route_mode = "route"
            recommended_action = "promote_to_route_candidate"
        else:
            recommended_gate_status = "pending"
            recommended_route_mode = "evaluate"
            recommended_action = "keep_evaluate"

        recommended_route_ready = (
            bool(current_admission_payload.get("route_ready", False))
            if only_unsupported_skips and current_admission_payload is not None
            else recommended_route_mode == "route" and recommended_gate_status == "passed"
        )
        recommended_admission = {
            "route_mode": recommended_route_mode,
            "gate_status": recommended_gate_status,
            "gate_checks": (
                current_gate_checks
                if only_unsupported_skips and current_admission_payload is not None
                else list(DEFAULT_ADMISSION_GATE_CHECKS)
            ),
            "route_ready": recommended_route_ready,
        }
        drift_fields, drift_details = _admission_drift_details(
            current=current_admission_payload,
            recommended=recommended_admission,
        )
        requires_config_update = bool(drift_fields)

        finalized[provider_id] = {
            **entry,
            "sample_names": sample_names,
            "provider_versions": provider_versions,
            "adapter_versions": adapter_versions,
            "current_admission": current_admission_payload,
            "recommended_admission": recommended_admission,
            "recommended_action": recommended_action,
            "reason_codes": reason_codes,
            "drift_fields": drift_fields,
            "drift_details": drift_details,
            "config_patch": _admission_config_patch(provider_id, recommended_admission),
            "requires_config_update": requires_config_update,
        }

    summary = {
        "provider_count": len(finalized),
        "route_ready_count": len(
            [entry for entry in finalized.values() if bool(entry["recommended_admission"]["route_ready"])]
        ),
        "route_count": len(
            [entry for entry in finalized.values() if str(entry["recommended_admission"]["route_mode"]) == "route"]
        ),
        "evaluate_count": len(
            [entry for entry in finalized.values() if str(entry["recommended_admission"]["route_mode"]) == "evaluate"]
        ),
        "passed_count": len(
            [entry for entry in finalized.values() if str(entry["recommended_admission"]["gate_status"]) == "passed"]
        ),
        "pending_count": len(
            [entry for entry in finalized.values() if str(entry["recommended_admission"]["gate_status"]) == "pending"]
        ),
        "failed_count": len(
            [entry for entry in finalized.values() if str(entry["recommended_admission"]["gate_status"]) == "failed"]
        ),
        "providers_requiring_config_update": len(
            [entry for entry in finalized.values() if bool(entry["requires_config_update"])]
        ),
        "promote_count": len(
            [entry for entry in finalized.values() if str(entry["recommended_action"]) == "promote_to_route_candidate"]
        ),
        "review_priority_count": len(
            [entry for entry in finalized.values() if str(entry["recommended_action"]) == "review_priority_order"]
        ),
        "providers_with_route_mode_drift": len(
            [entry for entry in finalized.values() if "route_mode" in entry.get("drift_fields", [])]
        ),
        "providers_with_gate_status_drift": len(
            [entry for entry in finalized.values() if "gate_status" in entry.get("drift_fields", [])]
        ),
        "providers_with_gate_checks_drift": len(
            [entry for entry in finalized.values() if "gate_checks" in entry.get("drift_fields", [])]
        ),
        "providers_with_route_ready_drift": len(
            [entry for entry in finalized.values() if "route_ready" in entry.get("drift_fields", [])]
        ),
        "provider_ids_requiring_config_update": [
            str(provider_id)
            for provider_id, entry in sorted(finalized.items())
            if bool(entry.get("requires_config_update"))
        ],
    }
    suite_gate = gate_summary if isinstance(gate_summary, Mapping) else {}
    return {
        "schema_version": "2026-06-provider-admission-summary",
        "suite_gate": {
            "gate": str(suite_gate.get("gate") or "unknown"),
            "passed": bool(suite_gate.get("passed", False)),
            "warnings": [str(item) for item in suite_gate.get("warnings", []) or [] if str(item)],
            "flags": [str(item) for item in suite_gate.get("flags", []) or [] if str(item)],
        },
        "summary": summary,
        "providers": finalized,
    }


def _apply_provider_admission_gate(
    gate_summary: Mapping[str, Any],
    admission_summary: Mapping[str, Any],
    *,
    gate_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_gate_policy = _gate_policy(gate_policy)
    payload = {
        key: value
        for key, value in dict(gate_summary).items()
    }
    flags = [str(item) for item in payload.get("flags", []) or [] if str(item)]
    warnings = [str(item) for item in payload.get("warnings", []) or [] if str(item)]
    findings = [item for item in payload.get("findings", []) or [] if isinstance(item, Mapping)]
    summary = admission_summary.get("summary") if isinstance(admission_summary.get("summary"), Mapping) else {}
    providers = admission_summary.get("providers") if isinstance(admission_summary.get("providers"), Mapping) else {}

    providers_requiring_config_update = int(summary.get("providers_requiring_config_update") or 0)
    providers_with_route_mode_drift = int(summary.get("providers_with_route_mode_drift") or 0)
    providers_with_gate_status_drift = int(summary.get("providers_with_gate_status_drift") or 0)
    providers_with_gate_checks_drift = int(summary.get("providers_with_gate_checks_drift") or 0)
    providers_with_route_ready_drift = int(summary.get("providers_with_route_ready_drift") or 0)
    provider_ids_requiring_config_update = [
        str(item)
        for item in summary.get("provider_ids_requiring_config_update", []) or []
        if str(item)
    ]

    if providers_requiring_config_update > 0:
        warnings.append("provider_admission_config_updates")
        for provider_id in provider_ids_requiring_config_update:
            provider_entry = providers.get(provider_id) if isinstance(providers.get(provider_id), Mapping) else {}
            findings.append(
                {
                    "severity": "warning",
                    "code": "provider_admission_config_update",
                    "provider_id": provider_id,
                    "drift_fields": list(provider_entry.get("drift_fields", []) or []),
                    "recommended_action": provider_entry.get("recommended_action"),
                    "message": "Provider admission recommendation differs from configured local parser admission",
                }
            )
    if providers_with_route_mode_drift > 0:
        warnings.append("provider_admission_route_mode_drift")
    if providers_with_gate_status_drift > 0:
        warnings.append("provider_admission_gate_status_drift")
    if providers_with_gate_checks_drift > 0:
        warnings.append("provider_admission_gate_checks_drift")
    if providers_with_route_ready_drift > 0:
        warnings.append("provider_admission_route_ready_drift")

    def _maybe_fail(count: int, policy_key: str, code: str, message: str) -> None:
        allowed = normalized_gate_policy.get(policy_key)
        if allowed is None or count <= allowed:
            return
        flags.append(code)
        findings.append(
            {
                "severity": "error",
                "code": code,
                "message": message,
                "observed": count,
                "allowed": allowed,
            }
        )

    _maybe_fail(
        providers_requiring_config_update,
        "max_providers_requiring_config_update",
        "provider_admission_config_update_budget_exceeded",
        "Provider admission config-update count exceeded gate policy budget",
    )
    _maybe_fail(
        providers_with_route_mode_drift,
        "max_providers_with_route_mode_drift",
        "provider_admission_route_mode_drift_budget_exceeded",
        "Provider route-mode drift count exceeded gate policy budget",
    )
    _maybe_fail(
        providers_with_gate_status_drift,
        "max_providers_with_gate_status_drift",
        "provider_admission_gate_status_drift_budget_exceeded",
        "Provider gate-status drift count exceeded gate policy budget",
    )
    _maybe_fail(
        providers_with_gate_checks_drift,
        "max_providers_with_gate_checks_drift",
        "provider_admission_gate_checks_drift_budget_exceeded",
        "Provider gate-check drift count exceeded gate policy budget",
    )
    _maybe_fail(
        providers_with_route_ready_drift,
        "max_providers_with_route_ready_drift",
        "provider_admission_route_ready_drift_budget_exceeded",
        "Provider route-ready drift count exceeded gate policy budget",
    )

    unique_flags = list(dict.fromkeys(flags))
    unique_warnings = list(dict.fromkeys(warnings))
    gate = "fail" if unique_flags else ("accept_with_warning" if unique_warnings else "accept")
    payload.update(
        {
            "gate": gate,
            "passed": gate != "fail",
            "flags": unique_flags,
            "warnings": unique_warnings,
            "findings": findings,
            "providers_requiring_config_update": providers_requiring_config_update,
            "providers_with_route_mode_drift": providers_with_route_mode_drift,
            "providers_with_gate_status_drift": providers_with_gate_status_drift,
            "providers_with_gate_checks_drift": providers_with_gate_checks_drift,
            "providers_with_route_ready_drift": providers_with_route_ready_drift,
            "provider_ids_requiring_config_update": provider_ids_requiring_config_update,
            "gate_policy": normalized_gate_policy,
        }
    )
    return payload


def _comparison_gate_summary(
    sample_reports: Sequence[dict[str, Any]],
    *,
    gate_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    flags: list[str] = []
    warnings: list[str] = []
    findings: list[dict[str, Any]] = []
    samples_without_completed_provider = 0
    samples_without_route_primary = 0
    samples_without_completed_route_primary = 0
    samples_best_provider_differs_from_route_primary = 0
    failed_provider_runs = 0
    skipped_provider_runs = 0
    provider_quality_warning_runs = 0
    provider_reading_order_warning_runs = 0
    normalized_gate_policy = _gate_policy(gate_policy)
    identity_summary = _provider_identity_summary(sample_reports)
    providers_with_multiple_provider_versions = int(
        identity_summary.get("providers_with_multiple_provider_versions") or 0
    )
    providers_with_multiple_adapter_versions = int(
        identity_summary.get("providers_with_multiple_adapter_versions") or 0
    )

    for sample in sample_reports:
        sample_name = str(sample.get("sample_name") or sample.get("file_name") or "")
        provider_selection_mode = str(sample.get("provider_selection_mode") or "route_plan")
        providers = [
            provider
            for provider in sample.get("providers", [])
            if isinstance(provider, dict)
        ]
        providers_by_id = {str(provider.get("provider_id") or ""): provider for provider in providers}
        completed = [provider for provider in providers if provider.get("status") == "done"]
        failed = [provider for provider in providers if provider.get("status") == "failed"]
        skipped = [provider for provider in providers if provider.get("status") == "skipped"]
        failed_provider_runs += len(failed)
        skipped_provider_runs += len(skipped)

        route_selection = sample.get("route_selection") if isinstance(sample.get("route_selection"), dict) else {}
        route_primary = str(route_selection.get("primary_provider_id") or "")
        best_provider_id = str(sample.get("best_provider_id") or "")

        if not route_primary:
            samples_without_route_primary += 1
            flags.append("route_primary_missing")
            findings.append(
                {
                    "sample_name": sample_name,
                    "severity": "error",
                    "code": "route_primary_missing",
                    "message": "Route plan did not select a primary provider",
                }
            )
        elif providers_by_id.get(route_primary, {}).get("status") != "done":
            samples_without_completed_route_primary += 1
            flags.append("route_primary_not_completed")
            findings.append(
                {
                    "sample_name": sample_name,
                    "severity": "error",
                    "code": "route_primary_not_completed",
                    "provider_id": route_primary,
                    "status": providers_by_id.get(route_primary, {}).get("status", "missing"),
                    "message": "Route primary provider did not complete successfully",
                }
            )

        if not completed:
            samples_without_completed_provider += 1
            flags.append("sample_without_completed_provider")
            findings.append(
                {
                    "sample_name": sample_name,
                    "severity": "error",
                    "code": "sample_without_completed_provider",
                    "message": "No provider completed for this sample",
                }
            )

        if failed:
            flags.append("provider_runs_failed")
            findings.extend(
                {
                    "sample_name": sample_name,
                    "severity": "error",
                    "code": "provider_run_failed",
                    "provider_id": str(provider.get("provider_id") or ""),
                    "message": str(provider.get("error") or "Provider run failed"),
                }
                for provider in failed
            )
        actionable_skipped = [
            provider
            for provider in skipped
            if provider_selection_mode == "explicit"
            or str(provider.get("reason") or "") not in {"parser_not_configured", "unsupported_media_type_or_extension"}
        ]
        if actionable_skipped:
            warnings.append("provider_runs_skipped")

        if route_primary and best_provider_id and best_provider_id != route_primary:
            samples_best_provider_differs_from_route_primary += 1
            warnings.append("best_provider_differs_from_route_primary")
            findings.append(
                {
                    "sample_name": sample_name,
                    "severity": "warning",
                    "code": "best_provider_differs_from_route_primary",
                    "route_primary_provider_id": route_primary,
                    "best_provider_id": best_provider_id,
                    "message": "Measured best provider differs from route primary",
                }
            )

        for provider in completed:
            rag_quality = provider.get("rag_coverage_quality") if isinstance(provider.get("rag_coverage_quality"), dict) else {}
            quality_gate = str(rag_quality.get("gate") or "accept")
            quality_flags = [str(flag) for flag in rag_quality.get("flags", []) if str(flag)]
            reading_order_axis = _provider_reading_order_axis(provider)
            reading_order_status = str(reading_order_axis.get("status") or "")
            has_quality_warning = False
            if quality_gate != "accept" or quality_flags:
                has_quality_warning = True
            if reading_order_status == "warning":
                has_quality_warning = True
                provider_reading_order_warning_runs += 1
                warnings.append("provider_reading_order_warnings")
                findings.append(
                    {
                        "sample_name": sample_name,
                        "severity": "warning",
                        "code": "provider_reading_order_warning",
                        "provider_id": str(provider.get("provider_id") or ""),
                        "reading_order_confidence": reading_order_axis.get("reading_order_confidence"),
                        "threshold": reading_order_axis.get("threshold"),
                        "message": "Provider reading-order confidence is below threshold",
                    }
                )
            if has_quality_warning:
                provider_quality_warning_runs += 1
                warnings.append("provider_quality_warnings")
                if quality_gate != "accept" or quality_flags:
                    findings.append(
                        {
                            "sample_name": sample_name,
                            "severity": "warning",
                            "code": "provider_quality_warning",
                            "provider_id": str(provider.get("provider_id") or ""),
                            "quality_gate": quality_gate,
                            "quality_flags": quality_flags,
                        }
                    )

    identity_providers = identity_summary.get("providers") if isinstance(identity_summary.get("providers"), Mapping) else {}
    if providers_with_multiple_provider_versions > 0:
        unique_provider_ids = [
            str(provider_id)
            for provider_id, entry in identity_providers.items()
            if isinstance(entry, Mapping) and len(list(entry.get("provider_versions") or [])) > 1
        ]
        warnings.append("provider_version_drift")
        findings.extend(
            {
                "severity": "warning",
                "code": "provider_version_drift",
                "provider_id": provider_id,
                "provider_versions": list((identity_providers.get(provider_id) or {}).get("provider_versions") or []),
                "message": "Provider resolved to multiple upstream versions across the suite",
            }
            for provider_id in unique_provider_ids
        )
    if providers_with_multiple_adapter_versions > 0:
        unique_provider_ids = [
            str(provider_id)
            for provider_id, entry in identity_providers.items()
            if isinstance(entry, Mapping) and len(list(entry.get("adapter_versions") or [])) > 1
        ]
        warnings.append("provider_adapter_version_drift")
        findings.extend(
            {
                "severity": "warning",
                "code": "provider_adapter_version_drift",
                "provider_id": provider_id,
                "adapter_versions": list((identity_providers.get(provider_id) or {}).get("adapter_versions") or []),
                "message": "Provider resolved to multiple adapter versions across the suite",
            }
            for provider_id in unique_provider_ids
        )

    unique_flags = list(dict.fromkeys(flags))
    unique_warnings = list(dict.fromkeys(warnings))
    max_reading_order_warning_runs = normalized_gate_policy.get("max_provider_reading_order_warning_runs")
    if (
        max_reading_order_warning_runs is not None
        and provider_reading_order_warning_runs > max_reading_order_warning_runs
    ):
        unique_flags.append("provider_reading_order_warning_budget_exceeded")
        findings.append(
            {
                "severity": "error",
                "code": "provider_reading_order_warning_budget_exceeded",
                "message": "Provider reading-order warning count exceeded gate policy budget",
                "observed": provider_reading_order_warning_runs,
                "allowed": max_reading_order_warning_runs,
            }
        )
    max_quality_warning_runs = normalized_gate_policy.get("max_provider_quality_warning_runs")
    if max_quality_warning_runs is not None and provider_quality_warning_runs > max_quality_warning_runs:
        unique_flags.append("provider_quality_warning_budget_exceeded")
        findings.append(
            {
                "severity": "error",
                "code": "provider_quality_warning_budget_exceeded",
                "message": "Provider quality warning count exceeded gate policy budget",
                "observed": provider_quality_warning_runs,
                "allowed": max_quality_warning_runs,
            }
        )
    max_route_primary_mismatches = normalized_gate_policy.get(
        "max_samples_best_provider_differs_from_route_primary"
    )
    if (
        max_route_primary_mismatches is not None
        and samples_best_provider_differs_from_route_primary > max_route_primary_mismatches
    ):
        unique_flags.append("best_provider_differs_from_route_primary_budget_exceeded")
        findings.append(
            {
                "severity": "error",
                "code": "best_provider_differs_from_route_primary_budget_exceeded",
                "message": "Best-provider mismatch count exceeded gate policy budget",
                "observed": samples_best_provider_differs_from_route_primary,
                "allowed": max_route_primary_mismatches,
            }
        )
    max_provider_version_drift = normalized_gate_policy.get("max_providers_with_multiple_provider_versions")
    if (
        max_provider_version_drift is not None
        and providers_with_multiple_provider_versions > max_provider_version_drift
    ):
        unique_flags.append("provider_version_drift_budget_exceeded")
        findings.append(
            {
                "severity": "error",
                "code": "provider_version_drift_budget_exceeded",
                "message": "Provider version drift count exceeded gate policy budget",
                "observed": providers_with_multiple_provider_versions,
                "allowed": max_provider_version_drift,
            }
        )
    max_adapter_version_drift = normalized_gate_policy.get("max_providers_with_multiple_adapter_versions")
    if (
        max_adapter_version_drift is not None
        and providers_with_multiple_adapter_versions > max_adapter_version_drift
    ):
        unique_flags.append("provider_adapter_version_drift_budget_exceeded")
        findings.append(
            {
                "severity": "error",
                "code": "provider_adapter_version_drift_budget_exceeded",
                "message": "Provider adapter-version drift count exceeded gate policy budget",
                "observed": providers_with_multiple_adapter_versions,
                "allowed": max_adapter_version_drift,
            }
        )
    gate = "fail" if unique_flags else ("accept_with_warning" if unique_warnings else "accept")
    return {
        "schema_version": "2026-06-provider-comparison-gate",
        "gate": gate,
        "passed": gate != "fail",
        "flags": unique_flags,
        "warnings": unique_warnings,
        "sample_count": len(sample_reports),
        "samples_without_route_primary": samples_without_route_primary,
        "samples_without_completed_route_primary": samples_without_completed_route_primary,
        "samples_without_completed_provider": samples_without_completed_provider,
        "samples_best_provider_differs_from_route_primary": samples_best_provider_differs_from_route_primary,
        "failed_provider_runs": failed_provider_runs,
        "skipped_provider_runs": skipped_provider_runs,
        "provider_quality_warning_runs": provider_quality_warning_runs,
        "provider_reading_order_warning_runs": provider_reading_order_warning_runs,
        "providers_with_multiple_provider_versions": providers_with_multiple_provider_versions,
        "providers_with_multiple_adapter_versions": providers_with_multiple_adapter_versions,
        "gate_policy": normalized_gate_policy,
        "findings": findings,
    }


def _provider_reading_order_axis(provider: Mapping[str, Any]) -> Mapping[str, Any]:
    provider_report = provider.get("provider_report")
    if not isinstance(provider_report, Mapping):
        return {}
    comparison_report = provider_report.get("comparison_report")
    if not isinstance(comparison_report, Mapping):
        return {}
    rankings = comparison_report.get("rankings")
    if not isinstance(rankings, Sequence):
        return {}
    provider_id = str(provider.get("provider_id") or "")
    ranking: Mapping[str, Any] | None = None
    for item in rankings:
        if isinstance(item, Mapping) and str(item.get("provider_id") or "") == provider_id:
            ranking = item
            break
    if ranking is None:
        for item in rankings:
            if isinstance(item, Mapping):
                ranking = item
                break
    if not isinstance(ranking, Mapping):
        return {}
    axes = ranking.get("axes")
    if not isinstance(axes, Mapping):
        return {}
    reading_order = axes.get("reading_order")
    return reading_order if isinstance(reading_order, Mapping) else {}


def build_report(
    *,
    config: str | Path,
    samples: Sequence[str | Path] = (),
    suite: str | Path | None = None,
    fixture_root: str | Path | None = None,
    providers: Sequence[str] | None = None,
    profile: str = "default",
    page_start: int | None = None,
    page_end: int | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    settings = load_settings(config)
    parser_settings = _parser_settings_by_name(settings)
    resolved_fixture_root = _fixture_root_path(fixture_root)
    default_page_range = _page_range(page_start=page_start, page_end=page_end)
    sample_specs: list[SampleSpec] = []
    gate_policy: dict[str, Any] = {}
    if suite is not None:
        suite_specs, gate_policy = _load_suite_samples(suite, fixture_root=resolved_fixture_root)
        sample_specs.extend(suite_specs)
    sample_specs.extend(
        SampleSpec(
            path=_resolve_path(Path.cwd(), sample),
            name=Path(sample).name,
            source="sample",
            page_start=(default_page_range[0] if default_page_range else None),
            page_end=(default_page_range[1] if default_page_range else None),
        )
        for sample in samples
    )
    if not sample_specs:
        raise ValueError("No samples were provided; use --sample or --suite")
    if progress:
        print(
            f"[provider-comparison-report] starting samples={len(sample_specs)} profile={profile}",
            file=sys.stderr,
            flush=True,
        )
    with TemporaryDirectory(prefix="parsecore-provider-compare-") as temp_root:
        temp_dir = Path(temp_root)
        sample_reports: list[dict[str, Any]] = []
        for index, sample in enumerate(sample_specs, start=1):
            if progress:
                print(
                    "[provider-comparison-report] sample {index}/{total}: {name} source={source}".format(
                        index=index,
                        total=len(sample_specs),
                        name=sample.name or sample.path.name,
                        source=sample.source,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            started = time.perf_counter()
            sample_report = _sample_report(
                settings=settings,
                parser_settings=parser_settings,
                path=sample.path,
                sample_index=index,
                sample_name=sample.name,
                sample_source=sample.source,
                profile=sample.profile or profile,
                providers=sample.providers or providers,
                page_range=_page_range(
                    page_start=(
                        sample.page_start if sample.page_start is not None else (
                            default_page_range[0] if default_page_range else None
                        )
                    ),
                    page_end=(
                        sample.page_end if sample.page_end is not None else (
                            default_page_range[1] if default_page_range else None
                        )
                    ),
                ),
                temp_dir=temp_dir,
            )
            sample_reports.append(sample_report)
            if progress:
                provider_reports = sample_report.get("providers") if isinstance(sample_report.get("providers"), list) else []
                print(
                    "[provider-comparison-report] sample {index}/{total} done: {name} providers={providers} elapsed_s={elapsed}".format(
                        index=index,
                        total=len(sample_specs),
                        name=sample_report.get("sample_name") or sample_report.get("file_name") or sample.path.name,
                        providers=len(provider_reports),
                        elapsed=f"{time.perf_counter() - started:.2f}",
                    ),
                    file=sys.stderr,
                    flush=True,
                )
    provider_runs = [
        provider
        for sample in sample_reports
        for provider in sample.get("providers", [])
        if isinstance(provider, dict)
    ]
    provider_identity_summary = _provider_identity_summary(sample_reports)
    base_gate_summary = _comparison_gate_summary(sample_reports, gate_policy=gate_policy)
    provider_admission_summary = _provider_admission_summary(
        settings=settings,
        sample_reports=sample_reports,
        gate_summary=base_gate_summary,
    )
    gate_summary = _apply_provider_admission_gate(
        base_gate_summary,
        provider_admission_summary,
        gate_policy=gate_policy,
    )
    provider_admission_summary["suite_gate"] = {
        "gate": str(gate_summary.get("gate") or "unknown"),
        "passed": bool(gate_summary.get("passed", False)),
        "warnings": [str(item) for item in gate_summary.get("warnings", []) or [] if str(item)],
        "flags": [str(item) for item in gate_summary.get("flags", []) or [] if str(item)],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": str(Path(config).resolve()),
        "suite": str(Path(suite).resolve()) if suite is not None else None,
        "fixture_root": str(resolved_fixture_root) if resolved_fixture_root is not None else None,
        "profile": profile,
        "gate_policy": gate_policy,
        "summary": {
            "sample_count": len(sample_reports),
            "provider_run_count": len(provider_runs),
            "completed_provider_runs": len([item for item in provider_runs if item.get("status") == "done"]),
            "failed_provider_runs": len([item for item in provider_runs if item.get("status") == "failed"]),
            "skipped_provider_runs": len([item for item in provider_runs if item.get("status") == "skipped"]),
        },
        "provider_identity_summary": provider_identity_summary,
        "gate_summary": gate_summary,
        "provider_admission_summary": provider_admission_summary,
        "samples": sample_reports,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# ParseCore Local Provider Comparison",
        "",
        f"- schema_version: `{payload.get('schema_version')}`",
        f"- profile: `{payload.get('profile')}`",
        f"- suite: `{payload.get('suite')}`",
        f"- gate_policy: `{json.dumps(payload.get('gate_policy') or {}, ensure_ascii=False)}`",
        f"- samples: {summary.get('sample_count', 0)}",
        f"- completed_provider_runs: {summary.get('completed_provider_runs', 0)}",
        f"- failed_provider_runs: {summary.get('failed_provider_runs', 0)}",
        f"- skipped_provider_runs: {summary.get('skipped_provider_runs', 0)}",
        f"- provider_quality_warning_runs: {(payload.get('gate_summary') or {}).get('provider_quality_warning_runs', 0)}",
        f"- reading_order_warning_runs: {(payload.get('gate_summary') or {}).get('provider_reading_order_warning_runs', 0)}",
        f"- best_provider_route_mismatches: {(payload.get('gate_summary') or {}).get('samples_best_provider_differs_from_route_primary', 0)}",
        f"- providers_with_multiple_provider_versions: {(payload.get('gate_summary') or {}).get('providers_with_multiple_provider_versions', 0)}",
        f"- providers_with_multiple_adapter_versions: {(payload.get('gate_summary') or {}).get('providers_with_multiple_adapter_versions', 0)}",
        f"- gate: `{(payload.get('gate_summary') or {}).get('gate')}`",
        "",
        "## Route Plan",
        "",
    ]
    for sample in payload.get("samples") or []:
        selection = sample.get("route_selection") or {}
        fallback_ids = selection.get("fallback_provider_ids") or []
        fallback_text = ", ".join(str(item) for item in fallback_ids) if fallback_ids else ""
        page_range = sample.get("page_range") if isinstance(sample.get("page_range"), dict) else {}
        page_text = ""
        if page_range:
            page_text = " pages=`{start}-{end}`".format(
                start=page_range.get("start", ""),
                end=page_range.get("end", ""),
            )
        lines.append(
            "- {sample}: primary=`{primary}` fallback=`{fallback}` policy=`{policy}`{page_text}".format(
                sample=sample.get("sample_name") or sample.get("file_name", ""),
                primary=selection.get("primary_provider_id") or "",
                fallback=fallback_text,
                policy=sample.get("routing_policy") or "",
                page_text=page_text,
            )
        )
    lines.extend(
        [
            "",
            "## Provider Identities",
            "",
            "| provider | versions | adapters | runs | completed | statuses |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    identity_summary = payload.get("provider_identity_summary") or {}
    for provider_id, identity in sorted((identity_summary.get("providers") or {}).items()):
        if not isinstance(identity, Mapping):
            continue
        lines.append(
            "| {provider} | {versions} | {adapters} | {runs} | {completed} | {statuses} |".format(
                provider=provider_id,
                versions=", ".join(str(item) for item in identity.get("provider_versions") or []) or "-",
                adapters=", ".join(str(item) for item in identity.get("adapter_versions") or []) or "-",
                runs=identity.get("run_count", 0),
                completed=identity.get("completed_run_count", 0),
                statuses=json.dumps(identity.get("status_counts") or {}, ensure_ascii=False),
            )
        )
    lines.extend(
        [
            "",
            "## Provider Admission Recommendations",
            "",
            "| provider | current | recommended | route_ready | action | drift | reasons | update |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    admission_summary = payload.get("provider_admission_summary") or {}
    admission_providers = admission_summary.get("providers") if isinstance(admission_summary.get("providers"), Mapping) else {}
    for provider_id, admission in sorted(admission_providers.items()):
        if not isinstance(admission, Mapping):
            continue
        current = admission.get("current_admission") if isinstance(admission.get("current_admission"), Mapping) else {}
        recommended = admission.get("recommended_admission") if isinstance(admission.get("recommended_admission"), Mapping) else {}
        current_text = (
            f"{current.get('route_mode', '-')}/{current.get('gate_status', '-')}"
            if current
            else "-"
        )
        recommended_text = (
            f"{recommended.get('route_mode', '-')}/{recommended.get('gate_status', '-')}"
            if recommended
            else "-"
        )
        lines.append(
            "| {provider} | {current} | {recommended} | {route_ready} | {action} | {drift} | {reasons} | {update} |".format(
                provider=provider_id,
                current=current_text,
                recommended=recommended_text,
                route_ready=str(bool(recommended.get("route_ready", False))).lower(),
                action=admission.get("recommended_action", ""),
                drift=", ".join(str(item) for item in admission.get("drift_fields") or []) or "-",
                reasons=", ".join(str(item) for item in admission.get("reason_codes") or []) or "-",
                update="yes" if admission.get("requires_config_update") else "no",
            )
        )
    lines.extend(["", "## Provider Admission Patches", ""])
    for provider_id, admission in sorted(admission_providers.items()):
        if not isinstance(admission, Mapping):
            continue
        if not admission.get("requires_config_update"):
            continue
        lines.append(f"### {provider_id}")
        lines.append("")
        lines.append("```toml")
        lines.extend(str(line) for line in admission.get("config_patch") or [])
        lines.append("```")
        lines.append("")
    lines.extend(
        [
            "",
            "## Provider Runs",
            "",
            "| sample | provider | version | adapter | status | score | elapsed_s | blocks | chunks | tables | recommendation |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for sample in payload.get("samples") or []:
        for provider in sample.get("providers") or []:
            lines.append(
                "| {sample} | {provider} | {version} | {adapter} | {status} | {score} | {elapsed_s} | {blocks} | {chunks} | {tables} | {recommendation} |".format(
                    sample=sample.get("sample_name") or sample.get("file_name", ""),
                    provider=provider.get("provider_id", ""),
                    version=provider.get("provider_version", "") or "-",
                    adapter=provider.get("adapter_version", "") or "-",
                    status=provider.get("status", ""),
                    score=provider.get("provider_score", ""),
                    elapsed_s=provider.get("elapsed_s", ""),
                    blocks=provider.get("blocks", ""),
                    chunks=provider.get("chunks", ""),
                    tables=provider.get("tables", ""),
                    recommendation=provider.get("recommendation", provider.get("reason", provider.get("error", ""))),
                )
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.sample and not args.suite:
        parser.error("at least one --sample or --suite is required")
    provider_ids = _provider_ids(args.provider)
    payload = build_report(
        config=args.config,
        samples=[Path(item) for item in args.sample],
        suite=args.suite,
        fixture_root=args.fixture_root,
        providers=provider_ids or None,
        profile=args.profile,
        page_start=args.page_start,
        page_end=args.page_end,
        progress=args.progress,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out_json:
        output_path = Path(args.out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"[provider-comparison-report] wrote {output_path}")
    else:
        print(text)
    if args.out_md:
        markdown_path = Path(args.out_md)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[provider-comparison-report] wrote {markdown_path}")
    failed = int((payload.get("summary") or {}).get("failed_provider_runs") or 0)
    gate_passed = bool((payload.get("gate_summary") or {}).get("passed", True))
    return 1 if failed or not gate_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
