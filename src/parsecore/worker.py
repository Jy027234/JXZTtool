from __future__ import annotations

from pathlib import Path
from time import sleep

from .bootstrap import build_runtime
from .models import ParseOutcome
from .runtime import ParseRuntime


class QueueWorker:
    def __init__(self, runtime: ParseRuntime) -> None:
        self.runtime = runtime

    def run_once(self) -> ParseOutcome | None:
        job = self.runtime.claim_next_job()
        if job is None:
            return None
        try:
            return self.runtime.execute(job_id=job.job_id)
        except Exception:
            return None

    def drain(self, *, max_jobs: int | None = None) -> int:
        processed = 0
        while max_jobs is None or processed < max_jobs:
            outcome = self.run_once()
            if outcome is None:
                break
            processed += 1
        return processed

    def serve_forever(self) -> None:
        poll_seconds = max(self.runtime.settings.runtime.poll_interval_ms, 50) / 1000
        while True:
            processed = self.drain(max_jobs=self.runtime.settings.runtime.max_workers)
            if processed == 0:
                sleep(poll_seconds)


def build_worker(config_path: str | Path = "parsecore.toml") -> QueueWorker:
    runtime = build_runtime(config_path)
    return QueueWorker(runtime)


def run_worker(
    config_path: str | Path = "parsecore.toml",
    *,
    once: bool = False,
    max_jobs: int | None = None,
) -> int:
    worker = build_worker(config_path)
    if once:
        return worker.drain(max_jobs=max_jobs or 1)
    if max_jobs is not None:
        return worker.drain(max_jobs=max_jobs)
    worker.serve_forever()
    return 0
