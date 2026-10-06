"""Deterministic tests for the study-only Tine perception fusion harness.

These tests are offline: no kwin-mcp session, no OCR runtime and no provider
calls. They cover provenance, filtering, deduplication, duplicate-label
ambiguity, coordinate disagreement, hidden/disabled rejection and
generation-scoped refs.
"""

import json
import unittest

from scripts.tine_perception import (
    AtspiElement,
    Bounds,
    CandidateInventory,
    KIND_OCR_TEXT,
    OcrDetection,
    SOURCE_ATSPI,
    SOURCE_OCR,
    StaleReferenceError,
    bounds_reason,
    elements_from_find,
    filter_atspi,
    filter_ocr,
    fuse,
    normalize_text,
    pair_group,
    selector_state,
)

WINDOW = Bounds(0, 0, 1280, 800)
VISIBLE_ENABLED = frozenset({"enabled", "sensitive", "showing", "visible"})


def button(role="button", label="Save", bounds=(100, 100, 80, 30), states=VISIBLE_ENABLED,
           actions=("Press",), value=None):
    return AtspiElement(role=role, label=label, states=states, actions=tuple(actions),
                        bounds=Bounds(*bounds), value=value)


def detection(text="Save", bounds=(110, 105, 60, 20), confidence=0.95):
    return OcrDetection(text=text, bounds=Bounds(*bounds), confidence=confidence)


class NormalizeTests(unittest.TestCase):
    def test_normalization_matches_tine_behaviour(self):
        self.assertEqual(normalize_text("Log In"), "login")
        self.assertEqual(normalize_text("Continue with Google"), "continuewithgoogle")
        self.assertEqual(normalize_text(None), "")
        self.assertEqual(normalize_text(" 12:34 "), "1234")
        # PP-OCR can return fullwidth digits; NFKC folding keeps them fusible.
        self.assertEqual(normalize_text("１２３"), "123")


class ProvenanceAndFusionTests(unittest.TestCase):
    def test_matching_label_overlapping_bounds_fuses_to_one_candidate(self):
        result = fuse([button()], [detection()], window=WINDOW)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.sources, (SOURCE_ATSPI, SOURCE_OCR))
        # AT-SPI role, states and actions stay the semantics.
        self.assertEqual(candidate.kind, "button")
        self.assertEqual(candidate.actions, ("Press",))
        self.assertEqual(candidate.atspi_role, "button")
        self.assertIn("sensitive", candidate.atspi_states)
        self.assertEqual(candidate.confidence, 0.95)
        self.assertIsNone(candidate.disagreement)
        self.assertEqual(
            {source: bounds.as_dict() for source, bounds in candidate.bounds_by_source.items()},
            {SOURCE_ATSPI: {"x": 100, "y": 100, "w": 80, "h": 30},
             SOURCE_OCR: {"x": 110, "y": 105, "w": 60, "h": 20}},
        )
        self.assertEqual(result.counts["fused"], 1)
        self.assertEqual(result.counts["ocr_only"], 0)

    def test_ocr_text_without_semantic_counterpart_is_not_a_control(self):
        result = fuse([button(label="Cancel")], [detection(text="Total 42")], window=WINDOW)
        ocr_only = [candidate for candidate in result.candidates
                    if candidate.sources == (SOURCE_OCR,)]
        self.assertEqual(len(ocr_only), 1)
        self.assertEqual(ocr_only[0].kind, KIND_OCR_TEXT)
        self.assertEqual(ocr_only[0].actions, ())
        self.assertIsNone(ocr_only[0].atspi_role)
        self.assertNotIn(ocr_only[0].candidate_id, [c.candidate_id for c in result.candidates if c.kind == "button"])

    def test_wide_container_containing_the_ocr_center_is_corroborated(self):
        # An AT-SPI container can be much wider than the OCR text run; a centre
        # outside the tolerance but inside the bounds is still corroboration.
        container = button(label="Welcome", bounds=(421, 236, 483, 30))
        text_run = detection(text="Welcome", bounds=(429, 242, 112, 24))
        result = fuse([container], [text_run], window=WINDOW)
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.counts["fused"], 1)

    def test_selector_state_has_no_coordinates_or_values(self):
        result = fuse([button()], [detection()], window=WINDOW)
        state = selector_state(result.candidates)
        blob = json.dumps(state)
        self.assertNotIn("bbox", blob)
        self.assertNotIn('"x"', blob)
        option = state["options"][0]
        self.assertEqual(set(option), {"id", "label", "kind", "actions", "sources",
                                       "confidence", "disagreement"})


class DeduplicationTests(unittest.TestCase):
    def test_atspi_wins_over_duplicate_ocr_text(self):
        result = fuse([button(label="Save")], [detection(text="Save")], window=WINDOW)
        labels = [candidate.label for candidate in result.candidates
                  if candidate.kind == KIND_OCR_TEXT]
        self.assertEqual(labels, [])
        self.assertEqual(result.counts["fused"], 1)

    def test_ocr_kept_when_no_atspi_label_matches(self):
        result = fuse([button(label="Cancel")], [detection(text="Save")], window=WINDOW)
        self.assertEqual(result.counts["fused"], 0)
        self.assertEqual(result.counts["ocr_only"], 1)
        self.assertEqual(result.counts["atspi_only"], 1)


