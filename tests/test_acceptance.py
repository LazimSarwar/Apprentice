"""Regressions for terminal placements, instance identity and persisted input."""
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from oneshot.check import compare
from oneshot.clients import ConfigurationError, require_env
from oneshot.evidence import TrackingBatch
from oneshot.graph import track_with_fallback
from oneshot.schema import Box, Object, ObjectWithEvidence, Relation, StepWithEvidence, TaskGraph
from oneshot.procedure import learn
from test_reliability import graph


def moved_attempt():
    att = graph(tracked=True).model_copy(deep=True)
    att.objects.append(ObjectWithEvidence(id="blue_tray", color="blue", shape="tray"))
    att.steps.append(StepWithEvidence(order=2, start_s=2.1, end_s=2.8, action="place",
        object="red_block", target="blue_tray", relation="inside"))
    att.objects[0].track.boxes.append(Box(frame=84, t_s=2.8, box=[150, 150, 160, 160]))
    att.final_relations = [Relation(relation="inside", a="red_block", b="blue_tray")]
    return att


def nested_placement():
    """A support inside a container, followed by an item placed on that support."""
    ref = graph().model_copy(deep=True)
    ref.objects.append(ObjectWithEvidence(id="white_sheet", color="white", shape="sheet"))
    ref.steps = [
        StepWithEvidence(order=1, start_s=0, end_s=1, action="place",
                         object="white_sheet", target="gray_tray", relation="inside"),
        StepWithEvidence(order=2, start_s=1.2, end_s=2, action="place",
                         object="red_block", target="white_sheet", relation="on"),
    ]
    ref.final_relations = [Relation(relation="inside", a="white_sheet", b="gray_tray"),
                           Relation(relation="on", a="red_block", b="white_sheet")]
    att = ref.model_copy(deep=True)
    att.steps[1].target = "gray_tray"
    att.steps[1].relation = "inside"
    att.final_relations[1] = Relation(relation="inside", a="red_block", b="gray_tray")
    return ref, att


