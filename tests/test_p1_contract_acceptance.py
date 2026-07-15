from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import p1_contract_acceptance


class P1ContractAcceptanceTests(unittest.TestCase):
    def test_run_acceptance_covers_all_p1_variants_and_bridges(self) -> None:
        report = p1_contract_acceptance.run_acceptance()

        self.assertEqual(report["schema_version"], "2026-07-p1-contract-acceptance")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["schema_count"], 6)
        self.assertEqual(report["summary"]["sample_variant_count"], 4)
        self.assertEqual(report["summary"]["payload_count"], 24)
        self.assertEqual(report["summary"]["failed_check_count"], 0)
        self.assertEqual(report["failures"], [])
        self.assertEqual(
            report["projections"]["workflow_phases"],
            ["inspect", "compare", "execute", "verify"],
        )

    def test_main_writes_json_and_markdown_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "p1.json"
            markdown_path = Path(tmp_dir) / "p1.md"
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                exit_code = p1_contract_acceptance.main(
                    ["--out", str(json_path), "--markdown-out", str(markdown_path)]
                )

            self.assertEqual(exit_code, 0)
            written = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], "passed")
            self.assertIn("P1 契约冻结", markdown_path.read_text(encoding="utf-8"))
            self.assertIn('"schema_version": "2026-07-p1-contract-acceptance"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
