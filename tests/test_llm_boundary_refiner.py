"""待办 2: the boundary refiner may only move boundaries, never rewrite text.

The old protocol asked the model for the rewritten paragraph and accepted the
answer whenever it contained at least 85% of the original characters.  The R2
probe showed what that accepts: ``AB-123 torque 25`` came back as
``AB-124 torque 95``.  These tests pin the replacement protocol -- numbered
original fragments in, relations out, text assembled by the program -- and the
refusals that keep the original paragraph.
"""

from __future__ import annotations

import json
import unittest

from parsecore.llm import (
    LlmBoundaryRefiner,
    LlmRequestError,
    _split_refine_fragments,
    _assemble_refined_parts,
)


PARAGRAPH = (
    "Apply the specified torque to the fastener. "
    "NOTE: Do not reuse a self-locking nut.\n"
    "Step 2 Install the new bolt. 3. Torque to 25 N.m."
)

#: Markers at line starts, so the parser's own low-confidence gate accepts it.
GATED_PARAGRAPH = (
    "NOTE: Do not reuse a self-locking nut.\n"
    "3. Torque to 25 N.m.\n"
    "Step 4 Record the value."
)

PART_NUMBER_PARAGRAPH = "Refer to AB-123 torque 25 for the procedure."

#: A part number and a legal boundary in one paragraph.
PART_NUMBER_WITH_BOUNDARY = (
    "Refer to AB-123 torque 25 for the procedure.\n"
    "NOTE: Do not reuse a self-locking nut."
)


