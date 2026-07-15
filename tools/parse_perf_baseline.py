"""Capture lightweight ParseCore parsing performance baselines."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import mimetypes
import statistics
import sys
import threading
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.api_payloads import (  # noqa: E402
    _document_providers_projection,
    _document_quality_projection,
)
from parsecore.models import Block, BlockType, ParseOutcome, ParseRequest  # noqa: E402


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

DEFAULT_EXTENSIONS = (".xls", ".xlsx", ".xlsm")
STABILITY_SCHEMA_VERSION = "2026-07-parse-perf-stability"
DEFAULT_PROCESS_SAMPLE_INTERVAL_MS = 100
# ``ocr_warm`` is the default fast-provider lane: it bypasses the whole
# document parse cache while allowing the explicitly observed page OCR cache.
# ``disabled`` is the all-cache-bypassed cold lane; ``configured`` is retained
# for diagnostic observations and must not be used as a stability baseline.
CACHE_MODES = ("disabled", "ocr_warm", "configured")
DEFAULT_CACHE_MODE = "ocr_warm"

try:  # Optional tooling dependency; never required by the ParseCore runtime.
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only when optional dependency is absent
    psutil = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture ParseCore parse performance baselines")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--sample-dir", default="D:/app/uploads")
    parser.add_argument("--sample", action="append", help="Explicit sample file; can be repeated")
    parser.add_argument(
        "--extensions",
        default=",".join(DEFAULT_EXTENSIONS),
        help="Comma-separated extensions to scan when --sample is not supplied",
    )
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of measured runs. Multi-run stability measurement requires one explicit sample.",
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        help=(
            "Unmeasured warmups before the measured runs. Defaults to one for --runs > 1 "
            "with a reused runtime, otherwise zero."
        ),
    )
    parser.add_argument(
        "--reuse-runtime",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse one runtime/parser lifecycle across runs; disable for an all-cold measurement lane.",
    )
    parser.add_argument(
        "--cache-mode",
        choices=CACHE_MODES,
        default=DEFAULT_CACHE_MODE,
        help=(
            "Cache behavior for each request: ocr_warm bypasses the in-process parse cache while "
            "using the page OCR cache; disabled bypasses both without modifying cache files; "
            "configured keeps all product defaults."
        ),
    )
    parser.add_argument(
        "--process-telemetry",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Sample current-process RSS/working set, CPU time and I/O with optional psutil.",
    )
    parser.add_argument(
        "--process-sample-interval-ms",
        type=int,
        default=DEFAULT_PROCESS_SAMPLE_INTERVAL_MS,
        help="Process telemetry sampling interval in milliseconds (default: 100).",
    )
    parser.add_argument(
        "--stability-policy",
        help="Optional JSON policy containing lane budgets, sample identity and structural fingerprint.",
    )
    parser.add_argument(
        "--enforce-stability-gate",
        action="store_true",
        help="Return non-zero when the supplied stability policy fails.",
    )
    parser.add_argument("--out-json", help="Optional JSON output path")
    parser.add_argument("--out-md", help="Optional Markdown output path")
    parser.add_argument(
        "--track-python-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Track Python peak allocations with tracemalloc (default: enabled for "
            "historical compatibility); use --no-track-python-memory for a clean latency lane"
        ),
    )
    parser.add_argument("--fail-on-errors", action="store_true")
    return parser


def _round(value: float) -> float:
    return round(float(value), 3)


def _round_optional(value: float | None) -> float | None:
    return _round(value) if value is not None else None


def _as_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _percentile(values: Iterable[float], ratio: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(1.0, max(0.0, float(ratio)))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _distribution(values: Iterable[float]) -> dict[str, Any]:
    samples = [float(value) for value in values]
    if not samples:
        return {
            "count": 0,
            "values": [],
            "mean": None,
            "p50": None,
            "median": None,
            "p95": None,
            "min": None,
            "max": None,
            "range": None,
            "population_stddev": None,
            "cv_pct": None,
        }
    mean = statistics.fmean(samples)
    deviation = statistics.pstdev(samples)
    p50 = _percentile(samples, 0.50)
    p95 = _percentile(samples, 0.95)
    return {
        "count": len(samples),
        "values": [_round(value) for value in samples],
        "mean": _round(mean),
        "p50": _round(p50 or 0.0),
        "median": _round(p50 or 0.0),
        "p95": _round(p95 or 0.0),
        "min": _round(min(samples)),
        "max": _round(max(samples)),
        "range": _round(max(samples) - min(samples)),
        "population_stddev": _round(deviation),
        "cv_pct": _round(deviation / mean * 100.0) if mean else 0.0,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _pipeline_cache_snapshot(runtime: Any) -> dict[str, int]:
    registry = getattr(runtime, "pipeline_registry", None)
    describe = getattr(registry, "describe", None)
    if not callable(describe):
        return {"size": 0, "hits": 0, "misses": 0}
    try:
        payload = describe()
    except Exception:  # pragma: no cover - defensive observability only
        return {"size": 0, "hits": 0, "misses": 0}
    cache = payload.get("cache") if isinstance(payload, Mapping) else {}
    return {
        "size": _as_int(cache.get("size") if isinstance(cache, Mapping) else 0),
        "hits": _as_int(cache.get("hits") if isinstance(cache, Mapping) else 0),
        "misses": _as_int(cache.get("misses") if isinstance(cache, Mapping) else 0),
    }


def _cache_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, int]:
    return {
        key: _as_int(after.get(key)) - _as_int(before.get(key))
        for key in ("size", "hits", "misses")
    }


def _parser_instance_labels(runtime: Any) -> list[str]:
    result: list[str] = []
    for parser in tuple(getattr(runtime, "parsers", ()) or ()):
        parser_name = str(getattr(parser, "name", "") or "").strip()
        result.append(parser_name or type(parser).__name__)
    return result


class _ProcessTelemetrySampler:
    """Best-effort process sampler kept entirely in benchmark tooling."""

    def __init__(self, *, enabled: bool, interval_ms: int) -> None:
        self._enabled = bool(enabled)
        self._interval_s = max(0.01, int(interval_ms) / 1000.0)
        self._samples: list[dict[str, float | int | None]] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: Any | None = None
        self._unavailable_reason: str | None = None
        if not self._enabled:
            self._unavailable_reason = "disabled"
        elif psutil is None:
            self._unavailable_reason = "psutil_not_installed"
        else:
            try:
                self._process = psutil.Process()
            except Exception as exc:  # pragma: no cover - platform/process failure
                self._unavailable_reason = f"psutil_unavailable:{type(exc).__name__}"

    def _snapshot(self) -> dict[str, float | int | None] | None:
        if self._process is None:
            return None
        try:
            memory = self._process.memory_info()
            cpu = self._process.cpu_times()
            io = self._process.io_counters()
        except Exception:  # pragma: no cover - process could exit during teardown
            return None
        rss_bytes = _as_int(getattr(memory, "rss", 0))
        working_set_bytes = _as_int(getattr(memory, "wset", rss_bytes), default=rss_bytes)
        return {
            "rss_bytes": rss_bytes,
            "working_set_bytes": working_set_bytes,
            "vms_bytes": _as_int(getattr(memory, "vms", 0)),
            "cpu_user_s": float(getattr(cpu, "user", 0.0) or 0.0),
            "cpu_system_s": float(getattr(cpu, "system", 0.0) or 0.0),
            "io_read_bytes": _as_int(getattr(io, "read_bytes", 0)),
            "io_write_bytes": _as_int(getattr(io, "write_bytes", 0)),
        }

    def _record(self) -> None:
        snapshot = self._snapshot()
        if snapshot is not None:
            self._samples.append(snapshot)

    def _sample_loop(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            self._record()

    def start(self) -> None:
        if self._process is None:
            return
        self._record()
        self._thread = threading.Thread(target=self._sample_loop, name="parse-perf-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        if self._process is None:
            return {
                "status": "disabled" if self._unavailable_reason == "disabled" else "unavailable",
                "collector": "psutil",
                "reason": self._unavailable_reason,
            }
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._interval_s * 4.0))
        self._record()
        if not self._samples:
            return {
                "status": "unavailable",
                "collector": "psutil",
                "reason": "no_process_samples",
            }
        first = self._samples[0]
        last = self._samples[-1]
        peaks = {
            key: max(_as_int(sample.get(key)) for sample in self._samples)
            for key in ("rss_bytes", "working_set_bytes", "vms_bytes")
        }
        delta = {
            key: _round(max(0.0, float(last.get(key) or 0.0) - float(first.get(key) or 0.0)))
            for key in ("cpu_user_s", "cpu_system_s")
        }
        delta["cpu_total_s"] = _round(delta["cpu_user_s"] + delta["cpu_system_s"])
        delta.update(
            {
                key: max(0, _as_int(last.get(key)) - _as_int(first.get(key)))
                for key in ("io_read_bytes", "io_write_bytes")
            }
        )
        return {
            "status": "available",
            "collector": "psutil",
            "scope": "runtime.submit_end_to_end",
            "process_id": _as_int(getattr(self._process, "pid", 0)),
            "sample_interval_ms": int(round(self._interval_s * 1000.0)),
            "sample_count": len(self._samples),
            "working_set_semantics": "rss" if sys.platform != "win32" else "Windows working set (rss)",
            "start": {key: _round_optional(value) if isinstance(value, float) else value for key, value in first.items()},
            "end": {key: _round_optional(value) if isinstance(value, float) else value for key, value in last.items()},
            "peak": peaks,
            "delta": delta,
        }


def _parse_extensions(value: str) -> set[str]:
    result: set[str] = set()
    for item in value.split(","):
        normalized = item.strip().lower()
        if not normalized:
            continue
        result.add(normalized if normalized.startswith(".") else f".{normalized}")
    return result or set(DEFAULT_EXTENSIONS)


def _discover_samples(*, sample_dir: Path, extensions: set[str], max_files: int) -> list[Path]:
    if not sample_dir.exists():
        return []
    return sorted(
        path
        for path in sample_dir.iterdir()
        if path.is_file() and path.suffix.lower() in extensions
    )[: max(1, max_files)]


def _media_type_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in MEDIA_TYPES:
        return MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed


def _provider_id_for_block(block: Block) -> str:
    metadata = block.metadata or {}
    for key in ("provider_id", "parser", "layout_source", "ocr_engine"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if block.type == BlockType.IMAGE:
        return "image-ocr" if metadata.get("ocr_engine") else "pdf-text"
    return "parsecore-native"


def _snapshot_for_provider_report(runtime: Any, outcome: ParseOutcome, blocks: tuple[Block, ...]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "job": outcome.job,
        "doc_id": outcome.job.doc_id,
        "blocks": blocks,
        "chunks": outcome.chunks,
    }
    provider_registry = getattr(runtime, "provider_registry", None)
    if callable(provider_registry):
        snapshot["provider_registry"] = provider_registry()
    quality_gate_config = getattr(runtime, "quality_gate_config", None)
    if callable(quality_gate_config):
        snapshot["quality_gate"] = quality_gate_config()
    return snapshot


def _annotate_provider_sample_metrics(
    blocks: tuple[Block, ...],
    *,
    primary_provider_id: str | None,
    elapsed_s: float,
    peak_kb: float | None,
) -> tuple[Block, ...]:
    if not blocks:
        return blocks
    target_provider_id = str(primary_provider_id or _provider_id_for_block(blocks[0]) or "")
    annotated = False
    result: list[Block] = []
    for block in blocks:
        provider_id = _provider_id_for_block(block)
        if not annotated and (not target_provider_id or provider_id == target_provider_id):
            metadata = dict(block.metadata or {})
            metadata.setdefault("provider_elapsed_s", elapsed_s)
            if peak_kb is not None:
                metadata.setdefault("peak_kb", peak_kb)
            result.append(
                Block(
                    block_id=block.block_id,
                    doc_id=block.doc_id,
                    type=block.type,
                    content=block.content,
                    metadata=metadata,
                )
            )
            annotated = True
        else:
            result.append(block)
    if not annotated:
        first = result[0]
        metadata = dict(first.metadata or {})
        metadata.setdefault("provider_elapsed_s", elapsed_s)
        if peak_kb is not None:
            metadata.setdefault("peak_kb", peak_kb)
        result[0] = Block(
            block_id=first.block_id,
            doc_id=first.doc_id,
            type=first.type,
            content=first.content,
            metadata=metadata,
        )
    return tuple(result)


def _provider_report_for_outcome(
    *,
    runtime: Any,
    outcome: ParseOutcome,
    elapsed_s: float,
    peak_kb: float | None,
) -> dict[str, Any]:
    # Provider projection is intentionally comprehensive (IR, coverage,
    # quality gate and comparison axes) and is expensive for large PDFs.  The
    # previous implementation built it twice solely to discover the primary
    # provider before attaching elapsed/memory observability to one block.
    # The projection's primary-provider rule is simply max block count with a
    # stable id tie-break, so compute that small aggregate first and project
    # once with the metrics already attached.
    provider_counts = Counter(_provider_id_for_block(block) for block in outcome.blocks)
    primary_provider_id = min(
        provider_counts,
        key=lambda provider_id: (-provider_counts[provider_id], str(provider_id)),
        default="",
    )
    annotated_blocks = _annotate_provider_sample_metrics(
        outcome.blocks,
        primary_provider_id=str(primary_provider_id or "") or None,
        elapsed_s=elapsed_s,
        peak_kb=peak_kb,
    )
    return _document_providers_projection(
        _snapshot_for_provider_report(runtime, outcome, annotated_blocks)
    )


def _quality_projection_summary(*, runtime: Any, outcome: ParseOutcome) -> dict[str, Any]:
    """Keep the quality endpoint's diagnostic summary without duplicating pages."""

    report = _document_quality_projection(
        _snapshot_for_provider_report(runtime, outcome, outcome.blocks)
    )
    return {
        "schema_version": report.get("schema_version"),
        "projection": report.get("projection"),
        "quality_gate": report.get("quality_gate"),
        "quality_summary": report.get("quality_summary"),
        "coverage_summary": report.get("coverage_summary"),
        "rag_coverage_quality": report.get("rag_coverage_quality"),
    }


