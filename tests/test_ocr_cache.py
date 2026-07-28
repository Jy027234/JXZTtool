from __future__ import annotations

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from parsecore.ocr_cache import PageOcrCache


class PageOcrCacheTests(unittest.TestCase):
    def test_round_trips_text_and_line_locators(self) -> None:
        with TemporaryDirectory(prefix="parsecore-ocr-cache-") as temp_dir:
            source = Path(temp_dir) / "source.pdf"
            source.write_bytes(b"%PDF-test")
            cache = PageOcrCache(Path(temp_dir) / "cache", ttl_seconds=60)
            lines = [{"text": "hello", "bbox": [1.0, 2.0, 3.0, 4.0]}]

            cache.put(
                file_path=str(source),
                page_number=1,
                provider_tag="rapidocr",
                text="hello",
                lines=lines,
            )

            self.assertEqual(cache.get(file_path=str(source), page_number=1, provider_tag="rapidocr"), "hello")
            self.assertEqual(
                cache.get_entry(file_path=str(source), page_number=1, provider_tag="rapidocr")["lines"],
                lines,
            )

    def test_legacy_text_only_entry_remains_readable(self) -> None:
        with TemporaryDirectory(prefix="parsecore-ocr-cache-") as temp_dir:
            source = Path(temp_dir) / "source.pdf"
            source.write_bytes(b"%PDF-test")
            cache = PageOcrCache(Path(temp_dir) / "cache", ttl_seconds=60)
            cache.put(file_path=str(source), page_number=1, provider_tag="rapidocr", text="legacy")
            entry_path = next((Path(temp_dir) / "cache").rglob("*.json"))
            entry_path.write_text(json.dumps({"ts": time.time(), "text": "legacy"}), encoding="utf-8")

            entry = cache.get_entry(file_path=str(source), page_number=1, provider_tag="rapidocr")

            self.assertEqual(entry["text"], "legacy")
            self.assertNotIn("lines", entry)


if __name__ == "__main__":
    unittest.main()