class DuplicateLabelAmbiguityTests(unittest.TestCase):
    def test_count_mismatch_reports_unknown_and_preserves_both_sides(self):
        refs = [button(label="Add", bounds=(100, 100, 40, 20)),
                button(label="Add", bounds=(100, 200, 40, 20))]
        dets = [detection(text="Add", bounds=(105, 105, 20, 12))]
        result = fuse(refs, dets, window=WINDOW)
        self.assertEqual(result.ambiguous_labels, ("add",))
        self.assertEqual(len(result.conflicts), 0)
        agreements = {candidate.disagreement for candidate in result.candidates}
        self.assertEqual(agreements, {"ambiguous_pairing"})
        sources = sorted(source for candidate in result.candidates for source in candidate.sources)
        self.assertEqual(sources, [SOURCE_ATSPI, SOURCE_ATSPI, SOURCE_OCR])

    def test_spatial_tie_is_ambiguous_rather_than_guessed(self):
        refs = [button(label="Go", bounds=(0, 0, 20, 20)),
                button(label="Go", bounds=(100, 100, 20, 20))]
        dets = [detection(text="Go", bounds=(100, 0, 20, 20)),
                detection(text="Go", bounds=(0, 100, 20, 20))]
        result = fuse(refs, dets, window=WINDOW)
        self.assertEqual(result.ambiguous_labels, ("go",))
        self.assertEqual(len(result.candidates), 4)

    def test_unique_spatial_order_pairs_and_fuses(self):
        refs = [button(label="Go", bounds=(0, 0, 20, 20)),
                button(label="Go", bounds=(0, 100, 20, 20))]
        dets = [detection(text="Go", bounds=(0, 2, 20, 20)),
                detection(text="Go", bounds=(0, 102, 20, 20))]
        result = fuse(refs, dets, window=WINDOW)
        self.assertEqual(result.ambiguous_labels, ())
        self.assertEqual(result.counts["fused"], 2)

    def test_pair_group_returns_none_for_single_empty_side(self):
        self.assertIsNone(pair_group([], [detection()]))
        self.assertIsNone(pair_group([button()], []))


class CoordinateDisagreementTests(unittest.TestCase):
    def test_far_apart_bounds_stay_a_conflict_with_both_sources(self):
        result = fuse([button(label="Save", bounds=(10, 10, 40, 20))],
                      [detection(text="Save", bounds=(200, 200, 40, 20), confidence=0.88)],
                      window=WINDOW)
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual(conflict.atspi_bounds.as_dict(), {"x": 10, "y": 10, "w": 40, "h": 20})
        self.assertEqual(conflict.ocr_bounds.as_dict(), {"x": 200, "y": 200, "w": 40, "h": 20})
        self.assertEqual(conflict.ocr_confidence, 0.88)
        self.assertGreater(conflict.distance, 20)
        disagreements = {candidate.disagreement for candidate in result.candidates}
        self.assertEqual(disagreements, {"coordinate_conflict"})
        # Neither coordinate is silently chosen: both are preserved.
        by_source = {tuple(sorted(candidate.sources)): candidate for candidate in result.candidates}
        self.assertIn((SOURCE_ATSPI,), by_source)
        self.assertIn((SOURCE_OCR,), by_source)


