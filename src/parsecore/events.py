"""Structured per-job event logging (B2).

Each event is a single JSON object on its own line. The schema is intentionally
flat so it can be ingested by ``jq`` / Loki / DuckDB without preprocessing.

Required keys:
    timestamp    UTC ISO-8601
    event        one of: started, state_changed, retry, dead_letter, failed, completed
    job_id       parse-job id (when known)
    doc_id       document id (when known)

Optional keys (set per event):
    state        new ParseJobState value (state_changed)
    attempt      current attempt counter (retry, failed, dead_letter)
    error        error message (failed, dead_letter)
    duration_s   wall-clock seconds (completed)
    blocks       block count (completed)
    chunks       chunk count (completed)

Logger writes are append-only; if the destination directory does not exist it
is created lazily. Failures inside the logger are swallowed so logging never
breaks the parse pipeline.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, UTC
from pathlib import Path
from typing import Any


SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
)
REDACTED_VALUE = "[redacted]"


def _field_is_sensitive(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return any(marker in normalized for marker in SENSITIVE_FIELD_MARKERS)


def _redact_event_value(key: str, value: Any) -> Any:
    if _field_is_sensitive(key):
        return REDACTED_VALUE
    if isinstance(value, dict):
        return {str(child_key): _redact_event_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_event_value(key, item) for item in value]
    return value


class JobEventLogger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: str, **fields: Any) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
            }
            for key, value in fields.items():
                if value is None:
                    continue
                payload[key] = _redact_event_value(key, value)
            line = json.dumps(payload, ensure_ascii=False)
            with self._lock:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.write("\n")
        except OSError:
            # Logging must never break parsing.
            return

    # P7-T02: stage timing helpers ----------------------------------------

    def log_stage_start(
        self,
        stage: str,
        *,
        job_id: str | None = None,
        doc_id: str | None = None,
        part_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.log(
            "stage_started",
            stage=stage,
            job_id=job_id,
            doc_id=doc_id,
            part_id=part_id,
            tenant_id=tenant_id,
        )

    def log_stage_end(
        self,
        stage: str,
        *,
        job_id: str | None = None,
        doc_id: str | None = None,
        part_id: str | None = None,
        tenant_id: str | None = None,
        duration_s: float | None = None,
        error_category: str | None = None,
    ) -> None:
        self.log(
            "stage_completed" if error_category is None else "stage_failed",
            stage=stage,
            job_id=job_id,
            doc_id=doc_id,
            part_id=part_id,
            tenant_id=tenant_id,
            duration_s=duration_s,
            error_category=error_category,
        )


class ParseStageTimer:
    """Lightweight context-manager that times a parse stage and logs it.

    Usage::

        timer = ParseStageTimer(logger, job_id="j1", doc_id="d1")
        with timer.stage("parse"):
            ...
        with timer.stage("chunk"):
            ...
    """

    def __init__(
        self,
        logger: JobEventLogger,
        *,
        job_id: str | None = None,
        doc_id: str | None = None,
        part_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self._logger = logger
        self._job_id = job_id
        self._doc_id = doc_id
        self._part_id = part_id
        self._tenant_id = tenant_id
        self._stages: dict[str, float] = {}

    def stage(self, name: str) -> "_StageContext":
        return _StageContext(self, name)

    @property
    def elapsed(self) -> dict[str, float]:
        return dict(self._stages)

    def _start(self, name: str) -> None:
        import time
        self._stages[name] = 0.0
        self._logger.log_stage_start(
            name,
            job_id=self._job_id,
            doc_id=self._doc_id,
            part_id=self._part_id,
            tenant_id=self._tenant_id,
        )
        self._start_ns = time.perf_counter_ns()

    def _end(self, name: str, *, error_category: str | None = None) -> None:
        import time
        duration_s = (time.perf_counter_ns() - getattr(self, "_start_ns", 0)) / 1e9
        self._stages[name] = round(duration_s, 4)
        self._logger.log_stage_end(
            name,
            job_id=self._job_id,
            doc_id=self._doc_id,
            part_id=self._part_id,
            tenant_id=self._tenant_id,
            duration_s=round(duration_s, 4),
            error_category=error_category,
        )


class _StageContext:
    def __init__(self, timer: ParseStageTimer, name: str) -> None:
        self._timer = timer
        self._name = name

    def __enter__(self) -> "_StageContext":
        self._timer._start(self._name)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        error_cat = None
        if exc is not None:
            error_cat = str(getattr(exc, "error_category", None) or "parser_failed")
        self._timer._end(self._name, error_category=error_cat)


__all__ = ["JobEventLogger", "ParseStageTimer"]
