"""Tests for the host-injectable integration surface (vector / LLM hooks).

ParseCore is positioned as an embedded library: it never owns vector or LLM
model lifecycles.  These tests pin the small Protocol/injection contract that
host products rely on.
"""

from __future__ import annotations

import unittest

from parsecore.contracts import BoundaryRefiner, SemanticRefiner
from parsecore.models import Block, BlockType
from parsecore.parsers import PdfTextParser, _refine_with_semantic_similarity
from parsecore.quality import evaluate_parse_quality


class _FakeSemanticRefiner:
    def __init__(self, scores: dict[tuple[str, str], float]) -> None:
        self._scores = scores
        self.calls = 0

    def similarity(self, *, left: str, right: str) -> float:
        self.calls += 1
        return self._scores.get((left, right), 0.0)


class _FakeBoundaryRefiner:
    def refine(self, *, paragraph: str, context=None) -> str:
        return paragraph


class SemanticRefinerProtocolTests(unittest.TestCase):
    def test_runtime_checkable_recognises_host_implementation(self) -> None:
        refiner = _FakeSemanticRefiner({})
        self.assertIsInstance(refiner, SemanticRefiner)

    def test_boundary_refiner_protocol_recognises_host_implementation(self) -> None:
        self.assertIsInstance(_FakeBoundaryRefiner(), BoundaryRefiner)


class RefineWithSemanticSimilarityTests(unittest.TestCase):
    def test_merges_high_similarity_adjacent_paragraphs(self) -> None:
        refiner = _FakeSemanticRefiner({("alpha", "beta"): 0.95})
        merged = _refine_with_semantic_similarity(
            ["alpha", "beta", "gamma"],
            refiner=refiner,
            merge_threshold=0.86,
            split_threshold=0.35,
        )
        self.assertEqual(len(merged), 2)
        self.assertIn("alpha", merged[0])
        self.assertIn("beta", merged[0])

    def test_keeps_paragraphs_when_similarity_low(self) -> None:
        refiner = _FakeSemanticRefiner({("alpha", "beta"): 0.10})
        merged = _refine_with_semantic_similarity(
            ["alpha", "beta"],
            refiner=refiner,
            merge_threshold=0.86,
            split_threshold=0.35,
        )
        self.assertEqual(merged, ["alpha", "beta"])

    def test_swallows_refiner_exceptions(self) -> None:
        class _Boom:
            def similarity(self, *, left, right):  # noqa: D401
                raise RuntimeError("model offline")

        merged = _refine_with_semantic_similarity(
            ["alpha", "beta"],
            refiner=_Boom(),
            merge_threshold=0.86,
            split_threshold=0.35,
        )
        self.assertEqual(merged, ["alpha", "beta"])


class PdfTextParserInjectionTests(unittest.TestCase):
    def test_accepts_semantic_refiner_constructor_arg(self) -> None:
        refiner = _FakeSemanticRefiner({})
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            semantic_refiner=refiner,
        )
        self.assertIs(parser._semantic_refiner, refiner)

    def test_default_semantic_refiner_is_none(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
        )
        self.assertIsNone(parser._semantic_refiner)


class RecommendedActionSignalsTests(unittest.TestCase):
    def _block(self, text: str, page: int = 1) -> Block:
        return Block(
            block_id=f"blk-{page}-{hash(text) & 0xffff}",
            doc_id="doc-1",
            type=BlockType.PARAGRAPH,
            content=text,
            metadata={"page": page},
        )

    def test_docx_single_page_recommends_vector_refine(self) -> None:
        # 25 paragraphs collapsed onto a single page → docx_single_page flag.
        blocks = [self._block(f"paragraph {i}", page=1) for i in range(25)]
        summary = evaluate_parse_quality(blocks)
        self.assertIn("docx_single_page", summary.flags)
        self.assertEqual(summary.recommended_action, "retry_with_vector_refine")

    def test_clean_output_has_no_recommendation(self) -> None:
        blocks = [
            self._block("Section heading: introduction.", page=1),
            self._block("Body paragraph one with adequate length.", page=2),
            self._block("Body paragraph two also long enough.", page=3),
        ]
        summary = evaluate_parse_quality(blocks)
        self.assertIsNone(summary.recommended_action)


if __name__ == "__main__":
    unittest.main()
