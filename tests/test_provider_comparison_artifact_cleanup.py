from __future__ import annotations

import io
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parsecore.cli import main as cli_main
from parsecore.parts import cleanup_provider_comparison_artifacts


def _make_old(path: Path, *, age_seconds: int = 2 * 86400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old", encoding="utf-8")
    modified_at = time.time() - age_seconds
    os.utime(path, (modified_at, modified_at))


class ProviderComparisonArtifactCleanupTests(unittest.TestCase):
    def test_dry_run_selects_only_self_check_provider_comparison_reports(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            json_report = root / "provider-comparison.fast.json"
            markdown_report = root / "nested" / "provider-comparison.full.md"
            self_check = root / "self-check.json"
            unrelated_audit = root / "optimization-audit.json"
            recent_report = root / "provider-comparison.perf.json"
            _make_old(json_report)
            _make_old(markdown_report)
            _make_old(self_check)
            _make_old(unrelated_audit)
            recent_report.write_text("recent", encoding="utf-8")

            report = cleanup_provider_comparison_artifacts(
                root,
                retention_seconds=86400,
            )

            self.assertEqual(report["status"], "dry_run")
            self.assertEqual(report["kind"], "comparison_report")
            self.assertEqual(report["candidates"], 2)
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["artifact_selector"], "provider-comparison.<profile>.{json,md}")
            self.assertEqual(
                sorted(Path(item["path"]).name for item in report["files"]),
                ["provider-comparison.fast.json", "provider-comparison.full.md"],
            )
            self.assertTrue(json_report.exists())
            self.assertTrue(markdown_report.exists())
            self.assertTrue(self_check.exists())
            self.assertTrue(unrelated_audit.exists())
            self.assertTrue(recent_report.exists())

    def test_execute_removes_only_expired_provider_comparison_reports(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            json_report = root / "provider-comparison.fast.json"
            markdown_report = root / "provider-comparison.fast.md"
            self_check = root / "self-check.json"
            _make_old(json_report)
            _make_old(markdown_report)
            _make_old(self_check)

            report = cleanup_provider_comparison_artifacts(
                root,
                retention_seconds=86400,
                dry_run=False,
            )

            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["removed"], 2)
            self.assertFalse(json_report.exists())
            self.assertFalse(markdown_report.exists())
            self.assertTrue(self_check.exists())

    def test_zero_retention_disables_cleanup_and_negative_value_is_rejected(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report = cleanup_provider_comparison_artifacts(root, retention_seconds=0)

        self.assertEqual(report["status"], "disabled")
        self.assertEqual(report["candidates"], 0)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            cleanup_provider_comparison_artifacts(".", retention_seconds=-1)

    def test_cli_defaults_to_dry_run_and_requires_execute_to_remove(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = root / "parsecore.toml"
            config.write_text(
                "[runtime]\nprovider_comparison_artifact_retention_seconds = 1\n",
                encoding="utf-8",
            )
            report_path = root / "provider-comparison.fast.json"
            _make_old(report_path, age_seconds=10)
            audit_path = root / "self-check.json"
            _make_old(audit_path, age_seconds=10)
            dry_run_out = root / "dry-run.json"

            dry_run_stdout = io.StringIO()
            with patch("sys.stdout", dry_run_stdout):
                exit_code = cli_main(
                    [
                        "cleanup-provider-comparison-artifacts",
                        "--config",
                        str(config),
                        "--root",
                        str(root),
                        "--out",
                        str(dry_run_out),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(dry_run_stdout.getvalue())["status"], "dry_run")
            self.assertEqual(json.loads(dry_run_out.read_text(encoding="utf-8"))["candidates"], 1)
            self.assertTrue(report_path.exists())

            execute_stdout = io.StringIO()
            with patch("sys.stdout", execute_stdout):
                exit_code = cli_main(
                    [
                        "cleanup-provider-comparison-artifacts",
                        "--config",
                        str(config),
                        "--root",
                        str(root),
                        "--execute",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(execute_stdout.getvalue())["status"], "completed")
            self.assertFalse(report_path.exists())
            self.assertTrue(audit_path.exists())


if __name__ == "__main__":
    unittest.main()
