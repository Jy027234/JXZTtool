"""B1+B2 smoke test: retry counter, dead-letter transition, structured events."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.bootstrap import build_runtime  # type: ignore
from parsecore.models import ParseJobState, ParseRequest  # type: ignore


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="parsecore_b1b2_"))
    try:
        # Isolate DB + log file.
        cfg_path = tmp / "parsecore.toml"
        cfg_path.write_text(
            (ROOT / "parsecore.toml")
            .read_text(encoding="utf-8")
            .replace("./var/parsecore.db", f"{(tmp / 'jobs.db').as_posix()}")
            .replace(
                'log_path = "var/logs/job_events.jsonl"',
                f'log_path = "{(tmp / "events.jsonl").as_posix()}"',
            )
            .replace("max_attempts = 3", "max_attempts = 2"),
            encoding="utf-8",
        )
        runtime = build_runtime(cfg_path)

        # --- Case 1: forced failure (missing file) -> FAILED then DEAD-LETTER
        job = runtime.start(
            ParseRequest(
                doc_id="missing-doc",
                file_path=str(tmp / "does-not-exist.pdf"),
                media_type="application/pdf",
            )
        )
        for attempt in (1, 2):
            try:
                runtime.execute(job_id=job.job_id)
            except Exception as exc:  # noqa: BLE001
                print(f"attempt {attempt} failed: {type(exc).__name__}: {exc}")
        final = runtime.job_store.get_job(job_id=job.job_id)
        assert final is not None
        print(
            "missing-doc ->",
            "state=",
            final.state.value,
            "attempts=",
            final.attempt_count,
            "dead_lettered_at=",
            final.dead_lettered_at,
        )
        assert final.state == ParseJobState.FAILED
        assert final.attempt_count == 2
        assert final.dead_lettered_at is not None

        # Third attempt on dead-lettered job must refuse.
        try:
            runtime.execute(job_id=job.job_id)
        except RuntimeError as exc:
            print("dead-letter refusal:", exc)
        else:
            raise AssertionError("expected RuntimeError on dead-lettered job")

        # --- Case 2: success path on a plaintext sample -> attempt_count=1
        sample = tmp / "hello.txt"
        sample.write_text("hello world\n\nsecond paragraph", encoding="utf-8")
        job2 = runtime.start(
            ParseRequest(
                doc_id="hello",
                file_path=str(sample),
                media_type="text/plain",
            )
        )
        outcome = runtime.execute(job_id=job2.job_id)
        final2 = runtime.job_store.get_job(job_id=job2.job_id)
        assert final2 is not None
        print(
            "hello ->",
            "state=",
            final2.state.value,
            "attempts=",
            final2.attempt_count,
            "blocks=",
            len(outcome.blocks),
        )
        assert final2.state == ParseJobState.DONE
        assert final2.attempt_count == 1

        # --- Event log assertions
        events_path = tmp / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        by_event: dict[str, int] = {}
        for ev in events:
            by_event[ev["event"]] = by_event.get(ev["event"], 0) + 1
        print("event counts:", by_event)
        assert by_event.get("started", 0) >= 3  # 2 failed + 1 success
        assert by_event.get("failed", 0) >= 1
        assert by_event.get("dead_letter", 0) >= 1
        assert by_event.get("completed", 0) >= 1
        assert by_event.get("state_changed", 0) >= 3  # parsing/structuring/embedding for success

        print("B1+B2 smoke OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
