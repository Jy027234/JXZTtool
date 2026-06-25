from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import payload_contract_check


class PayloadContractCheckTests(unittest.TestCase):
    def test_run_check_validates_all_registered_contracts(self) -> None:
        payload = payload_contract_check.run_check()

        self.assertEqual(payload["schema_version"], "2026-06-payload-contract-check")
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["summary"]["schema_count"], 6)
        self.assertEqual(payload["summary"]["payload_count"], 6)
        self.assertEqual(payload["summary"]["failed_schema_count"], 0)
        self.assertEqual(payload["summary"]["failed_payload_count"], 0)
        self.assertEqual(payload["failures"], [])

    def test_main_writes_json_report_when_out_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "payload-contract-check.json"
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                exit_code = payload_contract_check.main(["--out", str(out_path)])

            self.assertEqual(exit_code, 0)
            written = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], "passed")
            self.assertIn('"schema_version": "2026-06-payload-contract-check"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
