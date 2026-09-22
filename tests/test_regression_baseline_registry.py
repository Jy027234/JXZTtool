from __future__ import annotations

import hashlib
import json
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import regression_baseline
from tools.regression_baseline import _baseline_version_metadata
from tools.regression_baseline_registry import validate_registry


class BaselineVersionMetadataTests(unittest.TestCase):
    def test_save_writes_snapshot_without_git_or_fixture_parsing(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "baseline.json"
            args = Namespace(
                out=str(output),
                config=Path(temporary) / "parsecore.toml",
                pdf=[],
                strip_headers_footers=False,
            )
            with (
                patch.object(regression_baseline, "_current_git_revision", return_value=None),
                patch.object(regression_baseline, "_current_source_tree_state", return_value="unknown"),
                patch.object(regression_baseline, "build_runtime"),
                patch.object(regression_baseline, "_apply_runtime_overrides"),
                patch.object(regression_baseline, "_request_option_overrides_from_args", return_value={}),
                patch.object(regression_baseline, "_resolve_fixture_root", return_value=None),
            ):
                self.assertEqual(regression_baseline._cmd_save(args), 0)

            metadata = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(metadata["schema_version"], 2)
        self.assertEqual(metadata["baseline_revision"], "baseline")
        self.assertIsNone(metadata["source_commit"])
        self.assertEqual(metadata["source_tree_state"], "unknown")
        self.assertIsNone(metadata["source_evidence"])

    def test_exact_commit_evidence_requires_clean_known_source(self) -> None:
        args = Namespace(
            baseline_revision="source-v1",
            source_evidence_id="source-run",
            source_evidence_sha256="a" * 64,
            source_evidence_provenance="exact_commit_run",
        )
        metadata = _baseline_version_metadata(
            args,
            output_path=Path("baseline.source.json"),
            source_commit="b" * 40,
            source_tree_state="clean",
        )
        self.assertEqual(metadata["source_evidence"]["provenance"], "exact_commit_run")
        for commit, state in ((None, "clean"), ("b" * 40, "dirty")):
            with self.subTest(commit=commit, state=state):
                with self.assertRaisesRegex(ValueError, "requires a known commit and clean"):
                    _baseline_version_metadata(
                        args,
                        output_path=Path("baseline.source.json"),
                        source_commit=commit,
                        source_tree_state=state,
                    )


class BaselineRegistryTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> str:
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def _build_registry(self, root: Path) -> tuple[Path, Path]:
        baseline_dir = root / "baselines"
        evidence_root = root / "evidence"
        baseline_dir.mkdir()
        evidence_root.mkdir()
        source = {
            "schema_version": 2,
            "baseline_revision": "source-v1",
            "source_commit": "a" * 40,
            "source_tree_state": "clean",
            "metrics": {"blocks": 10},
        }
        candidate = {"schema": "candidate/v1", "result": {"blocks": 12}}
        source_hash = self._write_json(baseline_dir / "source.json", source)
        candidate_hash = self._write_json(evidence_root / "candidate.json", candidate)
        registry = {
            "schema": "parsecore-regression-baseline-registry/v1",
            "schema_version": 1,
            "baseline_artifacts": [
                {
                    "id": "source",
                    "path": "source.json",
                    "sha256": source_hash,
                    "metadata": {
                        "schema_version": 2,
                        "baseline_revision": "source-v1",
                        "source_commit": "a" * 40,
                        "source_tree_state": "clean",
                    },
                    "projections": {
                        "source.blocks": {"path": "metrics.blocks", "value": 10}
                    },
                }
            ],
            "evidence_artifacts": [
                {
                    "id": "candidate",
                    "path": "candidate.json",
                    "sha256": candidate_hash,
                    "schema": "candidate/v1",
                    "projections": {
                        "candidate.blocks": {"path": "result.blocks", "value": 12}
                    },
                }
            ],
            "projections": {"source.blocks": 10, "candidate.blocks": 12},
            "comparisons": [
                {
                    "artifact_ids": ["source", "candidate"],
                    "metric_deltas": {
                        "blocks": {
                            "reference_projection": "source.blocks",
                            "candidate_projection": "candidate.blocks",
                            "delta": 2,
                        }
                    },
                }
            ],
        }
        registry_path = baseline_dir / "registry.json"
        registry_path.write_text(json.dumps(registry, sort_keys=True), encoding="utf-8")
        return registry_path, evidence_root

    def test_registry_validates_hashes_metadata_projections_and_delta(self) -> None:
        with TemporaryDirectory() as temporary:
            registry, evidence_root = self._build_registry(Path(temporary))
            self.assertEqual(validate_registry(registry, evidence_root), [])

    def test_registry_rejects_projection_and_artifact_hash_mismatches(self) -> None:
        with TemporaryDirectory() as temporary:
            registry_path, evidence_root = self._build_registry(Path(temporary))
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["projections"]["source.blocks"] = 11
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            errors = validate_registry(registry_path, evidence_root)
            self.assertTrue(any("projection" in error for error in errors))

            (evidence_root / "candidate.json").write_text("{}", encoding="utf-8")
            errors = validate_registry(registry_path, evidence_root)
            self.assertTrue(any("SHA-256 mismatch" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
