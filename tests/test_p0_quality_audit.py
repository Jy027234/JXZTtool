from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.p0_quality_audit as quality_audit
from tools.p0_quality_audit import (
    _apply_embedding_override,
    _coverage_metrics,
    _expected_page_count,
    _pdf_page_extractability,
    load_sample_specs,
    traceability_metrics,
)


def test_load_sample_specs_resolves_root_and_validates_ranges(tmp_path: Path) -> None:
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4")
    manifest = tmp_path / "samples.json"
    manifest.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "id": "sample",
                        "category": "ordinary_pdf",
                        "file_name": "sample.pdf",
                        "page_start": 2,
                        "page_end": 4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = load_sample_specs(manifest, sample_root=tmp_path)

    assert specs[0].path == sample.resolve()
    assert specs[0].page_start == 2
    assert specs[0].page_end == 4


def test_load_sample_specs_rejects_incomplete_page_range(tmp_path: Path) -> None:
    manifest = tmp_path / "samples.json"
    manifest.write_text(
        json.dumps({"samples": [{"id": "bad", "file_name": "x.pdf", "page_start": 2}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="page_range_incomplete"):
        load_sample_specs(manifest, sample_root=tmp_path)


def test_traceability_requires_source_block_and_page() -> None:
    blocks = [
        SimpleNamespace(block_id="b1", metadata={"page": 3}),
        SimpleNamespace(block_id="b2", metadata={}),
    ]
    chunks = [
        SimpleNamespace(chunk_id="c1", block_ids=("b1",)),
        SimpleNamespace(chunk_id="c2", block_ids=("b2",)),
        SimpleNamespace(chunk_id="c3", block_ids=("missing",)),
    ]

    metrics = traceability_metrics(blocks, chunks)

    assert metrics["chunk_count"] == 3
    assert metrics["chunks_with_source_block"] == 2
    assert metrics["chunks_with_source_page"] == 1
    assert metrics["fully_traceable_chunk_count"] == 1
    assert metrics["traceability_ratio"] == pytest.approx(1 / 3)
    assert metrics["missing_source_ids"] == ["missing"]


def test_coverage_metrics_requires_reason_for_every_gap_page() -> None:
    pages = [
        {"page_number": 1, "missing_reason": "chunks_not_embedded", "quality_signal_codes": ["rag_chunks_not_embedded"]},
        {"page_number": 2, "unembedded_unit_ids": ["u2"], "quality_signal_codes": []},
        {"page_number": 3, "quality_signal_codes": ["ocr_attempted"]},
    ]

    metrics = _coverage_metrics(pages)

    assert metrics["gap_page_count"] == 2
    assert metrics["pages_with_missing_reason"] == 1
    assert metrics["missing_reason_complete"] is False
    assert metrics["missing_reason_counts"] == {"chunks_not_embedded": 1}


def test_pdf_page_extractability_marks_blank_page(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)

    probe = _pdf_page_extractability(path, 1)

    assert probe["status"] == "empty"
    assert probe["text_chars"] == 0
    assert probe["image_count"] == 0


def test_full_pdf_expected_page_count_is_used_for_audit_denominator(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    path = tmp_path / "source.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    manifest = tmp_path / "samples.json"
    manifest.write_text(
        json.dumps({"samples": [{"id": "source", "file_name": "source.pdf"}]}),
        encoding="utf-8",
    )

    spec = load_sample_specs(manifest, sample_root=tmp_path)[0]

    assert _expected_page_count(spec) == 3


def test_apply_embedding_override_is_explicit_and_local() -> None:
    from parsecore.stubs import NullEmbeddingProvider

    runtime = type("Runtime", (), {"embedding_provider": NullEmbeddingProvider()})()

    assert _apply_embedding_override(runtime, "fake") == "fake"
    assert runtime.embedding_provider.__class__.__name__ == "FakeEmbeddingProvider"

    configured_provider = object()
    runtime.embedding_provider = configured_provider
    assert _apply_embedding_override(runtime, "configured") == "configured"
    assert runtime.embedding_provider is configured_provider

    with pytest.raises(ValueError, match="unsupported_embedding_override"):
        _apply_embedding_override(runtime, "remote")


def test_build_report_supports_sample_batches_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    manifest = tmp_path / "samples.json"
    manifest.write_text(
        json.dumps(
            {
                "samples": [
                    {"id": "a", "category": "text", "file_name": "a.txt"},
                    {"id": "b", "category": "text", "file_name": "b.txt"},
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        providers=SimpleNamespace(
            embedding=SimpleNamespace(enabled=True, provider="fake")
        )
    )
    monkeypatch.setattr(
        quality_audit,
        "build_runtime",
        lambda _config: SimpleNamespace(settings=settings, embedding_provider=object()),
    )
    monkeypatch.setattr(quality_audit, "_apply_embedding_override", lambda *_args: "configured")
    calls: list[str] = []

    def fake_audit(_runtime, spec, *, index, temp_root, full_dir, progress):
        del index, temp_root, full_dir, progress
        calls.append(spec.sample_id)
        return (
            {
                "sample_id": spec.sample_id,
                "status": "done",
                "parsed_page_count": 1,
                "block_count": 1,
                "chunk_count": 1,
                "missing_page_numbers": [],
                "quality_signal_codes": [],
                "coverage_metrics": {
                    "missing_reason_complete": True,
                    "missing_reason_counts": {},
                },
                "traceability": {"traceability_ratio": 1.0},
            },
            [{"sample_id": spec.sample_id, "page_number": 1}],
        )

    monkeypatch.setattr(quality_audit, "_audit_one", fake_audit)
    out_dir = tmp_path / "audit"
    first, first_rows = quality_audit.build_report(
        config="unused",
        manifest=manifest,
        sample_root=tmp_path,
        out_dir=out_dir,
        sample_ids=["a"],
    )
    (out_dir / "summary.json").write_text(json.dumps(first), encoding="utf-8")
    (out_dir / "coverage_report.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in first_rows),
        encoding="utf-8",
    )

    resumed, resumed_rows = quality_audit.build_report(
        config="unused",
        manifest=manifest,
        sample_root=tmp_path,
        out_dir=out_dir,
        sample_ids=["a", "b"],
        resume=True,
    )

    assert calls == ["a", "b"]
    assert resumed["summary"]["sample_count"] == 2
    assert resumed["summary"]["completed_sample_count"] == 2
    assert {row["sample_id"] for row in resumed_rows} == {"a", "b"}
    (out_dir / "summary.json").write_text(json.dumps(resumed), encoding="utf-8")
    (out_dir / "coverage_report.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in resumed_rows),
        encoding="utf-8",
    )

    rerun, rerun_rows = quality_audit.build_report(
        config="unused",
        manifest=manifest,
        sample_root=tmp_path,
        out_dir=out_dir,
        rerun_sample_ids=["a"],
        resume=True,
    )

    assert calls == ["a", "b", "a"]
    assert rerun["summary"]["sample_count"] == 2
    assert {row["sample_id"] for row in rerun_rows} == {"a", "b"}