class _FakeClient:
    """Returns queued answers and records the prompts it was given."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        self.systems.append(str(system or ""))
        if not self.responses:
            raise AssertionError("unexpected extra llm call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return str(response)


def _state(client: _FakeClient, *, max_calls: int = 10):
    return LlmBoundaryRefiner(client, max_calls_per_doc=max_calls).begin_request()


def _plan(groups: list[list[int]], order: list[int] | None = None) -> str:
    payload: dict[str, object] = {"groups": groups}
    if order is not None:
        payload["order"] = order
    return json.dumps(payload)


class FragmentSplittingTests(unittest.TestCase):
    def test_fragments_are_verbatim_slices_of_the_paragraph(self) -> None:
        fragments = _split_refine_fragments(PARAGRAPH)
        self.assertEqual(
            "".join(fragment.raw for fragment in fragments), PARAGRAPH
        )
        self.assertEqual(
            [fragment.fragment_id for fragment in fragments],
            list(range(1, len(fragments) + 1)),
        )
        self.assertTrue(all(fragment.display for fragment in fragments))

    def test_only_source_boundaries_become_fragments(self) -> None:
        fragments = _split_refine_fragments(PARAGRAPH)
        # A sentence end, a NOTE block, a step marker and a numbered item.
        self.assertEqual(len(fragments), 4)
        self.assertEqual(
            fragments[0].display, "Apply the specified torque to the fastener."
        )
        self.assertEqual(fragments[1].display, "NOTE: Do not reuse a self-locking nut.")
        self.assertEqual(fragments[3].display, "3. Torque to 25 N.m.")

    def test_a_number_inside_a_sentence_is_never_a_boundary(self) -> None:
        # "25 N.m" and "AB-123" must stay inside their fragment: the model can
        # only choose between boundaries the source already provides.
        fragments = _split_refine_fragments(PART_NUMBER_PARAGRAPH)
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0].raw, PART_NUMBER_PARAGRAPH)

    def test_blank_input_has_no_fragments(self) -> None:
        self.assertEqual(_split_refine_fragments("   \n  "), ())


class RefinerProtocolTests(unittest.TestCase):
    def test_a_plan_splits_the_paragraph_and_reuses_original_text(self) -> None:
        client = _FakeClient(_plan([[1], [2], [3, 4]]))
        state = _state(client)
        parts = state.refine_paragraph(PARAGRAPH)
        fragments = _split_refine_fragments(PARAGRAPH)

        self.assertEqual(
            parts,
            [
                fragments[0].display,
                fragments[1].display,
                fragments[2].display + " " + fragments[3].display,
            ],
        )
        self.assertEqual("".join("".join(part.split()) for part in parts), "".join(PARAGRAPH.split()))
        self.assertEqual(state.calls_used, 1)
        self.assertEqual(state.rejections, {})

    def test_the_model_cannot_rewrite_a_number_or_a_part_number(self) -> None:
        # The R2 probe answer, in the new protocol: rewritten text is not a
        # relation, so it is refused and the original paragraph survives.
        client = _FakeClient(
            json.dumps({"groups": [["AB-124 torque 95"]], "reason": "cleaner"})
        )
        state = _state(client)

        parts = state.refine_paragraph(PART_NUMBER_WITH_BOUNDARY)

        self.assertEqual(parts, [PART_NUMBER_WITH_BOUNDARY])
        self.assertNotIn("AB-124", "".join(parts))
        self.assertIn("AB-123 torque 25", "".join(parts))
        self.assertEqual(state.calls_used, 1)
        self.assertEqual(state.calls_failed, 1)
        self.assertEqual(state.rejections, {"invalid_response": 1})

    def test_a_rewritten_paragraph_that_looks_like_text_is_refused(self) -> None:
        client = _FakeClient(
            "Apply 30 N.m of torque to the fastener, then check the part."
        )
        state = _state(client)
        parts = state.refine_paragraph(PART_NUMBER_WITH_BOUNDARY)

        self.assertEqual(parts, [PART_NUMBER_WITH_BOUNDARY])
        self.assertEqual(state.rejections, {"invalid_response": 1})

    def test_rewritten_text_in_a_group_is_refused(self) -> None:
        # Ids are what the protocol accepts; a quoted paragraph is not an id.
        fragments = _split_refine_fragments(PART_NUMBER_WITH_BOUNDARY)
        parts, reason = _assemble_refined_parts(
            response=json.dumps(
                {"groups": [["Refer to AB-124 torque 95 for the procedure."], [2]]}
            ),
            fragments=fragments,
            original=PART_NUMBER_WITH_BOUNDARY,
        )

        self.assertIsNone(parts)
        self.assertEqual(reason, "invalid_response")

    def test_a_prose_answer_that_rewrites_the_text_is_refused(self) -> None:
        client = _FakeClient(
            "Here is the corrected paragraph: Apply 30 N.m of torque instead."
        )
        state = _state(client)
        self.assertEqual(state.refine_paragraph(PARAGRAPH), [PARAGRAPH])
        self.assertEqual(state.calls_failed, 1)
        self.assertEqual(state.rejections, {"invalid_response": 1})

    def test_fragments_must_be_used_exactly_once(self) -> None:
        cases = {
            "missing_fragment": _plan([[1], [2]]),
            "duplicate_fragment": _plan([[1, 2], [2, 3]]),
            "unknown_fragment": _plan([[1], [2], [3], [4], [5]]),
            "empty_group": _plan([[1], [], [2], [3], [4]]),
        }
        for expected, response in cases.items():
            with self.subTest(reason=expected):
                client = _FakeClient(response)
                state = _state(client)
                self.assertEqual(state.refine_paragraph(PARAGRAPH), [PARAGRAPH])
                self.assertEqual(state.calls_failed, 1)
                self.assertEqual(state.rejections, {expected: 1})

    def test_order_must_be_a_permutation_of_the_groups(self) -> None:
        for order in ([0, 0, 1], [0, 1, 2], [0, 1, 9], [3, 2, 1, 0, 0]):
            with self.subTest(order=order):
                client = _FakeClient(_plan([[1], [2], [3], [4]], list(order)))
                state = _state(client)
                self.assertEqual(state.refine_paragraph(PARAGRAPH), [PARAGRAPH])
                self.assertEqual(state.rejections, {"invalid_order": 1})

    def test_a_reordered_plan_reorders_whole_fragments(self) -> None:
        client = _FakeClient(_plan([[1], [2], [3], [4]], [1, 0, 2, 3]))
        state = _state(client)
        parts = state.refine_paragraph(PARAGRAPH)
        fragments = _split_refine_fragments(PARAGRAPH)

        self.assertEqual(
            parts,
            [
                fragments[1].display,
                fragments[0].display,
                fragments[2].display,
                fragments[3].display,
            ],
        )
        # Reordering fragments never changes their text: the same pieces, in a
        # different sequence.
        self.assertEqual(
            sorted("".join(part.split()) for part in parts),
            sorted("".join(fragment.display.split()) for fragment in fragments),
        )

    def test_the_background_is_never_returned_unless_it_covers_everything(self) -> None:
        client = _FakeClient(_plan([[1, 2, 3, 4]]))
        state = _state(client)
        self.assertEqual(state.refine_paragraph(PARAGRAPH), [PARAGRAPH])
        self.assertEqual(state.rejections, {"no_change": 1})

    def test_a_markdown_fence_is_tolerated(self) -> None:
        client = _FakeClient("```json\n" + _plan([[1, 2], [3, 4]]) + "\n```")
        state = _state(client)
        parts = state.refine_paragraph(PARAGRAPH)
        self.assertEqual(len(parts), 2)

    def test_the_prompt_never_asks_for_the_text(self) -> None:
        client = _FakeClient(_plan([[1, 2], [3, 4]]))
        state = _state(client)
        state.refine_paragraph(PARAGRAPH)
        prompt = client.prompts[0]

        self.assertIn("1| Apply the specified torque to the fastener.", prompt)
        self.assertIn("4| 3. Torque to 25 N.m.", prompt)
        self.assertIn("do not retype or rewrite any text", prompt)
        self.assertNotIn("array of strings", prompt)


class RefinerBudgetAndFailureTests(unittest.TestCase):
    def test_a_paragraph_without_a_legal_boundary_costs_no_call(self) -> None:
        client = _FakeClient()
        state = _state(client)
        self.assertEqual(state.refine_paragraph("A single sentence about torque."), ["A single sentence about torque."])
        self.assertEqual(state.calls_used, 0)
        self.assertEqual(client.prompts, [])

    def test_the_answer_is_cached_per_paragraph(self) -> None:
        client = _FakeClient(_plan([[1, 2], [3, 4]]))
        state = _state(client)
        first = state.refine_paragraph(PARAGRAPH)
        second = state.refine_paragraph(PARAGRAPH)

        self.assertEqual(first, second)
        self.assertEqual(len(client.prompts), 1)

    def test_a_refusal_is_cached_too(self) -> None:
        client = _FakeClient("not json at all")
        state = _state(client)
        state.refine_paragraph(PARAGRAPH)
        state.refine_paragraph(PARAGRAPH)

        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(state.rejections, {"invalid_response": 1})

    def test_a_request_failure_keeps_the_original(self) -> None:
        client = _FakeClient(LlmRequestError("timeout"))
        state = _state(client)
        self.assertEqual(state.refine_paragraph(PARAGRAPH), [PARAGRAPH])
        self.assertEqual(state.calls_used, 1)
        self.assertEqual(state.calls_failed, 1)
        self.assertEqual(state.rejections, {"request_failed": 1})

    def test_the_call_budget_is_per_request(self) -> None:
        client = _FakeClient(_plan([[1, 2], [3, 4]]), _plan([[1, 2], [3, 4]]))
        refiner = LlmBoundaryRefiner(client, max_calls_per_doc=1)
        first = refiner.begin_request()
        second = refiner.begin_request()

        self.assertEqual(len(first.refine_paragraph(PARAGRAPH)), 2)
        self.assertEqual(first.calls_remaining, 0)
        # A second request has its own budget; one document cannot exhaust or
        # reset another document's allowance.
        self.assertEqual(second.calls_remaining, 1)
        self.assertEqual(len(second.refine_paragraph(PARAGRAPH)), 2)

    def test_exhausting_the_budget_keeps_the_original(self) -> None:
        client = _FakeClient(_plan([[1, 2], [3, 4]]))
        state = _state(client, max_calls=1)
        other = PARAGRAPH.replace("fastener", "bracket")

        self.assertEqual(len(state.refine_paragraph(PARAGRAPH)), 2)
        self.assertEqual(state.refine_paragraph(other), [other])
        self.assertEqual(state.rejections, {"call_budget_exhausted": 1})
        self.assertEqual(state.calls_used, 1)

    def test_too_many_fragments_are_not_sent(self) -> None:
        client = _FakeClient()
        state = _state(client)
        paragraph = " ".join(f"{index}. Step {index}." for index in range(1, 80))

        self.assertEqual(state.refine_paragraph(paragraph), [paragraph])
        self.assertEqual(client.prompts, [])
        self.assertEqual(state.rejections, {"too_many_fragments": 1})

    def test_the_reported_reason_is_the_last_refusal(self) -> None:
        client = _FakeClient("garbage")
        state = _state(client)
        state.refine_paragraph(PARAGRAPH)

        self.assertEqual(state.last_rejection, "invalid_response")
        self.assertEqual(state.rejections["invalid_response"], 1)


class AssembleHelperTests(unittest.TestCase):
    def test_official_plan_shape_is_accepted(self) -> None:
        fragments = _split_refine_fragments(PARAGRAPH)
        parts, reason = _assemble_refined_parts(
            response=_plan([[1, 2], [3], [4]]),
            fragments=fragments,
            original=PARAGRAPH,
        )

        self.assertEqual(reason, "")
        self.assertEqual(len(parts or []), 3)

    def test_stringified_ids_are_read_as_ids(self) -> None:
        fragments = _split_refine_fragments(PARAGRAPH)
        parts, reason = _assemble_refined_parts(
            response=json.dumps({"groups": [["1", "2"], ["3"], ["4"]]}),
            fragments=fragments,
            original=PARAGRAPH,
        )

        self.assertEqual(reason, "")
        self.assertEqual(len(parts or []), 3)

    def test_booleans_are_not_ids(self) -> None:
        fragments = _split_refine_fragments(PARAGRAPH)
        parts, reason = _assemble_refined_parts(
            response=json.dumps({"groups": [[True], [2], [3], [4]]}),
            fragments=fragments,
            original=PARAGRAPH,
        )

        self.assertIsNone(parts)
        self.assertEqual(reason, "invalid_response")

    def test_a_plan_without_order_keeps_the_given_group_order(self) -> None:
        fragments = _split_refine_fragments(PARAGRAPH)
        parts, reason = _assemble_refined_parts(
            response=_plan([[2], [1], [3], [4]]),
            fragments=fragments,
            original=PARAGRAPH,
        )

        self.assertEqual(reason, "")
        self.assertTrue((parts or [""])[0].startswith("NOTE:"))


class ParserIntegrationTests(unittest.TestCase):
    def test_the_parser_receives_only_verbatim_substrings(self) -> None:
        from parsecore.parsers import _refine_low_confidence_paragraphs

        client = _FakeClient(_plan([[1], [2, 3]]))
        refiner = LlmBoundaryRefiner(client, max_calls_per_doc=5).begin_request()
        refined = _refine_low_confidence_paragraphs(
            [GATED_PARAGRAPH], refiner=refiner, min_length=10, min_markers=1
        )

        self.assertEqual(len(refined), 2)
        compact_original = "".join(GATED_PARAGRAPH.split())
        for part in refined:
            # Every emitted paragraph is a piece of the original text.
            self.assertIn("".join(part.split()), compact_original)
        self.assertEqual(
            "".join("".join(part.split()) for part in refined), compact_original
        )

    def test_a_rewriting_model_changes_nothing_in_the_parser(self) -> None:
        from parsecore.parsers import _refine_low_confidence_paragraphs

        client = _FakeClient(
            "1| Apply 30 N.m of torque to the fastener.\n2| Then check the part."
        )
        refiner = LlmBoundaryRefiner(client, max_calls_per_doc=5).begin_request()
        refined = _refine_low_confidence_paragraphs(
            [GATED_PARAGRAPH], refiner=refiner, min_length=10, min_markers=1
        )

        self.assertEqual(refined, [GATED_PARAGRAPH])


if __name__ == "__main__":
    unittest.main()