def _content_block_count(*, outcome: ParseOutcome, provider_report: Mapping[str, Any]) -> int:
    rag_coverage = provider_report.get("rag_coverage_quality")
    if isinstance(rag_coverage, Mapping):
        count = _as_int(rag_coverage.get("total_indexable_units"), default=-1)
        if count >= 0:
            return count
    return sum(
        1
        for block in outcome.blocks
        if str((block.metadata or {}).get("index_policy") or "index").strip().lower() != "skip"
    )


def _parser_lifecycle(
    *,
    runtime: Any,
    cache_before: Mapping[str, Any],
    cache_after: Mapping[str, Any],
    runtime_generation: int,
    reuse_runtime: bool,
    runtime_created_for_run: bool,
    runtime_build_elapsed_s: float | None,
) -> dict[str, Any]:
    return {
        "mode": "reused_runtime" if reuse_runtime else "fresh_runtime",
        "phase": "warm" if _as_int(cache_before.get("size")) > 0 else "cold",
        "runtime_generation": int(runtime_generation),
        "runtime_created_for_run": bool(runtime_created_for_run),
        "runtime_build_elapsed_s": _round_optional(runtime_build_elapsed_s),
        "parser_instance_count": len(tuple(getattr(runtime, "parsers", ()) or ())),
        "parser_instances": _parser_instance_labels(runtime),
        "pipeline_cache": {
            "before": dict(cache_before),
            "after": dict(cache_after),
            "delta": _cache_delta(cache_before, cache_after),
        },
    }


