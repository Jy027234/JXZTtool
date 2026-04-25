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


if __name__ == "__main__":
    unittest.main()
