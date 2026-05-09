from __future__ import annotations

from pathlib import Path
import unittest

from parsecore import pdf_parts
from parsecore.pdf_parts import (
    child_doc_id,
    create_pdf_part_file,
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
