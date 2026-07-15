from __future__ import annotations

import json
from pathlib import Path

from tools.ai_gold_review import build_ai_review


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    elif isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_bytes(payload)  # type: ignore[arg-type]


def test_build_ai_review_records_explicit_nonhuman_scope(tmp_path: Path) -> None:
    packet = tmp_path / "packet"
    screenshot = packet / "pages" / "doc" / "p0001.png"
    text_probe = packet / "pages" / "doc" / "p0001.txt"
    _write(screenshot, b"png")
    _write(text_probe, "Title\nBody")
    import hashlib

    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    queue = tmp_path / "queue.json"
    _write(
        queue,
        {
            "minimum_approved_pages": 1,
            "pages": [
                {
                    "id": "review-doc-p1",
                    "document_id": "doc",
                    "page_number": 1,
                    "review_status": "pending",
                    "review": {},
                    "expected": {},
                }
            ],
        },
    )
    manifest = packet / "manifest.json"
    _write(
        manifest,
        {
            "pages": [
                {
                    "id": "review-doc-p1",
                    "source_probe": {"text_chars": 10, "image_count": 0},
                    "evidence": {
                        "screenshot": "pages/doc/p0001.png",
                        "screenshot_sha256": sha(screenshot),
                        "text": "pages/doc/p0001.txt",
                        "text_sha256": sha(text_probe),
                    },
                }
            ]
        },
    )
    evaluation = tmp_path / "evaluation.json"
    _write(
        evaluation,
        {
            "comparison": {
                "samples": [
                    {
                        "sample_name": "review-doc-p1",
                        "providers": [
                            {
                                "provider_id": "pdf-text",
                                "status": "done",
                                "gold_evidence": [
                                    {
                                        "position": 1,
                                        "page_number": 1,
                                        "kind": "title",
                                        "text": "Title",
                                        "provider_id": "pdf-text",
                                    },
                                    {
                                        "position": 2,
                                        "page_number": 1,
                                        "kind": "paragraph",
                                        "text": "Body",
                                        "provider_id": "pdf-text",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    )

    queue_out, audit = build_ai_review(
        queue_path=queue,
        manifest_path=manifest,
        evaluation_path=evaluation,
        visual_spot_checked_ids=["review-doc-p1"],
        reviewed_at="2026-07-14T18:00:00+08:00",
    )

    assert audit["status"] == "ok"
    assert audit["scope"] == "ai_assisted_review_not_human_gold"
    assert queue_out["pages"][0]["review_status"] == "approved"
    assert queue_out["pages"][0]["review"]["reviewer"].startswith("Codex")
    assert queue_out["pages"][0]["expected"]["anchors"] == ["Title", "Body"]


def test_build_ai_review_accepts_confirmed_blank_page_and_forbids_synthetic_title(tmp_path: Path) -> None:
    packet = tmp_path / "packet"
    screenshot = packet / "pages" / "doc" / "p0001.png"
    text_probe = packet / "pages" / "doc" / "p0001.txt"
    _write(screenshot, b"png")
    _write(text_probe, "")
    import hashlib

    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    queue = tmp_path / "queue.json"
    _write(
        queue,
        {"pages": [{"id": "review-doc-p1", "document_id": "doc", "page_number": 1, "review_status": "pending"}]},
    )
    manifest = packet / "manifest.json"
    _write(
        manifest,
        {
            "pages": [
                {
                    "id": "review-doc-p1",
                    "source_probe": {"text_chars": 0, "image_count": 0},
                    "evidence": {
                        "screenshot": "pages/doc/p0001.png",
                        "screenshot_sha256": sha(screenshot),
                        "text": "pages/doc/p0001.txt",
                        "text_sha256": sha(text_probe),
                    },
                }
            ]
        },
    )
    evaluation = tmp_path / "evaluation.json"
    _write(
        evaluation,
        {"comparison": {"samples": [{"sample_name": "review-doc-p1", "providers": [{"provider_id": "pdf-text", "status": "failed"}]}]}},
    )

    queue_out, audit = build_ai_review(queue_path=queue, manifest_path=manifest, evaluation_path=evaluation)

    assert audit["status"] == "ok"
    assert queue_out["pages"][0]["review_status"] == "approved"
    assert queue_out["pages"][0]["expected"]["mustNotBeHeading"]