class AcceptanceTests(unittest.TestCase):
    def test_coarse_container_description_requires_review_without_inventing_support(self):
        ref, att = nested_placement()
        result = compare(ref, att)
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)
        self.assertFalse(result.milestones[1]["done"])
        self.assertFalse(any(i.kind == "wrong_place" for i in result.issues))
        self.assertTrue(any("does not establish" in i.message for i in result.issues))

    def test_container_track_does_not_prove_or_disprove_support(self):
        ref, att = nested_placement()
        tracks = {o.id: o.track for o in graph(tracked=True).objects}
        for obj in att.objects:
            obj.track = tracks.get(obj.id)
        att.meta.track_seconds = 1
        result = compare(ref, att)
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.verified)
        self.assertTrue(any("white sheet" in i.message for i in result.issues))

    def test_coarse_later_placement_does_not_become_a_verified_final(self):
        ref, att = nested_placement()
        att.steps = [*ref.steps, att.steps[1].model_copy(update={"order": 3, "start_s": 2.1, "end_s": 2.5})]
        result = compare(ref, att)
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)
        self.assertTrue(any("Couldn't verify the final placement" in i.message for i in result.issues))

    def test_different_container_still_fails(self):
        ref, att = nested_placement()
        att.objects.append(ObjectWithEvidence(id="blue_tray", color="blue", shape="tray"))
        att.steps[1].target = "blue_tray"
        att.final_relations[1].b = "blue_tray"
        self.assertTrue(any(i.kind == "wrong_place" for i in compare(ref, att).issues))

    def test_reference_layout_cannot_supply_missing_attempt_support(self):
        ref, att = nested_placement()
        att.steps = att.steps[1:]
        att.final_relations = att.final_relations[1:]
        self.assertEqual(compare(ref, att).verdict, "fail")

    def test_future_support_placement_does_not_explain_an_earlier_move(self):
        ref, att = nested_placement()
        att.steps[0].start_s, att.steps[0].end_s = 2.2, 2.5
        att.steps[0].order, att.steps[1].order = 2, 1
        self.assertEqual(compare(ref, att).verdict, "fail")

    def test_explicit_support_preserves_required_order_detection(self):
        ref, _ = nested_placement()
        att = ref.model_copy(deep=True)
        att.steps[0].start_s, att.steps[0].end_s = 2.2, 2.5
        att.steps[0].order, att.steps[1].order = 2, 1
        self.assertTrue(any(i.kind == "order" and i.severity == "fail"
                            for i in compare(ref, att, procedure=learn([ref])).issues))

    def test_observed_support_inside_container_can_establish_containment(self):
        detailed, broad = nested_placement()
        result = compare(broad, detailed)
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.milestones[1]["time"], 2)
        self.assertFalse(result.verified)

    def test_inferred_containment_still_needs_tracking_evidence(self):
        detailed, broad = nested_placement()
        detailed.meta.track_seconds = 1
        self.assertEqual(compare(broad, detailed).verdict, "unsure")

    def test_support_moved_out_of_container_invalidates_terminal_containment(self):
        detailed, broad = nested_placement()
        detailed.steps.append(StepWithEvidence(order=3, start_s=2.1, end_s=2.5,
            action="place", object="white_sheet", target=None, relation="on"))
        detailed.final_relations = []
        result = compare(broad, detailed)
        self.assertEqual(result.verdict, "fail")
        self.assertTrue(any(i.kind == "wrong_place" for i in result.issues))

    def test_placement_on_container_does_not_imply_inside_it(self):
        from oneshot.check import _inside_container
        self.assertFalse(_inside_container(("on", "item", "box"), "box", {}))
        self.assertFalse(_inside_container(("on", "item", "sheet"), "box",
            {"sheet": ("on", "sheet", "box")}))
        self.assertFalse(_inside_container(("on", "item", "sheet"), "box",
            {"sheet": ("on", "sheet", "other"), "other": ("on", "other", "sheet")}))

    def test_correct_then_wrong_is_never_verified_or_accepted(self):
        report = compare(graph(), moved_attempt())
        self.assertFalse(report.passed)
        self.assertFalse(report.verified)
        self.assertTrue(any("Final placement changed" in i.message for i in report.issues))

    def test_untracked_correct_then_wrong_fails(self):
        att = moved_attempt()
        att.meta.track_seconds = None
        for obj in att.objects: obj.track = None
        report = compare(graph(), att)
        self.assertEqual(report.verdict, "fail")
        self.assertFalse(report.verified)

    def test_wrong_then_correct_can_pass_with_notes(self):
        att = moved_attempt()
        att.steps[0] = att.steps[0].model_copy(update={"target": "blue_tray"})
        att.steps[1] = att.steps[1].model_copy(update={"target": "gray_tray"})
        att.objects[0].track.boxes[-1].box = [20, 20, 30, 30]
        att.final_relations = [Relation(relation="inside", a="red_block", b="gray_tray")]
        self.assertTrue(compare(graph(), att).passed)

    def test_expected_intermediate_placement_is_not_a_required_final(self):
        ref = moved_attempt()
        # Intermediate reference milestones remain meaningful without becoming permanent goals.
        self.assertTrue(compare(ref, ref).passed)

    def test_partial_reads_do_not_enforce_terminal_goals(self):
        att = moved_attempt()
        self.assertFalse(compare(graph(), att, final=False).final_states)

    def test_identical_descriptions_do_not_share_a_tracker_request(self):
        objs = [Object(id=oid, color="red", shape="block") for oid in ("red_left", "red_right")]
        with patch("oneshot.graph.track_objects", return_value=TrackingBatch()) as tracker:
            batch = track_with_fallback(Path("unused"), objs, 30)
        self.assertEqual(tracker.call_args.args[1], [])
        self.assertEqual(set(batch.errors), {"red_left", "red_right"})
        self.assertTrue(all(t is None for t in batch.tracks.values()))

    def test_persisted_duplicate_trajectory_cannot_corroborate_two_objects(self):
        att = graph(tracked=True).model_copy(deep=True)
        att.objects[1].track = att.objects[0].track.model_copy(deep=True)
        report = compare(graph(), att)
        self.assertEqual(report.verdict, "unsure")
        self.assertFalse(report.verified)

    def test_saved_graphs_validate_ids_references_orders_and_times(self):
        for mutate in (
            lambda d: d["objects"].append(d["objects"][0]),
            lambda d: d["steps"][0].update(target="unknown"),
            lambda d: d["steps"][0].update(target="red_block"),
            lambda d: d["steps"][0].update(end_s=99),
            lambda d: d["steps"][0].update(start_s=float("nan")),
            lambda d: d["steps"].append(d["steps"][0]),
        ):
            data = graph().model_dump(); mutate(data)
            with self.assertRaises(ValidationError): TaskGraph.model_validate(data)

    def test_bad_saved_boxes_are_rejected(self):
        for bad in ([0, 0, 2], [2, 0, 1, 2], [0, 0, float("inf"), 2]):
            with self.assertRaises(ValidationError): Box(frame=0, t_s=0, box=bad)

    def test_confirmed_object_kind_overrides_ambiguous_model_name(self):
        ref=graph();proc=__import__('oneshot.procedure',fromlist=['learn']).learn([ref])
        proc.object_kinds={'gray_tray':'solid'}
        proc.steps[0].key=('on','red_block','gray_tray')
        proc.final_goals=[proc.steps[0].key]
        report=compare(ref,graph(),procedure=proc)
        self.assertTrue(report.passed)

    def test_missing_configuration_is_a_catchable_job_error(self):
        with patch.dict("os.environ", {"MODEL_API_KEY": ""}), self.assertRaises(ConfigurationError):
            require_env("MODEL_API_KEY")
