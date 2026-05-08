from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from parsecore.models import Block, BlockType, ParseJob, ParseJobState
from tools.ocr_benchmark import _run_one, _summary


class OcrBenchmarkTests(unittest.TestCase):
    def test_summary_uses_ocr_decision_trace_totals(self) -> None:
        payload = _summary(
            [
                {
                    "status": "done",
                    "elapsed_s": 1.2,
                    "layout_signals": {
                        "ocr_attempted_pages": 99,
                        "ocr_fallback_pages": 88,
                        "ocr_failed_pages": 77,
                    },
                    "ocr_decision_trace": {
                        "ocr_attempted_pages": 2,
                        "ocr_fallback_pages": 1,
                        "ocr_rejected_pages": 1,
                        "ocr_failed_pages": 1,
                        "native_text_token_count": 15,
                        "final_text_token_count": 41,
                    },
                }
            ]
        )

        self.assertEqual(payload["documents"], 1)
        self.assertEqual(payload["failed_documents"], 0)
        self.assertEqual(payload["ocr_attempted_pages"], 2)
        self.assertEqual(payload["ocr_fallback_pages"], 1)
        self.assertEqual(payload["ocr_rejected_pages"], 1)
        self.assertEqual(payload["ocr_failed_pages"], 1)
        self.assertEqual(payload["native_text_token_count"], 15)
        self.assertEqual(payload["final_text_token_count"], 41)

    def test_run_one_includes_ocr_decision_trace_payload(self) -> None:
        blocks = (
            Block(
                block_id="blk-1",
                doc_id="doc-bench",
                type=BlockType.PARAGRAPH,
                content="native text",
                metadata={
                    "page": 1,
                    "ocr_attempted": True,
                    "ocr_attempt_reason": "cid_dense",
                    "ocr_fallback_used": True,
                    "ocr_acceptance_reason": "fallback_applied",
                    "native_text_token_count": 3,
                    "final_text_token_count": 9,
                },
            ),
        )

        outcome = SimpleNamespace(
            job=ParseJob(
                job_id="job-bench-1",
                doc_id="doc-bench",
                file_path="sample.pdf",
                media_type="application/pdf",
                state=ParseJobState.DONE,
                created_at="2026-05-01T00:00:00+00:00",
                updated_at="2026-05-01T00:00:01+00:00",
                tenant_id="benchmark",
                quota_key="ocr",
                quota_units=1,
            ),
            blocks=blocks,
            chunks=(),
        )

        class _FakeRuntime:
            def submit(self, _request):
                return outcome

        result = _run_one(
            runtime=_FakeRuntime(),
            pdf_path=Path("sample.pdf"),
            index=1,
            top_pages=3,
            enable_ocr=True,
        )

        self.assertEqual(result["status"], ParseJobState.DONE.value)
        self.assertIn("ocr_decision_trace", result)
        self.assertEqual(result["ocr_decision_trace"]["ocr_attempted_pages"], 1)
        self.assertEqual(result["ocr_decision_trace"]["ocr_fallback_pages"], 1)
        self.assertEqual(result["ocr_decision_trace"]["native_text_token_count"], 3)
        self.assertEqual(result["ocr_decision_trace"]["final_text_token_count"], 9)


if __name__ == "__main__":
    unittest.main()
