"""待办 1/3: the picture behind a table or figure region is kept, not just located.

``source_region`` says where a table or a figure is on the page; this module
checks that the picture itself can be written, referenced from the block, and
verified by digest — and that it stays off until a deployment asks for it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from parsecore.assets import (
    REGION_ASSET_SCHEMA_VERSION,
    local_object_store_root,
    region_assets_manifest,
    write_region_assets,
)
from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, ParseRequest
from parsecore.run_store import RunArtifacts, SealError
from tests.support import TemporaryWorkspace

PDF_CONFIG = """
[project]
name = "test-region-assets"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
region_assets = true

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
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()

PDF_CONFIG_ASSETS_OFF = PDF_CONFIG.replace("region_assets = true", "region_assets = false")

PAGE = [
    (60.0, 700.0, "Parts list"),
    (60.0, 660.0, "Bolt 2 pcs"),
    (60.0, 620.0, "Nut 2 pcs"),
]


def _region_block(block_id: str, *, kind: str, bbox: list[float]) -> Block:
    return Block(
        block_id=block_id,
        doc_id="doc-assets",
        type=BlockType.TABLE if kind == "pdf_table" else BlockType.IMAGE,
        content="Parts list",
        metadata={
            "page": 1,
            "source_region": {
                "kind": kind,
                "page_number": 1,
                "bbox": bbox,
                "unit": "pt",
                "origin": "top_left",
                "rotation_degrees": 0,
                "page_width": 612.0,
                "page_height": 792.0,
                "crop_fraction": [0.08, 0.13, 0.49, 0.25],
            },
        },
    )


