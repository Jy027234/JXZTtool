from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import corpus_manifest


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class CorpusManifestWorkspace:
    """Small temp workspace: suite + baselines + fixtures, never var/regression."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.fixture_root = root / "fixtures"
        self.fixture_root.mkdir(parents=True, exist_ok=True)

    def fixture(self, name: str, payload: bytes = b"fixture-bytes") -> Path:
        path = self.fixture_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def baseline(self, name: str, fixtures: list[object]) -> Path:
        return _write_json(self.root / name, {"fixtures": fixtures})

    def suite(self, entries: list[object], name: str = "suite.json") -> Path:
        return _write_json(self.root / name, {"entries": entries})

    @staticmethod
    def digest(payload: bytes) -> dict[str, object]:
        return {
            "fixture_sha256": hashlib.sha256(payload).hexdigest(),
            "fixture_size_bytes": len(payload),
        }


class CorpusManifestValidatorTests(unittest.TestCase):
    def test_disabled_entry_with_directory_baseline_is_not_read_or_counted_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            suite = workspace.suite(
                [
                    {
                        "name": "multi-column-pdf",
                        "disabled": True,
                        # Empty baseline resolves to the (directory) suite dir;
                        # the old inline detector tried to read it as JSON.
                        "baseline": "",
                        "fixture_relative_path": "fixtures/multi-column.pdf",
                    }
                ]
            )

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_DISABLED)
        self.assertEqual(entry.reason, corpus_manifest.REASON_DISABLED_ENTRY)
        self.assertIsNone(entry.baseline_path)
        self.assertEqual(entry.fixture_count, 0)
        self.assertEqual(report.totals["disabled_entries"], 1)
        self.assertEqual(report.totals["missing_entries"], 0)
        self.assertEqual(report.totals["unavailable_entries"], 0)
        self.assertFalse(report.available)
        self.assertFalse(report.verified)

    def test_empty_baseline_path_is_reported_without_being_read(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            suite = workspace.suite([{"name": "blank-baseline", "baseline": "   "}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_UNAVAILABLE)
        self.assertEqual(entry.reason, corpus_manifest.REASON_EMPTY_BASELINE_PATH)
        self.assertFalse(report.available)
        self.assertEqual(report.reason, corpus_manifest.REASON_EMPTY_BASELINE_PATH)

    def test_directory_baseline_is_reported_as_not_a_regular_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            suite = workspace.suite([{"name": "dir-baseline", "baseline": "."}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_UNAVAILABLE)
        self.assertEqual(entry.reason, corpus_manifest.REASON_BASELINE_NOT_FILE)
        self.assertIn("directory", entry.message or "")

    def test_missing_baseline_is_reported_not_silently_skipped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            suite = workspace.suite([{"name": "missing-baseline", "baseline": "nope.json"}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_UNAVAILABLE)
        self.assertEqual(entry.reason, corpus_manifest.REASON_BASELINE_MISSING)
        self.assertFalse(report.available)
        self.assertFalse(report.verified)
        self.assertEqual(report.reasons[corpus_manifest.REASON_BASELINE_MISSING], 1)

    def test_missing_fixture_is_counted_and_reported(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            baseline = workspace.baseline(
                "b.json",
                [{"fixture": str(workspace.fixture_root / "gone.pdf")}],
            )
            suite = workspace.suite([{"name": "missing-fixture", "baseline": baseline.name}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_MISSING)
        self.assertEqual(entry.reason, corpus_manifest.REASON_FIXTURE_MISSING)
        self.assertEqual(entry.missing_fixture_count, 1)
        self.assertEqual(report.totals["missing_fixtures"], 1)
        self.assertFalse(report.available)

    def test_empty_fixture_set_is_unavailable_and_unverified(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            baseline = workspace.baseline("b.json", [])
            suite = workspace.suite([{"name": "empty-set", "baseline": baseline.name}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_EMPTY)
        self.assertEqual(entry.reason, corpus_manifest.REASON_EMPTY_FIXTURE_SET)
        self.assertEqual(entry.fixture_count, 0)
        self.assertFalse(report.available)
        self.assertFalse(report.verified)
        self.assertEqual(report.totals["empty_entries"], 1)

    def test_hash_drift_is_available_but_not_verified(self) -> None:
        payload = b"drifted-fixture"
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            fixture = workspace.fixture("sample.pdf", payload)
            baseline = workspace.baseline(
                "b.json",
                [
                    {
                        "fixture": str(fixture),
                        "fixture_sha256": hashlib.sha256(b"original").hexdigest(),
                        "fixture_size_bytes": len(b"original"),
                    }
                ],
            )
            suite = workspace.suite([{"name": "drifted", "baseline": baseline.name}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_HASH_DRIFT)
        self.assertEqual(entry.reason, corpus_manifest.REASON_FIXTURE_HASH_DRIFT)
        self.assertEqual(entry.hash_drift_count, 1)
        self.assertTrue(report.available)
        self.assertFalse(report.verified)
        self.assertEqual(report.reason, corpus_manifest.REASON_FIXTURE_HASH_DRIFT)
        fixture_report = entry.fixtures[0]
        self.assertEqual(fixture_report.sha256, hashlib.sha256(payload).hexdigest())
        self.assertFalse(fixture_report.hash_verified)

    def test_hash_and_size_match_verifies_entry(self) -> None:
        payload = b"stable-fixture"
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            fixture = workspace.fixture("sample.pdf", payload)
            baseline = workspace.baseline(
                "b.json",
                [{"fixture": str(fixture), **workspace.digest(payload)}],
            )
            suite = workspace.suite([{"name": "stable", "baseline": baseline.name}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        entry = report.entries[0]
        self.assertEqual(entry.status, corpus_manifest.STATUS_VERIFIED)
        self.assertIsNone(entry.reason)
        self.assertTrue(entry.fixtures[0].hash_recorded)
        self.assertEqual(entry.hash_unrecorded_count, 0)
        self.assertEqual(report.totals["hash_unrecorded_fixtures"], 0)
        self.assertTrue(report.available)
        self.assertTrue(report.verified)

    def test_unrecorded_hash_is_reported_but_does_not_block_verification(self) -> None:
        payload = b"legacy-fixture"
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            fixture = workspace.fixture("sample.pdf", payload)
            baseline = workspace.baseline("b.json", [{"fixture": str(fixture)}])
            suite = workspace.suite([{"name": "legacy", "baseline": baseline.name}])

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        self.assertTrue(report.verified)
        self.assertEqual(report.totals["hash_unrecorded_fixtures"], 1)
        self.assertFalse(report.entries[0].fixtures[0].hash_recorded)

    def test_fixture_relative_path_uses_fixture_root_env(self) -> None:
        payload = b"portable-fixture"
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            workspace.fixture("nested/sample.pdf", payload)
            baseline = workspace.baseline(
                "b.json",
                [
                    {
                        "fixture": str(Path(temp_dir) / "elsewhere" / "sample.pdf"),
                        "fixture_relative_path": "nested/sample.pdf",
                        **workspace.digest(payload),
                    }
                ],
            )
            suite = workspace.suite([{"name": "portable", "baseline": baseline.name}])
            with patch.dict(
                os.environ,
                {corpus_manifest.FIXTURE_ROOT_ENV: str(workspace.fixture_root)},
                clear=False,
            ):
                report = corpus_manifest.validate_suite(suite)

        self.assertTrue(report.verified)
        self.assertEqual(
            report.entries[0].fixtures[0].resolved,
            str(workspace.fixture_root / "nested" / "sample.pdf"),
        )

    def test_fixture_path_expands_environment_variables_and_user(self) -> None:
        payload = b"expanded-fixture"
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            workspace.fixture("expanded.pdf", payload)
            baseline = workspace.baseline(
                "b.json",
                [
                    {
                        "fixture": "${PARSECORE_TEST_CORPUS_DIR}/expanded.pdf",
                        **workspace.digest(payload),
                    }
                ],
            )
            suite = workspace.suite([{"name": "expanded", "baseline": baseline.name}])
            with patch.dict(
                os.environ,
                {"PARSECORE_TEST_CORPUS_DIR": str(workspace.fixture_root)},
                clear=False,
            ):
                report = corpus_manifest.validate_suite(suite)

        self.assertTrue(report.verified)

    def test_tilde_path_is_expanded_without_raising(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            baseline = workspace.baseline("b.json", [{"fixture": "~/definitely-missing-sample.pdf"}])
            suite = workspace.suite([{"name": "tilde", "baseline": baseline.name}])

            report = corpus_manifest.validate_suite(suite)

        resolved = report.entries[0].fixtures[0].resolved or ""
        self.assertNotIn("~", resolved)
        self.assertEqual(report.entries[0].reason, corpus_manifest.REASON_FIXTURE_MISSING)

    def test_malformed_entries_are_reported_without_raising(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            baseline = workspace.baseline("b.json", ["not-an-object"])
            suite = workspace.suite(
                [
                    "not-an-object",
                    {"name": "numeric-baseline", "baseline": 5},
                    {"name": "bad-fixture-type", "baseline": baseline.name},
                    {"name": "nul-path", "baseline": "b\0ad.json"},
                ]
            )

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        self.assertEqual(report.entries[0].reason, corpus_manifest.REASON_ENTRY_NOT_OBJECT)
        self.assertEqual(report.entries[1].reason, corpus_manifest.REASON_INVALID_BASELINE_PATH)
        self.assertEqual(report.entries[2].reason, corpus_manifest.REASON_INVALID_PATH)
        # A path that cannot be probed is still reported as a finding, not raised.
        self.assertIn(
            report.entries[3].reason,
            {corpus_manifest.REASON_BASELINE_MISSING, corpus_manifest.REASON_INVALID_PATH},
        )
        self.assertFalse(report.available)
        self.assertFalse(report.verified)

    def test_slow_tagged_entry_is_skipped_unless_included(self) -> None:
        payload = b"slow-fixture"
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            fixture = workspace.fixture("slow.pdf", payload)
            baseline = workspace.baseline(
                "b.json",
                [{"fixture": str(fixture), **workspace.digest(payload)}],
            )
            suite = workspace.suite(
                [{"name": "slow-sample", "baseline": baseline.name, "tags": ["slow"]}]
            )

            skipped = corpus_manifest.validate_suite(suite)
            included = corpus_manifest.validate_suite(suite, include_tags=("slow",))

        self.assertEqual(skipped.entries[0].status, corpus_manifest.STATUS_SKIPPED)
        self.assertEqual(skipped.entries[0].reason, corpus_manifest.REASON_EXCLUDED_BY_TAG)
        self.assertEqual(skipped.totals["skipped_entries"], 1)
        self.assertFalse(skipped.available)
        self.assertTrue(included.verified)

    def test_missing_and_invalid_suites_are_reported(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = corpus_manifest.validate_suite(root / "nope.json")
            invalid = _write_json(root / "invalid.json", {"entries": "nope"})
            invalid_report = corpus_manifest.validate_suite(invalid)
            empty = corpus_manifest.validate_suite(_write_json(root / "empty.json", {"entries": []}))
            broken = root / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            broken_report = corpus_manifest.validate_suite(broken)

        self.assertEqual(missing.reason, corpus_manifest.REASON_SUITE_MISSING)
        self.assertFalse(missing.available)
        self.assertEqual(invalid_report.reason, corpus_manifest.REASON_SUITE_INVALID_SHAPE)
        self.assertEqual(empty.reason, corpus_manifest.REASON_SUITE_EMPTY)
        self.assertEqual(broken_report.reason, corpus_manifest.REASON_SUITE_INVALID_JSON)
        for report in (missing, invalid_report, empty, broken_report):
            self.assertFalse(report.verified)
            self.assertEqual(report.entries, [])

    def test_totals_split_verified_available_and_unavailable_entries(self) -> None:
        payload = b"mixed-fixture"
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            fixture = workspace.fixture("ok.pdf", payload)
            ok_baseline = workspace.baseline(
                "ok.json",
                [{"fixture": str(fixture), **workspace.digest(payload)}],
            )
            drift_baseline = workspace.baseline(
                "drift.json",
                [{"fixture": str(fixture), "fixture_sha256": "0" * 64}],
            )
            empty_baseline = workspace.baseline("empty.json", [])
            suite = workspace.suite(
                [
                    {"name": "ok", "baseline": ok_baseline.name},
                    {"name": "drift", "baseline": drift_baseline.name},
                    {"name": "empty", "baseline": empty_baseline.name},
                    {"name": "missing-baseline", "baseline": "gone.json"},
                    {"name": "disabled", "disabled": True, "baseline": ""},
                    {"name": "tagged", "baseline": ok_baseline.name, "tags": ["slow"]},
                ]
            )

            report = corpus_manifest.validate_suite(suite, fixture_root=workspace.fixture_root)

        totals = report.totals
        self.assertEqual(totals["entries"], 6)
        self.assertEqual(totals["selected_entries"], 4)
        self.assertEqual(totals["verified_entries"], 1)
        self.assertEqual(totals["runnable_entries"], 2)
        self.assertEqual(totals["hash_drift_entries"], 1)
        self.assertEqual(totals["empty_entries"], 1)
        self.assertEqual(totals["unavailable_entries"], 1)
        self.assertEqual(totals["disabled_entries"], 1)
        self.assertEqual(totals["skipped_entries"], 1)
        self.assertFalse(report.available)
        self.assertFalse(report.verified)
        self.assertIn(corpus_manifest.REASON_BASELINE_MISSING, report.reasons)

        payload_dict = report.to_dict()
        self.assertIsInstance(json.dumps(payload_dict), str)
        compact = corpus_manifest.compact_dict(report)
        self.assertEqual(len(compact["entries"]), 6)
        self.assertNotIn("fixtures", compact["entries"][0])


class CorpusManifestCliTests(unittest.TestCase):
    def _verified_suite(self, workspace: CorpusManifestWorkspace) -> Path:
        payload = b"cli-fixture"
        fixture = workspace.fixture("cli.pdf", payload)
        baseline = workspace.baseline("b.json", [{"fixture": str(fixture), **workspace.digest(payload)}])
        return workspace.suite([{"name": "cli", "baseline": baseline.name}])

    def test_cli_require_verified_exit_codes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            verified = self._verified_suite(workspace)
            unverified = workspace.suite(
                [{"name": "missing", "baseline": "gone.json"}],
                name="unverified.json",
            )
            with patch("sys.stdout"), patch("sys.stderr"):
                self.assertEqual(corpus_manifest.main(["--suite", str(verified)]), 0)
                self.assertEqual(
                    corpus_manifest.main(["--suite", str(unverified)]),
                    0,
                )
                self.assertEqual(
                    corpus_manifest.main(["--suite", str(unverified), "--require-verified"]),
                    1,
                )
                self.assertEqual(
                    corpus_manifest.main(["--suite", str(verified), "--require-verified"]),
                    0,
                )

    def test_cli_writes_legacy_available_output_and_report(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = CorpusManifestWorkspace(root)
            verified = self._verified_suite(workspace)
            github_output = root / "github_output.txt"
            report_path = root / "corpus.json"

            with patch("sys.stdout"):
                exit_code = corpus_manifest.main(
                    [
                        "--suite",
                        str(verified),
                        "--github-output",
                        str(github_output),
                        "--out",
                        str(report_path),
                        "--quiet",
                    ]
                )

            outputs = dict(
                line.split("=", 1)
                for line in github_output.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(outputs["available"], "true")
        self.assertEqual(outputs["verified"], "true")
        self.assertEqual(outputs["status"], corpus_manifest.STATUS_VERIFIED)
        self.assertEqual(outputs["verified_entries"], "1")
        self.assertEqual(outputs["report"], str(report_path))
        self.assertTrue(report["verified"])
        self.assertEqual(report["schema_version"], corpus_manifest.SCHEMA_VERSION)

    def test_cli_prints_json_report(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = CorpusManifestWorkspace(Path(temp_dir))
            suite = workspace.suite([{"name": "missing", "baseline": "gone.json"}])
            with patch("sys.stdout") as stdout:
                exit_code = corpus_manifest.main(["--suite", str(suite), "--json"])

        printed = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        payload = json.loads(printed)
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], corpus_manifest.REASON_BASELINE_MISSING)


if __name__ == "__main__":
    unittest.main()
