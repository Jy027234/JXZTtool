from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from parsecore.cli import main as cli_main
from parsecore.export_jobs import create_export_package, list_expired_export_packages
from tests.support import TemporaryWorkspace


INVENTORY_CONFIG = """
[project]
name = "test-export-inventory"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 1
poll_interval_ms = 25
export_artifact_retention_seconds = 3600

[storage]
database_url = "__DB_URL__"
object_store = "local://__OBJECT_STORE__"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[parsers]]
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
""".strip()


def _export_id() -> str:
    return f"exp_{uuid.uuid4().hex}"


def _write_manifest(export_root: Path, manifest: dict) -> Path:
    manifest_path = export_root / manifest["export_id"] / "manifest.json"
    manifest_path.write_bytes(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    )
    return manifest_path


def _set_created_at(export_root: Path, manifest: dict, value: datetime) -> dict:
    updated = dict(manifest)
    updated["created_at"] = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_manifest(export_root, updated)
    return updated


def _snapshot_tree(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = path.read_bytes() if path.is_file() else None
    return snapshot


def _write_incomplete_package(export_root: Path, export_id: str, manifest_text: str | None) -> None:
    package_dir = export_root / export_id
    package_dir.mkdir(parents=True)
    (package_dir / "writing.tmp").write_text("synthetic incomplete package", encoding="utf-8")
    if manifest_text is not None:
        (package_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")


class ExpiredExportInventoryTests(unittest.TestCase):
    def test_inventory_counts_valid_expired_package_bytes_and_skips_incomplete_or_unsafe_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export_root = Path(temporary) / "_exports"
            export_root.mkdir()
            now = datetime.now(timezone.utc).replace(microsecond=0)

            expired = create_export_package(
                {"doc_id": "synthetic-expired", "pages": [{"page_number": 1, "text": "expired"}]},
                export_root,
                includes=["pages"],
                formats={"pages": "jsonl"},
            )
            expired = _set_created_at(export_root, expired, now - timedelta(hours=3))
            # A repeated manifest entry must not make the same file count twice.
            expired["files"].append(dict(expired["files"][0]))
            _write_manifest(export_root, expired)

            recent = create_export_package(
                {"doc_id": "synthetic-recent", "pages": [{"page_number": 1, "text": "recent"}]},
                export_root,
                includes=["pages"],
                formats={"pages": "jsonl"},
            )
            recent = _set_created_at(export_root, recent, now - timedelta(minutes=30))

            missing_manifest_id = _export_id()
            _write_incomplete_package(export_root, missing_manifest_id, None)

            damaged_manifest_id = _export_id()
            _write_incomplete_package(export_root, damaged_manifest_id, "{ definitely not json")

            outside_path = export_root / "outside-canary.bin"
            outside_path.write_bytes(b"synthetic outside file; never count or modify")
            outside_id = _export_id()
            outside_dir = export_root / outside_id
            outside_dir.mkdir()
            outside_manifest = {
                "export_id": outside_id,
                "state": "done",
                "created_at": (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
                "files": [{"path": "../outside-canary.bin", "bytes": outside_path.stat().st_size}],
            }
            _write_manifest(export_root, outside_manifest)

            size_mismatch_id = _export_id()
            size_mismatch_dir = export_root / size_mismatch_id
            size_mismatch_dir.mkdir()
            (size_mismatch_dir / "page.jsonl").write_text("synthetic page", encoding="utf-8")
            size_mismatch_manifest = {
                "export_id": size_mismatch_id,
                "state": "done",
                "created_at": (now - timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
                "files": [{"path": "page.jsonl", "bytes": 1}],
            }
            _write_manifest(export_root, size_mismatch_manifest)

            # A package-shaped directory nested below the root and an unrelated
            # top-level file are outside the inventory's package scan.
            (export_root / "unrelated.txt").write_text("ignore me", encoding="utf-8")
            nested = export_root / "nested" / _export_id()
            nested.mkdir(parents=True)
            (nested / "manifest.json").write_text("{}", encoding="utf-8")

            before = _snapshot_tree(export_root)
            report = list_expired_export_packages(export_root, retention_seconds=3600)
            after = _snapshot_tree(export_root)

            self.assertEqual(before, after, "inventory must only read package files")
            self.assertEqual(report["export_root"], str(export_root.resolve()))
            self.assertEqual(report["retention_seconds"], 3600)
            self.assertEqual(report["expired_count"], 1)
            self.assertEqual(
                {item["export_id"] for item in report["expired_packages"]},
                {expired["export_id"]},
            )
            self.assertEqual(
                datetime.fromisoformat(report["expired_packages"][0]["created_at"]),
                datetime.fromisoformat(expired["created_at"].replace("Z", "+00:00")),
            )
            expected_expired_bytes = (
                (export_root / expired["export_id"] / "manifest.json").stat().st_size
                + sum(
                    (export_root / expired["export_id"] / entry["path"]).stat().st_size
                    for entry in {item["path"]: item for item in expired["files"]}.values()
                )
            )
            self.assertEqual(report["expired_bytes"], expected_expired_bytes)
            self.assertEqual(report["expired_packages"][0]["bytes"], expected_expired_bytes)
            self.assertEqual(
                set(report["skipped_packages"]),
                {missing_manifest_id, damaged_manifest_id, outside_id, size_mismatch_id},
            )
            self.assertEqual(outside_path.read_bytes(), b"synthetic outside file; never count or modify")

    def test_inventory_skips_package_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            export_root = base / "_exports"
            export_root.mkdir()
            outside_root = base / "outside"
            outside_manifest = create_export_package(
                {"doc_id": "synthetic-outside", "pages": [{"page_number": 1, "text": "outside"}]},
                outside_root,
                includes=["pages"],
                formats={"pages": "jsonl"},
            )
            _set_created_at(outside_root, outside_manifest, datetime.now(timezone.utc) - timedelta(hours=3))
            target = outside_root / outside_manifest["export_id"]
            outside_id = outside_manifest["export_id"]
            link = export_root / outside_id
            try:
                link.symlink_to(target, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            report = list_expired_export_packages(export_root, retention_seconds=3600)

            self.assertEqual(report["expired_count"], 0)
            self.assertIn(outside_id, report["skipped_packages"])
            self.assertTrue((target / "manifest.json").is_file())

    def test_cli_inventory_does_not_create_a_missing_exports_directory(self) -> None:
        with TemporaryWorkspace(INVENTORY_CONFIG) as workspace:
            assert workspace.root is not None
            assert workspace.config_path is not None
            export_root = workspace.root / "object-store" / "_exports"
            self.assertFalse(export_root.exists())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    ["list-expired-export-packages", "--config", str(workspace.config_path)]
                )

            self.assertEqual(exit_code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["export_root"], str(export_root.resolve()))
            self.assertEqual(report["retention_seconds"], 3600)
            self.assertEqual(report["expired_count"], 0)
            self.assertEqual(report["expired_bytes"], 0)
            self.assertEqual(report["expired_packages"], [])
            self.assertEqual(report["skipped_packages"], [])
            self.assertFalse(export_root.exists(), "read-only CLI inventory must not create _exports")

    def test_cli_inventory_leaves_generated_export_available_to_download(self) -> None:
        with TemporaryWorkspace(INVENTORY_CONFIG) as workspace:
            assert workspace.root is not None
            assert workspace.config_path is not None
            export_root = workspace.root / "object-store" / "_exports"
            manifest = create_export_package(
                {
                    "doc_id": "synthetic-download",
                    "tenant_id": "default",
                    "pages": [{"page_number": 1, "text": "download remains available"}],
                },
                export_root,
                includes=["pages"],
                formats={"pages": "jsonl"},
            )
            _set_created_at(export_root, manifest, datetime.now(timezone.utc) - timedelta(hours=3))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    ["list-expired-export-packages", "--config", str(workspace.config_path)]
                )
            report = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["expired_count"], 1)
            self.assertEqual(report["expired_packages"][0]["export_id"], manifest["export_id"])

            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get(
                    f"/v1/parse/export-jobs/{manifest['export_id']}/download",
                    params={"file": "pages.jsonl"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.text), {"page_number": 1, "text": "download remains available"})
            self.assertTrue((export_root / manifest["export_id"] / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
