from __future__ import annotations

from pathlib import Path
import unittest

from parsecore import pdf_parts
from parsecore.pdf_parts import (
    child_doc_id,
    create_pdf_part_file,
    create_pdf_part_files,
    detect_pdf_page_count,
    plan_pdf_parts,
)


class PdfPartsPlanTests(unittest.TestCase):
    def test_plans_page_ranges_with_last_short_part(self) -> None:
        parts = plan_pdf_parts("doc-123", 12, target_pages_per_part=5)

        self.assertEqual(
            [(part["page_start"], part["page_end"], part["page_count"]) for part in parts],
            [(1, 5, 5), (6, 10, 5), (11, 12, 2)],
        )
        self.assertEqual([part["part_index"] for part in parts], [1, 2, 3])
        self.assertEqual({part["state"] for part in parts}, {"pending"})
        self.assertEqual({part["source_doc_id"] for part in parts}, {"doc-123"})
        self.assertEqual({part["doc_id"] for part in parts}, {"doc-123"})

    def test_uses_ocr_heavy_part_size_from_profile(self) -> None:
        parts = plan_pdf_parts(
            "scan",
            13,
            target_pages_per_part=10,
            ocr_heavy_pages_per_part=4,
            profile="ocr-heavy",
        )

        self.assertEqual(
            [(part["page_start"], part["page_end"]) for part in parts],
            [(1, 4), (5, 8), (9, 12), (13, 13)],
        )

    def test_uses_ocr_heavy_part_size_from_options(self) -> None:
        parts = plan_pdf_parts(
            "scan",
            9,
            target_pages_per_part=8,
            ocr_heavy_pages_per_part=3,
            options={"force_ocr": True},
        )

        self.assertEqual([part["page_count"] for part in parts], [3, 3, 3])

    def test_rejects_invalid_total_pages_and_part_sizes(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_total_pages"):
            plan_pdf_parts("doc", 0)
        with self.assertRaisesRegex(ValueError, "invalid_pages_per_part"):
            plan_pdf_parts("doc", 1, target_pages_per_part=0)

    def test_child_doc_id_is_stable_and_safe(self) -> None:
        first = child_doc_id("客户 文档/Alpha#1", 2)
        second = child_doc_id("客户 文档/Alpha#1", 2)

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9._-]+-part-2$")
        self.assertNotIn("/", first)
        self.assertNotIn(" ", first)
        self.assertNotEqual(first, child_doc_id("客户 文档/Alpha#1", 3))

    def test_plan_returns_suggestions_without_creating_jobs(self) -> None:
        """P5-T02: plan_pdf_parts is a pure function — returns suggestions only, no side effects."""
        parts = plan_pdf_parts("doc-dry-run", 20, target_pages_per_part=5)

        # Must return exactly 4 parts for 20 pages with 5 pages per part
        self.assertEqual(len(parts), 4)

        # Every part must be in "pending" state (not created/running/done)
        for part in parts:
            self.assertEqual(part["state"], "pending")
            self.assertIsNotNone(part["part_id"])
            self.assertIsNotNone(part["page_start"])
            self.assertIsNotNone(part["page_end"])
            self.assertEqual(part["source_doc_id"], "doc-dry-run")

        # Verify page ranges are contiguous and cover all pages
        self.assertEqual(parts[0]["page_start"], 1)
        self.assertEqual(parts[-1]["page_end"], 20)
        for i in range(1, len(parts)):
            self.assertEqual(parts[i]["page_start"], parts[i - 1]["page_end"] + 1)

    def test_plan_is_pure_no_state_mutation(self) -> None:
        """P5-T02: calling plan_pdf_parts twice yields identical results (idempotent)."""
        first = plan_pdf_parts("doc-idempotent", 15, target_pages_per_part=5)
        second = plan_pdf_parts("doc-idempotent", 15, target_pages_per_part=5)

        self.assertEqual(first, second)

    def test_plan_does_not_require_store_or_runtime(self) -> None:
        """P5-T02: plan_pdf_parts works standalone without any store/runtime dependency."""
        # Direct call without any runtime or store — proves dry-run nature
        parts = plan_pdf_parts("doc-standalone", 8, target_pages_per_part=3)

        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0]["page_start"], 1)
        self.assertEqual(parts[0]["page_end"], 3)
        self.assertEqual(parts[1]["page_start"], 4)
        self.assertEqual(parts[1]["page_end"], 6)
        self.assertEqual(parts[2]["page_start"], 7)
        self.assertEqual(parts[2]["page_end"], 8)
        self.assertEqual(parts[2]["page_count"], 2)  # last part is short

    # ── P5-T01: 完善 part 策略决策因子 ──

    def test_plan_shrinks_part_size_for_large_file(self) -> None:
        """P5-T01: file_size_bytes > 100MB 时 part 大小减半。"""
        parts = plan_pdf_parts(
            "doc-large-file",
            100,
            target_pages_per_part=50,
            file_size_bytes=200 * 1024 * 1024,  # 200MB
        )
        # 默认 50 页/part, 大文件减半 = 25 页/part, 100 页 → 4 个 part
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0]["page_count"], 25)
        self.assertEqual(parts[-1]["page_count"], 25)

    def test_plan_switches_to_ocr_heavy_when_ratio_high(self) -> None:
        """P5-T01: ocr_page_ratio > 0.3 时切换到 ocr_heavy 目标。"""
        parts = plan_pdf_parts(
            "doc-ocr-dense",
            20,
            target_pages_per_part=50,
            ocr_heavy_pages_per_part=6,
            ocr_page_ratio=0.5,
        )
        # ocr_page_ratio=0.5 > 0.3 → 使用 ocr_heavy 目标 = 6 页/part
        self.assertEqual(parts[0]["page_count"], 6)
        self.assertGreaterEqual(len(parts), 3)

    def test_plan_shrinks_part_size_when_failure_rate_high(self) -> None:
        """P5-T01: historical_failure_rate > 0.2 时 part 大小减半。"""
        parts = plan_pdf_parts(
            "doc-flaky",
            100,
            target_pages_per_part=50,
            historical_failure_rate=0.3,
        )
        # 默认 50 页/part, 高失败率减半 = 25 页/part, 100 页 → 4 个 part
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0]["page_count"], 25)

    def test_plan_ignores_new_factors_when_none(self) -> None:
        """P5-T01: 新参数全为 None 时行为与之前一致（向后兼容）。"""
        # 无新参数
        old_parts = plan_pdf_parts("doc-compat", 50, target_pages_per_part=10)
        # 新参数全部 None
        new_parts = plan_pdf_parts(
            "doc-compat",
            50,
            target_pages_per_part=10,
            file_size_bytes=None,
            ocr_page_ratio=None,
            historical_failure_rate=None,
        )
        self.assertEqual(len(old_parts), len(new_parts))
        for old, new in zip(old_parts, new_parts):
            self.assertEqual(old["page_start"], new["page_start"])
            self.assertEqual(old["page_end"], new["page_end"])
            self.assertEqual(old["page_count"], new["page_count"])

    def test_plan_file_below_100mb_does_not_shrink(self) -> None:
        """P5-T01: file_size_bytes <= 100MB 不触发缩减。"""
        parts = plan_pdf_parts(
            "doc-small",
            50,
            target_pages_per_part=20,
            file_size_bytes=50 * 1024 * 1024,  # 50MB
        )
        # 50 页, 20 页/part → 3 parts, 这里不触发大文件缩减
        self.assertEqual(parts[0]["page_count"], 20)

    def test_plan_failure_rate_at_boundary_does_not_shrink(self) -> None:
        """P5-T01: historical_failure_rate == 0.2 不触发缩减。"""
        parts = plan_pdf_parts(
            "doc-boundary",
            50,
            target_pages_per_part=20,
            historical_failure_rate=0.2,
        )
        # failure_rate = 0.2, 不 > 0.2 → 不缩减
        self.assertEqual(parts[0]["page_count"], 20)

    def test_plan_ocr_ratio_at_boundary_does_not_switch(self) -> None:
        """P5-T01: ocr_page_ratio == 0.3 不触发 ocr_heavy 切换。"""
        parts = plan_pdf_parts(
            "doc-ocr-boundary",
            20,
            target_pages_per_part=20,
            ocr_heavy_pages_per_part=6,
            ocr_page_ratio=0.3,
        )
        # ocr_page_ratio = 0.3, 不 > 0.3 → 不切换到 ocr_heavy
        self.assertEqual(parts[0]["page_count"], 20)


class PdfPartsIoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_reader = pdf_parts.PdfReader
        self.original_writer = pdf_parts.PdfWriter

    def tearDown(self) -> None:
        pdf_parts.PdfReader = self.original_reader
        pdf_parts.PdfWriter = self.original_writer

    def test_detect_pdf_page_count_with_fake_reader(self) -> None:
        class FakeReader:
            def __init__(self, file_path: str) -> None:
                self.file_path = file_path
                self.pages = ["p1", "p2", "p3"]

        pdf_parts.PdfReader = FakeReader

        self.assertEqual(detect_pdf_page_count("fake.pdf"), 3)

    def test_detect_pdf_page_count_wraps_reader_errors(self) -> None:
        class BrokenReader:
            def __init__(self, file_path: str) -> None:
                raise RuntimeError("not a pdf")

        pdf_parts.PdfReader = BrokenReader

        with self.assertRaisesRegex(ValueError, "invalid_pdf"):
            detect_pdf_page_count("bad.pdf")

    def test_create_pdf_part_file_writes_selected_pages_with_fake_classes(self) -> None:
        writes: list[list[str]] = []

        class FakeReader:
            def __init__(self, source_file: object) -> None:
                self.pages = ["p1", "p2", "p3", "p4"]

        class FakeWriter:
            def __init__(self) -> None:
                self.pages: list[str] = []

            def add_page(self, page: str) -> None:
                self.pages.append(page)

            def write(self, target_file: object) -> None:
                writes.append(list(self.pages))
                target_file.write(b"fake-pdf")

        pdf_parts.PdfReader = FakeReader
        pdf_parts.PdfWriter = FakeWriter
        tmp_dir = Path(self._testMethodName)
        tmp_dir.mkdir(exist_ok=True)
        source = tmp_dir / "source.pdf"
        target = tmp_dir / "part.pdf"
        source.write_bytes(b"%PDF-fake")
        try:
            create_pdf_part_file(str(source), str(target), 2, 3)
            self.assertEqual(writes, [["p2", "p3"]])
            self.assertEqual(target.read_bytes(), b"fake-pdf")
        finally:
            if target.exists():
                target.unlink()
            source.unlink()
            tmp_dir.rmdir()

    def test_create_pdf_part_files_opens_source_once_for_batch(self) -> None:
        reader_sources: list[object] = []
        writes: list[list[str]] = []

        class FakeReader:
            def __init__(self, source_file: object) -> None:
                reader_sources.append(source_file)
                self.pages = ["p1", "p2", "p3", "p4", "p5"]

        class FakeWriter:
            def __init__(self) -> None:
                self.pages: list[str] = []

            def add_page(self, page: str) -> None:
                self.pages.append(page)

            def write(self, target_file: object) -> None:
                writes.append(list(self.pages))
                target_file.write(",".join(self.pages).encode("utf-8"))

        pdf_parts.PdfReader = FakeReader
        pdf_parts.PdfWriter = FakeWriter
        tmp_dir = Path(self._testMethodName)
        tmp_dir.mkdir(exist_ok=True)
        source = tmp_dir / "source.pdf"
        first = tmp_dir / "part-1.pdf"
        second = tmp_dir / "part-2.pdf"
        source.write_bytes(b"%PDF-fake")
        try:
            create_pdf_part_files(
                str(source),
                [
                    {"target_path": str(first), "page_start": 1, "page_end": 2},
                    {"target_path": str(second), "page_start": 4, "page_end": 5},
                ],
            )

            self.assertEqual(len(reader_sources), 1)
            self.assertEqual(writes, [["p1", "p2"], ["p4", "p5"]])
            self.assertEqual(first.read_bytes(), b"p1,p2")
            self.assertEqual(second.read_bytes(), b"p4,p5")
        finally:
            for path in (first, second, source):
                if path.exists():
                    path.unlink()
            tmp_dir.rmdir()

    def test_create_pdf_part_file_rejects_invalid_page_ranges(self) -> None:
        class FakeReader:
            def __init__(self, source_file: object) -> None:
                self.pages = ["p1", "p2"]

        class FakeWriter:
            def add_page(self, page: str) -> None:
                pass

            def write(self, target_file: object) -> None:
                pass

        pdf_parts.PdfReader = FakeReader
        pdf_parts.PdfWriter = FakeWriter
        tmp_dir = Path(self._testMethodName)
        tmp_dir.mkdir(exist_ok=True)
        source = tmp_dir / "source.pdf"
        source.write_bytes(b"%PDF-fake")
        try:
            with self.assertRaisesRegex(ValueError, "invalid_page_range"):
                create_pdf_part_file(str(source), "unused.pdf", 0, 1)
            with self.assertRaisesRegex(ValueError, "invalid_page_range"):
                create_pdf_part_file(str(source), "unused.pdf", 2, 1)
            with self.assertRaisesRegex(ValueError, "invalid_page_range"):
                create_pdf_part_file(str(source), "unused.pdf", 1, 3)
        finally:
            source.unlink()
            tmp_dir.rmdir()


if __name__ == "__main__":
    unittest.main()
