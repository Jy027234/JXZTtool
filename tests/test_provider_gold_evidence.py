from __future__ import annotations

import json
from pathlib import Path

from tools.provider_gold_evidence import build_evidence_packet, build_risk_review_index


def test_build_evidence_packet_keeps_pending_status_and_hashes_artifacts(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as handle:
        writer.write(handle)
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "id": "review-doc-p1",
                        "document_id": "doc",
                        "page_number": 1,
                        "review_status": "pending",
                        "review": {"reviewer": ""},
                        "expected": {"anchors": []},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    source_map = tmp_path / "sources.json"
    source_map.write_text(json.dumps({"doc": str(source)}), encoding="utf-8")

    def fake_render(source_path: Path, page_number: int, output_prefix: Path, *, dpi: int) -> Path:
        assert source_path == source.resolve()
        assert page_number == 1
        assert dpi == 120
        output = output_prefix.with_suffix(".png")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png-evidence")
        return output

    packet = build_evidence_packet(
        queue_path=queue,
        source_map_path=source_map,
        output_dir=tmp_path / "packet",
        dpi=120,
        render_page=fake_render,
    )

    assert packet["page_count"] == 1
    assert packet["pending_page_count"] == 1
    assert packet["pages"][0]["review_status"] == "pending"
    assert packet["pages"][0]["source_probe"]["text_chars"] == 0
    screenshot = tmp_path / "packet" / packet["pages"][0]["evidence"]["screenshot"]
    text = tmp_path / "packet" / packet["pages"][0]["evidence"]["text"]
    assert screenshot.read_bytes() == b"png-evidence"
    assert text.read_text(encoding="utf-8") == ""
    assert (tmp_path / "packet" / "manifest.json").exists()
    assert (tmp_path / "packet" / "README.md").exists()


def test_risk_review_index_links_priority_pages_without_approving_them(tmp_path: Path) -> None:
    packet_dir = tmp_path / "packet"
    packet_dir.mkdir()
    manifest = packet_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "page_count": 1,
                "pending_page_count": 1,
                "pages": [
                    {
                        "id": "review-doc-p1",
                        "page_number": 1,
                        "review_status": "pending",
                        "evidence": {"screenshot": "pages/doc/p0001.png", "text": "pages/doc/p0001.txt"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text(
        json.dumps(
            {
                "gold_evaluation": {
                    "risk_summary": {
                        "priority_pages": [
                            {
                                "page_id": "review-doc-p1",
                                "page_number": 1,
                                "risk_codes": ["candidate_slower_than_baseline", "table_count_changed"],
                                "candidate_elapsed_s": 2.0,
                                "baseline_elapsed_s": 1.0,
                                "candidate_blocks": 4,
                                "baseline_blocks": 2,
                                "candidate_tables": 0,
                                "baseline_tables": 1,
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = build_risk_review_index(evidence_manifest_path=manifest, evaluation_path=evaluation)

    review = packet_dir / "RISK_REVIEW.md"
    assert result["priority_page_count"] == 1
    assert "candidate_slower_than_baseline" in review.read_text(encoding="utf-8")
    assert "pages/doc/p0001.png" in review.read_text(encoding="utf-8")
    updated = json.loads(manifest.read_text(encoding="utf-8"))
    assert updated["pages"][0]["review_status"] == "pending"
    assert updated["risk_review"]["priority_page_count"] == 1
