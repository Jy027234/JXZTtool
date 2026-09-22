from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from parsecore.assets import write_region_assets
from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, Chunk, ParseRequest
from parsecore.run_store import RunArtifacts
from tests.support import TemporaryWorkspace
from tests.test_read_consistency import CONFIG, DOCX_MEDIA_TYPE


def publish(runtime, *, text="OLD", tenant="default", doc_id="doc", assets=(), warning=False):
    block_id = f"{tenant}-{doc_id}-{text}"
    return runtime.publish_run(
        doc_id=doc_id,
        tenant_id=tenant,
        job_id=f"job-{text}",
        artifacts=RunArtifacts(
            blocks=(Block(block_id, doc_id, BlockType.PARAGRAPH, text, {"page": 1}),),
            chunks=(Chunk(f"c-{block_id}", doc_id, (block_id,), text, "en"),),
            views={
                "pages": [{"page_number": 1, "text": text}],
                "records": [{"record_id": "r1", "fields": {"value": text}, "page_start": 1, "page_end": 1}],
            },
            assets=assets,
        ),
        validation_status="warning" if warning else "passed",
    )


class ReviewPublicationIntegrityTests(unittest.TestCase):
    def test_new_region_render_does_not_overwrite_an_older_versions_file(self):
        with TemporaryWorkspace(CONFIG) as workspace:
            block = Block("same-block", "doc", BlockType.TABLE, "table", {
                "source_region": {"kind": "pdf_table", "page_number": 1, "bbox": [0, 0, 10, 10]},
            })
            with patch("parsecore.assets.render_region_png", side_effect=[(b"OLD", 10, 10), (b"NEW", 10, 10)]):
                _, old, _ = write_region_assets(source_path="source.pdf", blocks=(block,), root=workspace.root, doc_id="doc")
                _, new, _ = write_region_assets(source_path="source.pdf", blocks=(block,), root=workspace.root, doc_id="doc")
            self.assertEqual(Path(old[0].path).read_bytes(), b"OLD")
            self.assertEqual(Path(new[0].path).read_bytes(), b"NEW")
            self.assertNotEqual(old[0].path, new[0].path)

    def test_sweep_without_tenant_keeps_each_tenants_active_and_preview(self):
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            formal = publish(runtime, tenant="tenant-b")
            preview = publish(runtime, tenant="tenant-b", text="NEW", warning=True)
            result = runtime.cleanup_run_artifacts(retention_seconds=0, dry_run=False)
            self.assertEqual(result["eligible_runs"], 0)
            self.assertIn(formal["parse_run_id"], result["protected_runs"])
            self.assertIn(preview["parse_run_id"], result["protected_runs"])

    def test_sweep_keeps_files_referenced_by_another_retained_run(self):
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            path = workspace.root / "shared.png"
            path.write_bytes(b"shared-image")
            asset = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            publish(runtime, assets=(asset,))
            formal = publish(runtime, text="NEW", assets=(asset,))
            result = runtime.cleanup_run_artifacts(retention_seconds=0, dry_run=False)
            self.assertTrue(path.exists(), "the active run still owns the same file")
            self.assertEqual(result["deleted_files"], 0)
            self.assertIn(formal["parse_run_id"], result["protected_runs"])

    def test_review_cannot_publish_another_tenants_run_for_same_doc_id(self):
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            preview = publish(runtime, tenant="tenant-b", warning=True)
            with self.assertRaises(LookupError):
                runtime.publish_reviewed_run(doc_id="doc", tenant_id="tenant-a", parse_run_id=preview["parse_run_id"], reason="reviewed")
            self.assertIsNone(runtime.publication_state(doc_id="doc", tenant_id="tenant-b")["active_run_id"])

    def test_records_fast_path_reports_the_version_it_serves(self):
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            old = publish(runtime)
            source = workspace.create_docx("new.docx", ["NEW"])
            runtime.start(ParseRequest("doc", str(source), DOCX_MEDIA_TYPE))
            publish(runtime, text="NEW", warning=True)
            records = runtime.get_document_records_projection(doc_id="doc", limit=1)
            self.assertEqual(records["parse_run_id"], "job-OLD")
            self.assertEqual(records["items"][0]["fields"]["value"], "OLD")
            self.assertEqual(records["publication"]["served_run_id"], old["parse_run_id"])

    def test_sweep_keeps_an_execution_still_writing_staged_artifacts(self):
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            run = runtime.run_store.begin_run(doc_id="doc", job_id="running")
            runtime.run_store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
            runtime.run_store.save_run_artifacts(
                parse_run_id=run.parse_run_id,
                artifacts=RunArtifacts(blocks=(Block("b", "doc", BlockType.PARAGRAPH, "partial", {}),)),
            )
            result = runtime.cleanup_run_artifacts(retention_seconds=0, dry_run=False)
            self.assertEqual(result["eligible_runs"], 0)
            self.assertIn(run.parse_run_id, result["protected_runs"])

    def test_document_read_never_mixes_a_publication_between_live_reads(self):
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            source = workspace.create_docx("old.docx", ["OLD"])
            runtime.start(ParseRequest("doc", str(source), DOCX_MEDIA_TYPE))
            old = publish(runtime)
            get_chunks = runtime.job_store.get_chunks

            def publish_between_reads(**kwargs):
                publish(runtime, text="NEW")
                return get_chunks(**kwargs)

            with patch.object(runtime.job_store, "get_chunks", side_effect=publish_between_reads):
                snapshot = runtime.get_document(doc_id="doc", document_view_types=("pages",))
            body = snapshot["blocks"][0].content
            self.assertEqual(snapshot["chunks"][0].text, body)
            self.assertEqual(snapshot["document_views"]["pages"][0]["text"], body)
            if body == "OLD":
                self.assertEqual(snapshot["publication"]["served_run_id"], old["parse_run_id"])


if __name__ == "__main__":
    unittest.main()
