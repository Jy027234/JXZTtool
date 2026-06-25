"""Tests for P7-T02 (ParseStageTimer) and P7-T05 (error categories)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from parsecore.api_responses import (
    ERROR_CATEGORIES,
    PARSE_STAGES,
    error_category_for_code,
)
from parsecore.events import JobEventLogger, ParseStageTimer


class ErrorCategoryTests(unittest.TestCase):
    """P7-T05: error category taxonomy."""

    def test_all_known_categories_have_http_status(self) -> None:
        for name, info in ERROR_CATEGORIES.items():
            with self.subTest(name=name):
                self.assertIn("http_status", info)
                self.assertIn("description", info)
                self.assertIsInstance(info["http_status"], int)

    def test_error_category_for_known_code(self) -> None:
        self.assertEqual(error_category_for_code("file_too_large"), "invalid_input")
        self.assertEqual(error_category_for_code("parse_failed"), "parser_failed")
        self.assertEqual(error_category_for_code("job_timeout"), "timeout")
        self.assertEqual(error_category_for_code("document_not_found"), "not_found")
        self.assertEqual(error_category_for_code("ocr_provider_unreachable"), "provider_unavailable")
        self.assertEqual(error_category_for_code("quota_exceeded"), "quota_exceeded")
        self.assertEqual(error_category_for_code("database_error"), "storage_failed")

    def test_error_category_for_unknown_code_returns_unknown(self) -> None:
        self.assertEqual(error_category_for_code("some_random_error"), "unknown")
        self.assertEqual(error_category_for_code(""), "unknown")

    def test_parse_stages_tuple_is_frozen(self) -> None:
        self.assertIsInstance(PARSE_STAGES, tuple)
        self.assertIn("upload", PARSE_STAGES)
        self.assertIn("parse", PARSE_STAGES)
        self.assertIn("normalize", PARSE_STAGES)
        self.assertIn("chunk", PARSE_STAGES)
        self.assertIn("embed", PARSE_STAGES)
        self.assertIn("export", PARSE_STAGES)
        self.assertIn("rerun", PARSE_STAGES)


class ParseStageTimerTests(unittest.TestCase):
    """P7-T02: stage timing instrumentation."""

    def test_stage_timer_records_elapsed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            logger = JobEventLogger(log_path)
            timer = ParseStageTimer(logger, job_id="j1", doc_id="d1")
            with timer.stage("parse"):
                pass
            with timer.stage("chunk"):
                pass
        elapsed = timer.elapsed
        self.assertIn("parse", elapsed)
        self.assertIn("chunk", elapsed)
        self.assertGreaterEqual(elapsed["parse"], 0)
        self.assertGreaterEqual(elapsed["chunk"], 0)

    def test_stage_timer_logs_stage_events(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            logger = JobEventLogger(log_path)
            timer = ParseStageTimer(logger, job_id="j1", doc_id="d1")
            with timer.stage("parse"):
                pass
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line) for line in lines]
        event_types = [e["event"] for e in events]
        self.assertIn("stage_started", event_types)
        self.assertIn("stage_completed", event_types)
        parse_completed = [e for e in events if e["event"] == "stage_completed" and e.get("stage") == "parse"]
        self.assertEqual(len(parse_completed), 1)
        self.assertIn("duration_s", parse_completed[0])
        self.assertIsNone(parse_completed[0].get("error_category"))

    def test_stage_timer_logs_failure_on_exception(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            logger = JobEventLogger(log_path)
            timer = ParseStageTimer(logger, job_id="j1", doc_id="d1")
            with self.assertRaises(ValueError):
                with timer.stage("parse"):
                    raise ValueError("boom")
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line) for line in lines]
        failed = [e for e in events if e["event"] == "stage_failed" and e.get("stage") == "parse"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error_category"], "parser_failed")

    def test_stage_timer_part_id_propagated(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            logger = JobEventLogger(log_path)
            timer = ParseStageTimer(logger, job_id="j1", doc_id="d1", part_id="p1", tenant_id="tenant-a")
            with timer.stage("embed"):
                pass
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line) for line in lines]
        for event in events:
            self.assertEqual(event.get("part_id"), "p1")
            self.assertEqual(event.get("tenant_id"), "tenant-a")

    def test_logger_redacts_sensitive_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            logger = JobEventLogger(log_path)
            logger.log(
                "started",
                job_id="j1",
                api_key="secret-key",
                headers={"Authorization": "Bearer secret-token", "x-request-id": "req-1"},
                nested=[{"refresh_token": "secret-refresh"}],
            )
            event = json.loads(log_path.read_text(encoding="utf-8").strip())

        self.assertEqual(event["api_key"], "[redacted]")
        self.assertEqual(event["headers"]["Authorization"], "[redacted]")
        self.assertEqual(event["headers"]["x-request-id"], "req-1")
        self.assertEqual(event["nested"][0]["refresh_token"], "[redacted]")

    def test_logger_stage_methods(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            logger = JobEventLogger(log_path)
            logger.log_stage_start("upload", job_id="j1", doc_id="d1")
            logger.log_stage_end("upload", job_id="j1", doc_id="d1", duration_s=1.5)
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line) for line in lines]
        self.assertEqual(events[0]["event"], "stage_started")
        self.assertEqual(events[1]["event"], "stage_completed")
        self.assertAlmostEqual(events[1]["duration_s"], 1.5)


if __name__ == "__main__":
    unittest.main()