def _performance_request_options(cache_mode: str) -> dict[str, Any]:
    """Return request-only cache controls for a reproducible measurement lane."""

    normalized = str(cache_mode).strip().lower()
    if normalized == "disabled":
        # These controls are request scoped: they never delete, invalidate, or
        # otherwise mutate any cache entries already held by the product.
        return {
            "parse_cache": False,
            "post_process": {"ocr_cache": False},
        }
    if normalized == "ocr_warm":
        # The page OCR cache is retained as an explicit, separately auditable
        # warm dependency.  The full-document cache remains off so every run
        # still traverses the parser and persistence path.
        return {
            "parse_cache": False,
            "post_process": {"ocr_cache": True},
        }
    if normalized == "configured":
        return {}
    raise ValueError(f"unsupported cache_mode={cache_mode!r}; expected one of {CACHE_MODES}")


def _cache_state(*, cache_mode: str, blocks: Iterable[Block] = ()) -> dict[str, Any]:
    metadata = [
        dict(block.metadata or {})
        for block in blocks
        if isinstance(getattr(block, "metadata", None), Mapping)
    ]
    parse_states = sorted(
        {
            str(item.get("parse_cache_state"))
            for item in metadata
            if item.get("parse_cache_state") is not None
        }
    )
    return {
        "requested_mode": str(cache_mode).strip().lower(),
        "request_options": _performance_request_options(cache_mode),
        "observed_block_count": len(metadata),
        "parse_cache": {
            "observed_states": parse_states,
            "observed_hit_blocks": sum(bool(item.get("parse_cache_hit")) for item in metadata),
        },
        "ocr_cache": {
            "observed_cache_hit_blocks": sum(
                item.get("ocr_acceptance_reason") == "cache_hit" for item in metadata
            ),
        },
    }


