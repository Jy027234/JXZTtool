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
                payload[key] = value
            line = json.dumps(payload, ensure_ascii=False)
            with self._lock:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.write("\n")
        except OSError:
            # Logging must never break parsing.
            return


__all__ = ["JobEventLogger"]
