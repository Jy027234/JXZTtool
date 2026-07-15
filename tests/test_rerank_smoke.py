from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _rerank_smoke


ROOT = Path(__file__).resolve().parent.parent
ALIYUN_PROFILE = ROOT / "parsecore.pgvector.aliyun-rag.toml.example"


class RerankSmokeTests(unittest.TestCase):
    def test_require_live_reports_missing_credential_without_leaking_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "rerank-smoke.json"
            with patch.dict(
                os.environ,
                {"PARSECORE_ALIYUN_API_KEY": ""},
                clear=False,
            ):
                exit_code = _rerank_smoke.main(
                    [
                        "--config",
                        str(ALIYUN_PROFILE),
                        "--require-live",
                        "--out-json",
                        str(output),
                    ]
                )

            rendered = output.read_text(encoding="utf-8")
            payload = json.loads(rendered)

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(payload["reason"], "missing env var PARSECORE_ALIYUN_API_KEY")
        self.assertNotIn("PARSECORE_ALIYUN_API_KEY=", rendered)

    def test_fake_provider_can_smoke_without_a_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "fake-rerank.toml"
            config.write_text(
                ALIYUN_PROFILE.read_text(encoding="utf-8").replace(
                    'provider = "dashscope-compatible"',
                    'provider = "fake"',
                ),
                encoding="utf-8",
            )
            output = root / "rerank-smoke.json"
            with patch.dict(
                os.environ,
                {"PARSECORE_ALIYUN_API_KEY": ""},
                clear=False,
            ):
                exit_code = _rerank_smoke.main(
                    ["--config", str(config), "--out-json", str(output)]
                )

            rendered = output.read_text(encoding="utf-8")
            payload = json.loads(rendered)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["result_indexes"], [0, 1, 2])
        self.assertNotIn("hydraulic pressure", rendered)


if __name__ == "__main__":
    unittest.main()