def _run_one(
    *,
    runtime: Any,
    path: Path,
    index: int,
    track_python_memory: bool = True,
    run_number: int | None = None,
    doc_id: str | None = None,
    runtime_generation: int = 1,
    reuse_runtime: bool = True,
    runtime_created_for_run: bool = False,
    runtime_build_elapsed_s: float | None = None,
    process_telemetry: bool = True,
    process_sample_interval_ms: int = DEFAULT_PROCESS_SAMPLE_INTERVAL_MS,
    include_projections: bool = True,
    cache_mode: str = DEFAULT_CACHE_MODE,
) -> dict[str, Any]:
    size_bytes = path.stat().st_size
    media_type = _media_type_for(path)
    cache_before = _pipeline_cache_snapshot(runtime)
    tracemalloc_started_here = bool(track_python_memory and not tracemalloc.is_tracing())
    if tracemalloc_started_here:
        tracemalloc.start()
    sampler = _ProcessTelemetrySampler(
        enabled=process_telemetry,
        interval_ms=process_sample_interval_ms,
    )
    sampler.start()
    started = time.perf_counter()
    try:
        outcome = runtime.submit(
            ParseRequest(
                doc_id=doc_id or f"perf-sample-{index}",
                file_path=str(path),
                media_type=media_type,
                tenant_id="perf-baseline",
                quota_key="parse",
                quota_units=1,
                options=_performance_request_options(cache_mode),
            )
        )
        _current_bytes, peak_bytes = (
            tracemalloc.get_traced_memory() if track_python_memory else (0, 0)
        )
    except Exception as exc:
        _current_bytes, peak_bytes = (
            tracemalloc.get_traced_memory() if track_python_memory else (0, 0)
        )
        elapsed_s = _round(time.perf_counter() - started)
        execution_telemetry = sampler.stop()
        if tracemalloc_started_here:
            tracemalloc.stop()
        cache_after = _pipeline_cache_snapshot(runtime)
        return {
            "run_number": run_number,
            "document": str(path),
            "file_name": path.name,
            "suffix": path.suffix.lower(),
            "status": "failed",
            "media_type": media_type,
            "size_bytes": size_bytes,
            "elapsed_s": elapsed_s,
            "peak_kb": _round(peak_bytes / 1024) if track_python_memory else None,
            "error": str(exc),
            "process_telemetry": execution_telemetry,
            "parser_lifecycle": _parser_lifecycle(
                runtime=runtime,
                cache_before=cache_before,
                cache_after=cache_after,
                runtime_generation=runtime_generation,
                reuse_runtime=reuse_runtime,
                runtime_created_for_run=runtime_created_for_run,
                runtime_build_elapsed_s=runtime_build_elapsed_s,
            ),
            "stage_timings": {},
            "cache_state": _cache_state(cache_mode=cache_mode),
        }
    elapsed_s = _round(time.perf_counter() - started)
    execution_telemetry = sampler.stop()
    if tracemalloc_started_here:
        tracemalloc.stop()
    peak_kb = _round(peak_bytes / 1024) if track_python_memory else None
    cache_after = _pipeline_cache_snapshot(runtime)
    lifecycle = _parser_lifecycle(
        runtime=runtime,
        cache_before=cache_before,
        cache_after=cache_after,
        runtime_generation=runtime_generation,
        reuse_runtime=reuse_runtime,
        runtime_created_for_run=runtime_created_for_run,
        runtime_build_elapsed_s=runtime_build_elapsed_s,
    )
    table_blocks = [block for block in outcome.blocks if block.type == BlockType.TABLE]
    result: dict[str, Any] = {
        "run_number": run_number,
        "document": str(path),
        "file_name": path.name,
        "suffix": path.suffix.lower(),
        "status": outcome.job.state.value,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "elapsed_s": elapsed_s,
        "peak_kb": peak_kb,
        "mb_per_s": _round((size_bytes / 1048576) / elapsed_s) if elapsed_s > 0 else 0.0,
        "blocks": len(outcome.blocks),
        "raw_blocks": len(outcome.blocks),
        "chunks": len(outcome.chunks),
        "tables": len(table_blocks),
        "process_telemetry": execution_telemetry,
        "parser_lifecycle": lifecycle,
        "cache_state": _cache_state(cache_mode=cache_mode, blocks=outcome.blocks),
        "stage_timings": {
            key: _round(value)
            for key, value in dict(getattr(outcome, "stage_timings", {}) or {}).items()
        },
    }
    if not include_projections:
        result["observation_scope"] = "warmup_runtime_submit_only"
        return result

    provider_started = time.perf_counter()
    try:
        provider_report = _provider_report_for_outcome(
            runtime=runtime,
            outcome=outcome,
            elapsed_s=elapsed_s,
            peak_kb=peak_kb,
        )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"provider_projection_failed:{type(exc).__name__}:{exc}"
        result["stage_timings"]["provider_projection"] = _round(time.perf_counter() - provider_started)
        return result
    result["stage_timings"]["provider_projection"] = _round(time.perf_counter() - provider_started)

    quality_started = time.perf_counter()
    try:
        quality_projection = _quality_projection_summary(runtime=runtime, outcome=outcome)
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"quality_projection_failed:{type(exc).__name__}:{exc}"
        result["stage_timings"]["quality_projection"] = _round(time.perf_counter() - quality_started)
        return result
    result["stage_timings"]["quality_projection"] = _round(time.perf_counter() - quality_started)

    comparison_report = provider_report.get("comparison_report") or {}
    rankings = comparison_report.get("rankings") or []
    best_ranking = rankings[0] if rankings else {}
    provider_summary = provider_report.get("summary") or {}
    fingerprint = {
        "raw_blocks": len(outcome.blocks),
        "content_blocks": _content_block_count(outcome=outcome, provider_report=provider_report),
        "chunks": len(outcome.chunks),
        "tables": len(table_blocks),
        "figures": _as_int(provider_summary.get("total_figures")),
        "pages": _as_int(provider_summary.get("total_pages")),
    }
    result.update(
        {
            "content_blocks": fingerprint["content_blocks"],
            "figures": fingerprint["figures"],
            "pages": fingerprint["pages"],
            "fingerprint": fingerprint,
            "primary_provider_id": provider_summary.get("primary_provider_id"),
            "best_provider_id": comparison_report.get("best_provider_id"),
            "best_provider_score": best_ranking.get("score"),
            "provider_report": provider_report,
            "quality_projection": quality_projection,
        }
    )
    return result


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed_values = [
        float(item.get("elapsed_s", 0.0))
        for item in results
        if item.get("status") != "failed"
    ]
    slowest = max(results, key=lambda item: float(item.get("elapsed_s", 0.0)), default={})
    peak_values = [
        float(item["peak_kb"])
        for item in results
        if item.get("status") != "failed" and item.get("peak_kb") is not None
    ]
    elapsed_distribution = _distribution(elapsed_values)
    peak_distribution = _distribution(peak_values)
    return {
        "documents": len(results),
        "failed_documents": sum(1 for item in results if item.get("status") == "failed"),
        "total_elapsed_s": _round(sum(float(item.get("elapsed_s", 0.0)) for item in results)),
        "median_elapsed_s": elapsed_distribution["median"] if elapsed_values else 0.0,
        "p50_elapsed_s": elapsed_distribution["p50"],
        "p95_elapsed_s": elapsed_distribution["p95"],
        "max_elapsed_s": elapsed_distribution["max"] if elapsed_values else 0.0,
        "elapsed_cv_pct": elapsed_distribution["cv_pct"],
        "max_peak_kb": peak_distribution["max"],
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in results),
        "total_blocks": sum(int(item.get("blocks") or 0) for item in results),
        "total_chunks": sum(int(item.get("chunks") or 0) for item in results),
        "total_tables": sum(int(item.get("tables") or 0) for item in results),
        "slowest_document": slowest.get("file_name"),
    }