class RejectionTests(unittest.TestCase):
    def test_hidden_state_is_rejected(self):
        hidden = button(states=frozenset({"enabled", "sensitive"}))
        usable, rejections, _ = filter_atspi([hidden], window=WINDOW)
        self.assertEqual(usable, [])
        self.assertIn("hidden", [rejection.reason for rejection in rejections])

    def test_disabled_state_is_rejected(self):
        disabled = button(states=frozenset({"enabled", "showing", "visible"}))
        usable, rejections, _ = filter_atspi([disabled], window=WINDOW)
        self.assertEqual(usable, [])
        self.assertIn("disabled", [rejection.reason for rejection in rejections])

    def test_int_min_extents_reported_as_hidden(self):
        element = button(bounds=(-2147483648, 5, 10, 10),
                         states=frozenset({"enabled", "sensitive", "showing", "visible"}))
        usable, rejections, _ = filter_atspi([element], window=WINDOW)
        self.assertEqual(usable, [])
        self.assertEqual(rejections[0].reason, "hidden_no_extents")

    def test_negative_or_zero_dimensions_are_malformed(self):
        self.assertEqual(bounds_reason(Bounds(0, 0, -5, 10)), "malformed_bounds")
        self.assertEqual(bounds_reason(Bounds(0, 0, 10, 0)), "malformed_bounds")
        self.assertIsNone(bounds_reason(Bounds(0, 0, 10, 10)))

    def test_out_of_window_rejected_for_both_sources(self):
        element = button(bounds=(1270, 100, 80, 30))
        usable, rejections, _ = filter_atspi([element], window=WINDOW)
        self.assertEqual(usable, [])
        self.assertIn("out_of_window", [rejection.reason for rejection in rejections])
        _, ocr_rejections = filter_ocr([detection(bounds=(1270, 100, 60, 20))], window=WINDOW)
        self.assertIn("out_of_window", [rejection.reason for rejection in ocr_rejections])

    def test_unnamed_filler_is_not_semantic(self):
        filler = AtspiElement(role="filler", label="", states=VISIBLE_ENABLED,
                              actions=(), bounds=Bounds(1, 1, 100, 100))
        usable, rejections, _ = filter_atspi([filler], window=WINDOW)
        self.assertEqual(usable, [])
        self.assertEqual(rejections[0].reason, "not_semantic")

    def test_low_confidence_and_empty_ocr_text_rejected(self):
        usable, rejections = filter_ocr(
            [detection(confidence=0.5), detection(text="   ")], window=WINDOW)
        self.assertEqual(usable, [])
        reasons = [rejection.reason for rejection in rejections]
        self.assertIn("low_confidence", reasons)
        self.assertIn("empty_text", reasons)

    def test_non_usable_elements_never_become_candidates(self):
        result = fuse(
            [button(label="Hidden", states=frozenset({"showing", "visible"})),
             button(label="Ok", bounds=(1, 1, 20, 20))],
            [], window=WINDOW)
        labels = [candidate.label for candidate in result.candidates]
        self.assertEqual(labels, ["Ok"])

    def test_elements_from_find_applies_offset(self):
        elements = elements_from_find([{
            "role": "button", "name": "One", "states": ["enabled", "sensitive", "showing", "visible"],
            "actions": ["Press"], "x": 10, "y": 20, "width": 30, "height": 10,
        }], offset=(100, 50))
        self.assertEqual(elements[0].bounds.as_dict(), {"x": 110, "y": 70, "w": 30, "h": 10})


class PrivateValueTests(unittest.TestCase):
    SENTINEL = "SENTINEL-42"

    def test_editable_value_is_scrubbed_and_matching_ocr_is_dropped(self):
        field = AtspiElement(
            role="text", label="Amount",
            states=frozenset({"editable", "enabled", "sensitive", "showing", "visible"}),
            actions=("SetFocus",), bounds=Bounds(10, 10, 120, 24), value=self.SENTINEL)
        result = fuse([field], [detection(text=self.SENTINEL, bounds=(12, 12, 60, 12))],
                      window=WINDOW)
        blob = json.dumps({
            "candidates": [candidate.label for candidate in result.candidates],
            "selector_state": selector_state(result.candidates),
        })
        self.assertNotIn("SENTINEL", blob)
        self.assertNotIn("sentinel", blob)
        self.assertEqual(result.private_value_count, 1)
        self.assertIn("private_value", [rejection.reason for rejection in result.rejections])
        # No OCR candidate survives with the private text.
        self.assertEqual([candidate for candidate in result.candidates
                          if candidate.sources == (SOURCE_OCR,)], [])

    def test_value_is_cleared_on_the_returned_element(self):
        field = AtspiElement(
            role="text", label="Amount",
            states=frozenset({"editable", "enabled", "sensitive", "showing", "visible"}),
            actions=(), bounds=Bounds(10, 10, 120, 24), value=self.SENTINEL)
        usable, _, private = filter_atspi([field], window=WINDOW)
        self.assertIsNone(usable[0].value)
        self.assertEqual(private, {"sentinel42"})


class GenerationScopedRefTests(unittest.TestCase):
    def test_refresh_invalidates_previous_generation_refs(self):
        inventory = CandidateInventory()
        first = inventory.refresh([button(label="Save")], [], window=WINDOW)
        ref = first.candidates[0].candidate_id
        self.assertIsNotNone(inventory.resolve(ref))
        second = inventory.refresh([button(label="Cancel")], [], window=WINDOW)
        self.assertEqual(second.generation, 2)
        self.assertIsNone(inventory.resolve(ref))
        with self.assertRaises(StaleReferenceError):
            inventory.select(ref)
        self.assertEqual(inventory.select(second.candidates[0].candidate_id).label, "Cancel")

    def test_selected_ref_still_resolves_to_the_same_element_after_refresh(self):
        inventory = CandidateInventory()
        inventory.refresh([button(label="Save", bounds=(1, 2, 10, 10))], [], window=WINDOW)
        result = inventory.refresh([button(label="Save", bounds=(1, 2, 10, 10))], [], window=WINDOW)
        candidate = inventory.select(result.candidates[0].candidate_id)
        self.assertEqual(candidate.bounds_by_source[SOURCE_ATSPI].as_dict(),
                         {"x": 1, "y": 2, "w": 10, "h": 10})




if __name__ == "__main__":
    unittest.main()