class RegionAssetWriterTests(unittest.TestCase):
    def test_a_table_and_a_figure_region_become_verifiable_files(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            document_path = workspace.create_positioned_pdf("parts.pdf", [PAGE])
            root = workspace.root / "object-store"
            plain = Block(
                block_id="doc-assets-p-1",
                doc_id="doc-assets",
                type=BlockType.PARAGRAPH,
                content="Parts list",
                metadata={"page": 1},
            )
            blocks, assets, failure = write_region_assets(
                source_path=str(document_path),
                blocks=(
                    _region_block("doc-assets-t-1", kind="pdf_table", bbox=[50.0, 100.0, 300.0, 200.0]),
                    _region_block("doc-assets-i-1", kind="pdf_figure", bbox=[320.0, 100.0, 520.0, 220.0]),
                    plain,
                ),
                root=root,
                doc_id="doc-assets",
            )

            self.assertIsNone(failure)
            self.assertEqual([asset.kind for asset in assets], ["pdf_table", "pdf_figure"])
            files = [Path(asset.path) for asset in assets]
            contents = [path.read_bytes() for path in files]
            self.assertTrue(all(path.exists() for path in files))
            self.assertTrue(all(content.startswith(b"\x89PNG") for content in contents))
            for asset, content in zip(assets, contents):
                self.assertEqual(asset.sha256, hashlib.sha256(content).hexdigest())
                self.assertEqual(asset.bytes, len(content))
                self.assertGreater(asset.width, 0)
                self.assertGreater(asset.height, 0)
            # The descriptor travels with the block, including where it came from.
            descriptor = blocks[0].metadata["asset"]
            self.assertEqual(descriptor["schema_version"], REGION_ASSET_SCHEMA_VERSION)
            self.assertEqual(descriptor["page_number"], 1)
            self.assertEqual(descriptor["bbox"], [50.0, 100.0, 300.0, 200.0])
            self.assertEqual(descriptor["crop_fraction"], [0.08, 0.13, 0.49, 0.25])
            self.assertFalse(descriptor["verified"])
            # A block with no region is returned untouched.
            self.assertNotIn("asset", blocks[2].metadata)
            manifest = region_assets_manifest(assets)
            self.assertEqual(manifest["count"], 2)
            self.assertEqual(manifest["total_bytes"], sum(asset.bytes for asset in assets))

    def test_a_missing_source_or_a_bad_box_writes_nothing(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            blocks, assets, failure = write_region_assets(
                source_path="",
                blocks=(_region_block("doc-assets-t-1", kind="pdf_table", bbox=[50.0, 100.0, 300.0, 200.0]),),
                root=workspace.root / "object-store",
                doc_id="doc-assets",
            )
            self.assertEqual(assets, ())
            self.assertEqual(failure, "asset_source_unavailable")

            document_path = workspace.create_positioned_pdf("parts.pdf", [PAGE])
            blocks, assets, failure = write_region_assets(
                source_path=str(document_path),
                blocks=(
                    _region_block("doc-assets-t-2", kind="pdf_table", bbox=[300.0, 100.0, 50.0, 200.0]),
                    _region_block("doc-assets-t-3", kind="pdf_table", bbox=[50.0, 100.0, 300.0, 900.0]),
                ),
                root=workspace.root / "object-store",
                doc_id="doc-assets",
            )
            # An inverted box is not a region at all.  A box that reaches past the
            # page edge is clamped to the page: the asset is the part that exists,
            # and the descriptor reports the clamped box.
            self.assertEqual([asset.block_id for asset in assets], ["doc-assets-t-3"])
            self.assertEqual(assets[0].bbox, (50.0, 100.0, 300.0, 792.0))
            self.assertTrue(Path(assets[0].path).exists())
            self.assertNotIn("asset", blocks[0].metadata)
            self.assertEqual(blocks[1].metadata["asset"]["bbox"], [50.0, 100.0, 300.0, 792.0])

    def test_local_object_store_root_recognizes_only_local_urls(self) -> None:
        self.assertEqual(local_object_store_root("local://var/uploads"), Path("var/uploads"))
        self.assertIsNone(local_object_store_root("s3://bucket/prefix"))
        self.assertIsNone(local_object_store_root(""))


class RegionAssetRuntimeTests(unittest.TestCase):
    def test_the_parse_keeps_the_region_picture_when_the_deployment_asks(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            document_path = workspace.create_positioned_pdf("parts.pdf", [PAGE])
            runtime = build_runtime(workspace.config_path)
            self.assertTrue(runtime.settings.runtime.region_assets)
            request = ParseRequest(
                doc_id="doc-assets",
                file_path=str(document_path),
                media_type="application/pdf",
            )
            job = runtime.start(request)
            updated = runtime._attach_region_assets(
                job=runtime.get_job(job_id=job.job_id),
                request=request,
                blocks=(
                    _region_block("doc-assets-t-1", kind="pdf_table", bbox=[50.0, 100.0, 300.0, 200.0]),
                ),
            )
            descriptor = updated[0].metadata["asset"]
            written = Path(descriptor["path"])
            exists = written.exists()
            digest = hashlib.sha256(written.read_bytes()).hexdigest()

        self.assertTrue(exists)
        self.assertEqual(digest, descriptor["sha256"])
        self.assertEqual(descriptor["kind"], "pdf_table")

    def test_nothing_is_written_when_the_deployment_does_not_ask(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG_ASSETS_OFF) as workspace:
            document_path = workspace.create_positioned_pdf("parts.pdf", [PAGE])
            runtime = build_runtime(workspace.config_path)
            self.assertFalse(runtime.settings.runtime.region_assets)
            request = ParseRequest(
                doc_id="doc-assets-off",
                file_path=str(document_path),
                media_type="application/pdf",
            )
            job = runtime.start(request)
            blocks = (
                _region_block("doc-assets-off-t-1", kind="pdf_table", bbox=[50.0, 100.0, 300.0, 200.0]),
            )
            updated = runtime._attach_region_assets(
                job=runtime.get_job(job_id=job.job_id), request=request, blocks=blocks
            )
            png_files = list((workspace.root / "object-store").rglob("*.png"))

        self.assertIs(updated[0], blocks[0])
        self.assertNotIn("asset", updated[0].metadata)
        self.assertEqual(png_files, [])

    def test_a_parse_without_regions_writes_no_assets_and_does_not_fail(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            document_path = workspace.create_docx("plain.docx", ["Maintenance Manual"])
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-assets-plain",
                    file_path=str(document_path),
                    media_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                )
            )
            png_files = list((workspace.root / "object-store").rglob("*.png"))

        self.assertEqual(outcome.job.state.value, "done")
        self.assertEqual(png_files, [])


class RegionAssetPublicationTests(unittest.TestCase):
    def test_a_published_run_records_the_assets_it_owns(self) -> None:
        config = PDF_CONFIG.replace(
            "region_assets = true",
            'region_assets = true\nrun_artifact_profile = "assets-only"',
        )
        with TemporaryWorkspace(config) as workspace:
            document_path = workspace.create_positioned_pdf("parts.pdf", [PAGE])
            runtime = build_runtime(workspace.config_path)
            blocks, assets, failure = write_region_assets(
                source_path=str(document_path),
                blocks=(_region_block("doc-assets-t-1", kind="pdf_table", bbox=[50.0, 100.0, 300.0, 200.0]),),
                root=workspace.root / "object-store",
                doc_id="doc-assets",
            )
            self.assertIsNone(failure)
            published = runtime.publish_run(
                doc_id="doc-assets",
                artifacts=RunArtifacts(
                    blocks=blocks,
                    views={"pages": [{"page_number": 1, "text": "Parts list"}]},
                    assets=tuple(asset.as_payload() for asset in assets),
                ),
                job_id="job-assets",
                source_version="sha256:assets",
                validation_status="passed",
                expected_generation=0,
            )
            record = runtime.run_store.get_run(parse_run_id=published["parse_run_id"])

        self.assertTrue(published["published"])
        self.assertEqual(published["publication_generation"], 1)
        self.assertEqual(record.manifest["counts"]["assets"], 1)
        self.assertIn("assets", record.manifest["required_artifacts"])
        self.assertEqual(record.manifest["artifact_profile"], "assets-only")

    def test_a_run_whose_asset_file_vanished_does_not_become_the_formal_version(self) -> None:
        config = PDF_CONFIG.replace(
            "region_assets = true",
            'region_assets = true\nrun_artifact_profile = "assets-only"',
        )
        with TemporaryWorkspace(config) as workspace:
            document_path = workspace.create_positioned_pdf("parts.pdf", [PAGE])
            runtime = build_runtime(workspace.config_path)
            blocks, assets, _failure = write_region_assets(
                source_path=str(document_path),
                blocks=(_region_block("doc-assets-t-2", kind="pdf_table", bbox=[50.0, 100.0, 300.0, 200.0]),),
                root=workspace.root / "object-store",
                doc_id="doc-assets",
            )
            Path(assets[0].path).unlink()
            with self.assertRaises(SealError):
                runtime.publish_run(
                    doc_id="doc-assets",
                    artifacts=RunArtifacts(
                        blocks=blocks,
                        views={"pages": [{"page_number": 1, "text": "Parts list"}]},
                        assets=tuple(asset.as_payload() for asset in assets),
                    ),
                    job_id="job-assets-gone",
                    source_version="sha256:assets",
                    validation_status="passed",
                    expected_generation=0,
                )
            state = runtime.publication_state(doc_id="doc-assets")

        self.assertIsNone(state["active_run_id"])
        self.assertFalse(state["formal_version_available"])


if __name__ == "__main__":
    unittest.main()