def _run_fingerprint(result: Mapping[str, Any]) -> dict[str, int]:
    raw = result.get("fingerprint")
    source = raw if isinstance(raw, Mapping) else result
    return {
        "raw_blocks": _as_int(source.get("raw_blocks") if isinstance(source, Mapping) else 0),
        "content_blocks": _as_int(
            source.get("content_blocks") if isinstance(source, Mapping) else 0,
            default=_as_int(result.get("chunks")),
        ),
        "chunks": _as_int(source.get("chunks") if isinstance(source, Mapping) else 0),
        "tables": _as_int(source.get("tables") if isinstance(source, Mapping) else 0),
        "figures": _as_int(source.get("figures") if isinstance(source, Mapping) else 0),
        "pages": _as_int(source.get("pages") if isinstance(source, Mapping) else 0),
    }


def _lifecycle_key(result: Mapping[str, Any]) -> str:
    lifecycle = result.get("parser_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return "unknown"
    mode = str(lifecycle.get("mode") or "unknown")
    phase = str(lifecycle.get("phase") or "unknown")
    cache_state = result.get("cache_state")
    cache_mode = (
        str(cache_state.get("requested_mode") or "unknown")
        if isinstance(cache_state, Mapping)
        else "unknown"
    )
    return f"{mode}:{phase}:cache={cache_mode}"


def _same_json(values: Iterable[Mapping[str, Any]]) -> bool:
    serialized = [json.dumps(value, sort_keys=True, ensure_ascii=False) for value in values]
    return bool(serialized) and len(set(serialized)) == 1


def _aggregate_stage_timings(results: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    values_by_stage: dict[str, list[float]] = defaultdict(list)
    for result in results:
        timings = result.get("stage_timings")
        if not isinstance(timings, Mapping):
            continue
        for name, value in timings.items():
            try:
                values_by_stage[str(name)].append(float(value))
            except (TypeError, ValueError):
                continue
    return {name: _distribution(values) for name, values in sorted(values_by_stage.items())}


def _aggregate_process_telemetry(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    telemetry = [
        item.get("process_telemetry")
        for item in results
        if isinstance(item.get("process_telemetry"), Mapping)
        and item.get("process_telemetry", {}).get("status") == "available"
    ]
    if not telemetry:
        return {
            "available_runs": 0,
            "status": "unavailable",
        }

    def values(section: str, key: str) -> list[float]:
        result: list[float] = []
        for item in telemetry:
            mapping = item.get(section)
            if not isinstance(mapping, Mapping):
                continue
            try:
                result.append(float(mapping.get(key)))
            except (TypeError, ValueError):
                continue
        return result

    return {
        "available_runs": len(telemetry),
        "status": "available",
        "peak_rss_bytes": _distribution(values("peak", "rss_bytes")),
        "peak_working_set_bytes": _distribution(values("peak", "working_set_bytes")),
        "cpu_total_s": _distribution(values("delta", "cpu_total_s")),
        "cpu_user_s": _distribution(values("delta", "cpu_user_s")),
        "cpu_system_s": _distribution(values("delta", "cpu_system_s")),
        "io_read_bytes": _distribution(values("delta", "io_read_bytes")),
        "io_write_bytes": _distribution(values("delta", "io_write_bytes")),
    }


def _stability_groups(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[_lifecycle_key(result)].append(result)
    report: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        successful = [item for item in group if item.get("status") != "failed"]
        fingerprints = [_run_fingerprint(item) for item in successful if item.get("fingerprint")]
        report.append(
            {
                "key": key,
                "run_numbers": [item.get("run_number") for item in group],
                "run_count": len(group),
                "successful_runs": len(successful),
                "success_rate_pct": _round(len(successful) / len(group) * 100.0) if group else 0.0,
                "elapsed_s": _distribution(
                    float(item.get("elapsed_s") or 0.0) for item in successful
                ),
                "peak_kb": _distribution(
                    float(item.get("peak_kb"))
                    for item in successful
                    if item.get("peak_kb") is not None
                ),
                "fingerprints_identical": _same_json(fingerprints),
                "stage_timings_s": _aggregate_stage_timings(successful),
                "process_telemetry": _aggregate_process_telemetry(successful),
            }
        )
    return report


def _outlier_runs(
    results: list[dict[str, Any]],
    *,
    median_multiplier: float,
) -> list[dict[str, Any]]:
    successful = [item for item in results if item.get("status") != "failed"]
    elapsed = _distribution(float(item.get("elapsed_s") or 0.0) for item in successful)
    median = elapsed.get("p50")
    if median is None or float(median) <= 0:
        return []
    threshold = float(median) * float(median_multiplier)
    outliers: list[dict[str, Any]] = []
    for item in successful:
        if float(item.get("elapsed_s") or 0.0) <= threshold:
            continue
        outliers.append(
            {
                "run_number": item.get("run_number"),
                "elapsed_s": item.get("elapsed_s"),
                "threshold_s": _round(threshold),
                "fingerprint": item.get("fingerprint"),
                "stage_timings": item.get("stage_timings"),
                "process_telemetry": item.get("process_telemetry"),
                "parser_lifecycle": item.get("parser_lifecycle"),
                "cache_state": item.get("cache_state"),
            }
        )
    return outliers


def _load_stability_policy(
    policy: str | Path | Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if policy is None:
        return None, None
    if isinstance(policy, Mapping):
        return dict(policy), "inline"
    path = Path(policy)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"stability policy must be a JSON object: {path}")
    return payload, str(path.resolve())


def _gate(name: str, passed: bool, summary: str) -> dict[str, Any]:
    return {"name": name, "status": "passed" if passed else "failed", "summary": summary}


def _policy_gates(
    *,
    results: list[dict[str, Any]],
    sample_paths: list[Path],
    track_python_memory: bool,
    policy: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if policy is None:
        return [], []
    successful = [item for item in results if item.get("status") != "failed"]
    elapsed = _distribution(float(item.get("elapsed_s") or 0.0) for item in successful)
    peaks = _distribution(
        float(item.get("peak_kb"))
        for item in successful
        if item.get("peak_kb") is not None
    )
    groups = _stability_groups(results)
    fingerprint_policy = policy.get("fingerprint")
    sample_policy = policy.get("sample")
    measurement_policy = policy.get("measurement")
    lane_key = "tracked_memory" if track_python_memory else "latency"
    lane = policy.get(lane_key)
    lane = lane if isinstance(lane, Mapping) else {}
    gates: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    if isinstance(sample_policy, Mapping) and sample_policy.get("sha256"):
        actual_hash = _sha256(sample_paths[0]) if len(sample_paths) == 1 and sample_paths[0].is_file() else None
        expected_hash = str(sample_policy.get("sha256") or "").upper()
        gates.append(
            _gate(
                "sample_identity",
                actual_hash == expected_hash,
                f"sha256={actual_hash} expected={expected_hash}",
            )
        )

    if isinstance(measurement_policy, Mapping) and measurement_policy.get("cache_mode") is not None:
        expected_cache_mode = str(measurement_policy.get("cache_mode") or "").strip().lower()
        observed_cache_modes = [
            str((item.get("cache_state") or {}).get("requested_mode") or "unknown")
            if isinstance(item.get("cache_state"), Mapping)
            else "unknown"
            for item in results
        ]
        gates.append(
            _gate(
                "measurement_cache_mode",
                bool(observed_cache_modes) and all(mode == expected_cache_mode for mode in observed_cache_modes),
                f"expected={expected_cache_mode} observed={observed_cache_modes}",
            )
        )
        if measurement_policy.get("require_parse_cache_bypass"):
            parse_cache_bypassed = bool(results) and all(
                isinstance(item.get("cache_state"), Mapping)
                and (item["cache_state"].get("parse_cache") or {}).get("observed_states") == ["disabled"]
                and int((item["cache_state"].get("parse_cache") or {}).get("observed_hit_blocks") or 0) == 0
                for item in results
            )
            gates.append(
                _gate(
                    "measurement_parse_cache_bypass",
                    parse_cache_bypassed,
                    "parse cache reported disabled with no observed full-document cache hit",
                )
            )
        if measurement_policy.get("require_ocr_cache_hit"):
            ocr_cache_warm = bool(results) and all(
                isinstance(item.get("cache_state"), Mapping)
                and int((item["cache_state"].get("ocr_cache") or {}).get("observed_cache_hit_blocks") or 0) > 0
                for item in results
            )
            gates.append(
                _gate(
                    "measurement_ocr_cache_warm",
                    ocr_cache_warm,
                    "each measured run observed at least one page OCR-cache hit",
                )
            )
        if measurement_policy.get("require_no_ocr_cache_hits"):
            ocr_cache_bypassed = bool(results) and all(
                isinstance(item.get("cache_state"), Mapping)
                and int((item["cache_state"].get("ocr_cache") or {}).get("observed_cache_hit_blocks") or 0) == 0
                for item in results
            )
            gates.append(
                _gate(
                    "measurement_ocr_cache_bypass",
                    ocr_cache_bypassed,
                    "no page OCR-cache hit was observed",
                )
            )

    if isinstance(fingerprint_policy, Mapping):
        expected = {key: _as_int(fingerprint_policy.get(key)) for key in fingerprint_policy}
        fingerprints = [_run_fingerprint(item) for item in successful]
        fingerprint_passed = bool(fingerprints) and all(
            all(fingerprint.get(key) == value for key, value in expected.items())
            for fingerprint in fingerprints
        )
        gates.append(
            _gate(
                "structural_fingerprint",
                fingerprint_passed,
                f"expected={expected} runs={len(fingerprints)}",
            )
        )

    minimum_runs = _as_int(lane.get("min_runs"), default=0)
    if minimum_runs:
        gates.append(
            _gate(
                f"{lane_key}_minimum_runs",
                len(results) >= minimum_runs,
                f"runs={len(results)} minimum={minimum_runs}",
            )
        )
    minimum_success_rate = lane.get("min_success_rate_pct")
    if minimum_success_rate is not None:
        success_rate = len(successful) / len(results) * 100.0 if results else 0.0
        gates.append(
            _gate(
                f"{lane_key}_success_rate",
                success_rate >= float(minimum_success_rate),
                f"success_rate={_round(success_rate)}% minimum={minimum_success_rate}%",
            )
        )
    if lane.get("require_uniform_lifecycle"):
        gates.append(
            _gate(
                f"{lane_key}_uniform_lifecycle",
                len(groups) == 1,
                f"comparison_groups={len(groups)} keys={[group['key'] for group in groups]}",
            )
        )
    if not track_python_memory:
        if lane.get("max_p50_s") is not None:
            p50 = elapsed.get("p50")
            gates.append(
                _gate(
                    "latency_p50_budget",
                    p50 is not None and float(p50) <= float(lane["max_p50_s"]),
                    f"p50={p50}s maximum={lane['max_p50_s']}s",
                )
            )
        if lane.get("max_cv_pct") is not None:
            cv_pct = elapsed.get("cv_pct")
            gates.append(
                _gate(
                    "latency_cv_budget",
                    cv_pct is not None and float(cv_pct) <= float(lane["max_cv_pct"]),
                    f"cv={cv_pct}% maximum={lane['max_cv_pct']}%",
                )
            )
    elif lane.get("max_mean_peak_kb") is not None:
        mean_peak = peaks.get("mean")
        gates.append(
            _gate(
                "python_peak_memory_budget",
                mean_peak is not None and float(mean_peak) <= float(lane["max_mean_peak_kb"]),
                f"mean_peak={mean_peak}KB maximum={lane['max_mean_peak_kb']}KB",
            )
        )

    multiplier = float(lane.get("tail_outlier_median_multiplier") or 1.2)
    outliers = _outlier_runs(results, median_multiplier=multiplier)
    if track_python_memory and outliers:
        observations.append(
            {
                "code": "tracemalloc_elapsed_tail_outlier",
                "severity": "observation",
                "summary": (
                    f"{len(outliers)} tracked run(s) exceeded {multiplier}x the p50; "
                    "per-run stage and process telemetry are retained in stability.outliers"
                ),
            }
        )
    return gates, observations


def _stability_report(
    *,
    results: list[dict[str, Any]],
    sample_paths: list[Path],
    track_python_memory: bool,
    policy: Mapping[str, Any] | None,
    policy_source: str | None,
) -> dict[str, Any]:
    successful = [item for item in results if item.get("status") != "failed"]
    groups = _stability_groups(results)
    lane = policy.get("tracked_memory" if track_python_memory else "latency") if policy else {}
    multiplier = float(lane.get("tail_outlier_median_multiplier") or 1.2) if isinstance(lane, Mapping) else 1.2
    gates, observations = _policy_gates(
        results=results,
        sample_paths=sample_paths,
        track_python_memory=track_python_memory,
        policy=policy,
    )
    failed_gates = [gate for gate in gates if gate["status"] != "passed"]
    return {
        "schema_version": STABILITY_SCHEMA_VERSION,
        "policy_source": policy_source,
        "status": "not_configured" if policy is None else "failed" if failed_gates else "passed",
        "comparison_valid": len(groups) == 1,
        "comparison_groups": groups,
        "run_count": len(results),
        "successful_runs": len(successful),
        "success_rate_pct": _round(len(successful) / len(results) * 100.0) if results else 0.0,
        "elapsed_s": _distribution(float(item.get("elapsed_s") or 0.0) for item in successful),
        "peak_kb": _distribution(
            float(item.get("peak_kb"))
            for item in successful
            if item.get("peak_kb") is not None
        ),
        "stage_timings_s": _aggregate_stage_timings(successful),
        "process_telemetry": _aggregate_process_telemetry(successful),
        "outlier_policy": {"median_multiplier": multiplier},
        "outliers": _outlier_runs(results, median_multiplier=multiplier),
        "gates": gates,
        "observations": observations,
    }


def _close_runtime(runtime: Any) -> None:
    for resource_name in ("job_store", "index"):
        resource = getattr(runtime, resource_name, None)
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def build_report(
    *,
    config: str | Path,
    sample_dir: str | Path,
    samples: list[str | Path] | None = None,
    extensions: set[str] | None = None,
    max_files: int = 20,
    track_python_memory: bool = True,
    runs: int = 1,
    warmup_runs: int | None = None,
    reuse_runtime: bool = True,
    process_telemetry: bool = True,
    process_sample_interval_ms: int = DEFAULT_PROCESS_SAMPLE_INTERVAL_MS,
    cache_mode: str = DEFAULT_CACHE_MODE,
    stability_policy: str | Path | Mapping[str, Any] | None = None,
    enforce_stability_gate: bool = False,
) -> dict[str, Any]:
    cache_mode = str(cache_mode).strip().lower()
    _performance_request_options(cache_mode)
    sample_root = Path(sample_dir)
    paths = [Path(item) for item in samples or []]
    if not paths:
        paths = _discover_samples(
            sample_dir=sample_root,
            extensions=extensions or set(DEFAULT_EXTENSIONS),
            max_files=max_files,
        )
    run_count = max(1, int(runs))
    if run_count > 1 and len(paths) != 1:
        raise ValueError("multi-run stability measurement requires exactly one resolved sample")
    resolved_warmup_runs = (
        int(warmup_runs)
        if warmup_runs is not None
        else 1 if run_count > 1 and reuse_runtime else 0
    )
    if resolved_warmup_runs < 0:
        raise ValueError("warmup_runs must be non-negative")
    if resolved_warmup_runs and not reuse_runtime:
        raise ValueError("warmup_runs requires --reuse-runtime so warm cache state reaches measured runs")
    if not paths:
        run_count = 0
        resolved_warmup_runs = 0
    policy, policy_source = _load_stability_policy(stability_policy)

    results: list[dict[str, Any]] = []
    warmup_results: list[dict[str, Any]] = []
    runtime: Any | None = None
    runtime_generation = 0
    persistent_build_elapsed_s: float | None = None

    def acquire_runtime() -> tuple[Any, bool, float]:
        nonlocal runtime, runtime_generation, persistent_build_elapsed_s
        if reuse_runtime and runtime is not None:
            return runtime, False, 0.0
        build_started = time.perf_counter()
        created = build_runtime(config)
        build_elapsed_s = _round(time.perf_counter() - build_started)
        runtime_generation += 1
        if reuse_runtime:
            runtime = created
            persistent_build_elapsed_s = build_elapsed_s
        return created, True, build_elapsed_s

    try:
        for warmup_index in range(1, resolved_warmup_runs + 1):
            active_runtime, created, build_elapsed_s = acquire_runtime()
            try:
                warmup_results.append(
                    _run_one(
                        runtime=active_runtime,
                        path=paths[0],
                        index=warmup_index,
                        run_number=warmup_index,
                        doc_id=f"perf-warmup-{warmup_index}",
                        track_python_memory=track_python_memory,
                        runtime_generation=runtime_generation,
                        reuse_runtime=reuse_runtime,
                        runtime_created_for_run=created,
                        runtime_build_elapsed_s=build_elapsed_s,
                        process_telemetry=process_telemetry,
                        process_sample_interval_ms=process_sample_interval_ms,
                        include_projections=False,
                        cache_mode=cache_mode,
                    )
                )
            finally:
                if not reuse_runtime:
                    _close_runtime(active_runtime)

        for run_number in range(1, run_count + 1):
            if run_count == 1 and len(paths) > 1:
                active_runtime, created, build_elapsed_s = acquire_runtime()
                try:
                    for sample_index, path in enumerate(paths, start=1):
                        results.append(
                            _run_one(
                                runtime=active_runtime,
                                path=path,
                                index=sample_index,
                                run_number=sample_index,
                                track_python_memory=track_python_memory,
                                runtime_generation=runtime_generation,
                                reuse_runtime=reuse_runtime,
                                runtime_created_for_run=created and sample_index == 1,
                                runtime_build_elapsed_s=build_elapsed_s if sample_index == 1 else None,
                                process_telemetry=process_telemetry,
                                process_sample_interval_ms=process_sample_interval_ms,
                                cache_mode=cache_mode,
                            )
                        )
                finally:
                    if not reuse_runtime:
                        _close_runtime(active_runtime)
                break

            active_runtime, created, build_elapsed_s = acquire_runtime()
            try:
                results.append(
                    _run_one(
                        runtime=active_runtime,
                        path=paths[0],
                        index=run_number,
                        run_number=run_number,
                        doc_id=f"perf-sample-{run_number}",
                        track_python_memory=track_python_memory,
                        runtime_generation=runtime_generation,
                        reuse_runtime=reuse_runtime,
                        runtime_created_for_run=created,
                        runtime_build_elapsed_s=build_elapsed_s,
                        process_telemetry=process_telemetry,
                        process_sample_interval_ms=process_sample_interval_ms,
                        cache_mode=cache_mode,
                    )
                )
            finally:
                if not reuse_runtime:
                    _close_runtime(active_runtime)
    finally:
        if reuse_runtime and runtime is not None:
            _close_runtime(runtime)

    stability = _stability_report(
        results=results,
        sample_paths=paths,
        track_python_memory=track_python_memory,
        policy=policy,
        policy_source=policy_source,
    )
    status = "ok"
    if (
        not paths
        or any(item.get("status") == "failed" for item in results)
        or any(item.get("status") == "failed" for item in warmup_results)
        or (enforce_stability_gate and stability.get("status") == "failed")
    ):
        status = "failed"
    return {
        "schema_version": STABILITY_SCHEMA_VERSION,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": str(Path(config).resolve()),
        "sample_dir": str(sample_root.resolve()),
        "extensions": sorted(extensions or set(DEFAULT_EXTENSIONS)),
        "measurement": {
            "elapsed_scope": "runtime.submit_end_to_end",
            "track_python_memory": bool(track_python_memory),
            "lane": "python_allocation_tracked" if track_python_memory else "clean_latency",
            "cache": {
                "mode": cache_mode,
                "request_options": _performance_request_options(cache_mode),
                "scope": "request_scoped_no_cache_file_mutation",
            },
            "runtime_lifecycle": {
                "reuse_runtime": bool(reuse_runtime),
                "warmup_runs": resolved_warmup_runs,
                "comparison_rule": "Only identical runtime mode, pipeline-cache phase, and request cache mode are aggregated for gates.",
                "initial_runtime_build_elapsed_s": persistent_build_elapsed_s,
            },
            "process_telemetry": {
                "enabled": bool(process_telemetry),
                "sample_interval_ms": max(10, int(process_sample_interval_ms)),
                "dependency": "optional_psutil_tooling_only",
            },
        },
        "summary": _summary(results),
        "results": results,
        "warmup_results": warmup_results,
        "stability": stability,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# ParseCore Parse Performance Baseline",
        "",
        f"- status: **{payload.get('status')}**",
        f"- sample_dir: `{payload.get('sample_dir')}`",
        f"- elapsed_scope: `{(payload.get('measurement') or {}).get('elapsed_scope', 'unknown')}`",
        f"- track_python_memory: `{bool((payload.get('measurement') or {}).get('track_python_memory', True))}`",
        f"- cache_mode: `{((payload.get('measurement') or {}).get('cache') or {}).get('mode', 'unknown')}`",
        f"- documents: {summary.get('documents', 0)}",
        f"- total_elapsed_s: {summary.get('total_elapsed_s', 0)}",
        f"- max_peak_kb: {summary.get('max_peak_kb', 0)}",
        "",
        "| document | status | primary_provider | best_provider | provider_score | size_bytes | elapsed_s | peak_kb | mb_per_s | blocks | chunks | tables |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in payload.get("results") or []:
        lines.append(
            "| {file_name} | {status} | {primary_provider_id} | {best_provider_id} | {best_provider_score} | {size_bytes} | {elapsed_s} | {peak_kb} | {mb_per_s} | {blocks} | {chunks} | {tables} |".format(
                file_name=item.get("file_name", ""),
                status=item.get("status", ""),
                primary_provider_id=item.get("primary_provider_id", ""),
                best_provider_id=item.get("best_provider_id", ""),
                best_provider_score=item.get("best_provider_score", ""),
                size_bytes=item.get("size_bytes", 0),
                elapsed_s=item.get("elapsed_s", ""),
                peak_kb=item.get("peak_kb", ""),
                mb_per_s=item.get("mb_per_s", ""),
                blocks=item.get("blocks", 0),
                chunks=item.get("chunks", 0),
                tables=item.get("tables", 0),
            )
        )
    stability = payload.get("stability") or {}
    if stability:
        elapsed = stability.get("elapsed_s") or {}
        peaks = stability.get("peak_kb") or {}
        telemetry = stability.get("process_telemetry") or {}
        lines.extend(
            [
                "",
                "## Stability and telemetry",
                "",
                f"- stability_status: `{stability.get('status')}`",
                f"- comparison_valid: `{stability.get('comparison_valid')}`",
                f"- runs: {stability.get('run_count')} ({stability.get('successful_runs')} successful)",
                (
                    f"- elapsed_s: P50 {elapsed.get('p50')}, P95 {elapsed.get('p95')}, "
                    f"max {elapsed.get('max')}, CV {elapsed.get('cv_pct')}%"
                ),
                f"- peak_kb: mean {peaks.get('mean')}, max {peaks.get('max')}",
                (
                    f"- process_telemetry: {telemetry.get('status')}, "
                    f"available_runs={telemetry.get('available_runs')}"
                ),
            ]
        )
        groups = stability.get("comparison_groups") or []
        if groups:
            lines.extend(["", "| lifecycle group | runs | P50 s | P95 s | CV |", "| --- | ---: | ---: | ---: | ---: |"])
            for group in groups:
                group_elapsed = group.get("elapsed_s") or {}
                lines.append(
                    f"| {group.get('key')} | {group.get('run_count')} | {group_elapsed.get('p50')} | "
                    f"{group_elapsed.get('p95')} | {group_elapsed.get('cv_pct')}% |"
                )
        gates = stability.get("gates") or []
        if gates:
            lines.extend(["", "| stability gate | status | evidence |", "| --- | --- | --- |"])
            for gate in gates:
                lines.append(f"| {gate.get('name')} | {gate.get('status')} | {gate.get('summary')} |")
        observations = stability.get("observations") or []
        if observations:
            lines.extend(["", "### Observations", ""])
            for observation in observations:
                lines.append(f"- `{observation.get('code')}`: {observation.get('summary')}")
        if stability.get("outliers"):
            lines.append(f"- retained_outliers: {len(stability.get('outliers') or [])}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    explicit_samples = [Path(item) for item in args.sample] if args.sample else None
    extensions = _parse_extensions(args.extensions)
    payload = build_report(
        config=args.config,
        sample_dir=args.sample_dir,
        samples=explicit_samples,
        extensions=extensions,
        max_files=max(1, args.max_files),
        track_python_memory=bool(args.track_python_memory),
        runs=max(1, args.runs),
        warmup_runs=args.warmup_runs,
        reuse_runtime=bool(args.reuse_runtime),
        process_telemetry=bool(args.process_telemetry),
        process_sample_interval_ms=max(10, args.process_sample_interval_ms),
        cache_mode=args.cache_mode,
        stability_policy=args.stability_policy,
        enforce_stability_gate=bool(args.enforce_stability_gate),
    )
    if args.fail_on_errors and int((payload.get("summary") or {}).get("failed_documents") or 0) > 0:
        payload["status"] = "failed"

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out_json:
        output_path = Path(args.out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"[parse-perf-baseline] wrote {output_path}")
    else:
        print(text)

    if args.out_md:
        markdown_path = Path(args.out_md)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[parse-perf-baseline] wrote {markdown_path}")

    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
