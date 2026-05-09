from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from parsecore.config import load_settings


_TOML = """
[project]
name = "p"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 1
poll_interval_ms = 500
staged_upload_max_bytes = 104857600
max_active_parts_per_doc = 3
job_timeout_seconds = 120
part_timeout_seconds = 30
retry_backoff_seconds = 2.5
retry_backoff_max_seconds = 45
api_key_env = "PARSECORE_API_KEY"

[storage]
database_url = "sqlite:///./var/x.db"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"
batch_size = 8

[providers.ocr]
enabled = true
provider = "remote-http"
base_url = "https://example.invalid"
api_key_env = "PARSECORE_OCR_API_KEY"
timeout_seconds = 9.5
max_retries = 4
options = { endpoint_path = "/ocr/v1", det_use_dilation = true }

[[parsers]]
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]
options = { post_process = { strip_headers_footers = false, short_block_min_length = 20 } }

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
"""


class LoadSettingsParserOptionsTests(unittest.TestCase):
    def test_parser_options_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(_TOML, encoding="utf-8")
            settings = load_settings(path)

        parsers = {p.name: p for p in settings.parsers}
        pdf = parsers["pdf-text"]
        self.assertEqual(
            dict(pdf.options.get("post_process")),
            {"strip_headers_footers": False, "short_block_min_length": 20},
        )

        docx = parsers["docx-native"]
        self.assertEqual(dict(docx.options), {})
        self.assertTrue(settings.providers.embedding.enabled)
        self.assertEqual(settings.providers.embedding.batch_size, 8)
        self.assertEqual(settings.providers.embedding.model, "text-embedding-3-small")
        self.assertTrue(settings.providers.ocr.enabled)
        self.assertEqual(settings.providers.ocr.provider, "remote-http")
        self.assertEqual(settings.providers.ocr.base_url, "https://example.invalid")
        self.assertEqual(settings.providers.ocr.api_key_env, "PARSECORE_OCR_API_KEY")
        self.assertEqual(settings.providers.ocr.timeout_seconds, 9.5)
        self.assertEqual(settings.providers.ocr.max_retries, 4)
        self.assertEqual(settings.runtime.api_key_env, "PARSECORE_API_KEY")
        self.assertFalse(settings.runtime.allow_external_file_paths)
        self.assertEqual(settings.runtime.staged_upload_max_bytes, 104857600)
        self.assertEqual(settings.runtime.max_active_parts_per_doc, 3)
        self.assertEqual(settings.runtime.job_timeout_seconds, 120)
        self.assertEqual(settings.runtime.part_timeout_seconds, 30)
        self.assertEqual(settings.runtime.retry_backoff_seconds, 2.5)
        self.assertEqual(settings.runtime.retry_backoff_max_seconds, 45)
        self.assertEqual(
            dict(settings.providers.ocr.options),
            {"endpoint_path": "/ocr/v1", "det_use_dilation": True},
        )

    def test_legacy_jobcard_adapter_is_normalized_to_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(_TOML.replace('adapter = "embedded"', 'adapter = "jobcard"'), encoding="utf-8")
            settings = load_settings(path)

        self.assertEqual(settings.product_adapter, "embedded")


if __name__ == "__main__":
    unittest.main()
