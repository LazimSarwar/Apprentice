"""Objects the reading leaves out are searched for, and a miss needs both witnesses to be a skip."""
import unittest
from unittest.mock import patch

from oneshot.check import compare
from oneshot.procedure import learn, with_confirmed_kinds, with_reference_objects
from oneshot.schema import Box, GraphMeta, ObjectWithEvidence, Relation, StepWithEvidence, TaskGraph, Track, VideoInfo
from web import live
from web.baseline import final_picture

BOX = [100, 100, 700, 700]
CUBE = [300, 300, 450, 450]
FINALS = [Relation(relation="inside", a="red_cube", b="yellow_box"),
          Relation(relation="inside", a="black_charger", b="yellow_box")]


def track(name, *spans):
    boxes = []
    for start, end, box in spans:
        t = start
        while t <= end + 1e-9:
            boxes.append(Box(frame=int(round(t * 4)), t_s=round(t, 2), box=list(box)))
            t += 0.25
    boxes.sort(key=lambda b: b.frame)
    return Track(concept=name, sam_object_id=name, first_s=boxes[0].t_s, last_s=boxes[-1].t_s,
                 visible_frames=len(boxes), boxes=boxes)


def obj(name, kind="solid", *spans, **extra):
    color, shape = name.split("_")
    return ObjectWithEvidence(id=name, color=color, shape=shape, kind=kind, tracking_description=f"{color} {shape}",
                              tracked=bool(spans), track=track(name, *spans) if spans else None, **extra)


def graph(objects, steps, finals=(), tracked=True):
    return TaskGraph(source="x", video=VideoInfo(width=1000, height=800, fps=4, frames=80, duration_s=20),
                     objects=list(objects), steps=list(steps), final_relations=list(finals),
                     meta=GraphMeta(model="test", track_seconds=1.0 if tracked else None))


CUBE_STEP = StepWithEvidence(order=1, start_s=1, end_s=3, action="place", object="red_cube", target="yellow_box", relation="inside")
CHARGER_STEP = StepWithEvidence(order=2, start_s=5, end_s=7, action="place", object="black_charger", target="yellow_box", relation="inside")
REF = graph([obj("yellow_box", "container"), obj("red_cube"), obj("black_charger"), obj("gray_block")],
            [CUBE_STEP, CHARGER_STEP], FINALS, tracked=False)
PROC = learn([REF])
BOX_TRACKED = ("yellow_box", "container", (0, 20, BOX))
CUBE_TRACKED = ("red_cube", "solid", (0, 2.75, [800, 700, 880, 780]), (3, 20, CUBE))


def reading_without_charger(*objects):
    return graph([obj(*BOX_TRACKED), obj(*CUBE_TRACKED), *objects], [CUBE_STEP],
                 [Relation(relation="inside", a="red_cube", b="yellow_box")])


class SearchListTests(unittest.TestCase):
    def test_left_out_procedure_object_is_added_for_the_tracker(self):
        att = graph([obj("yellow_box", "container"), obj("red_cube")], [CUBE_STEP], tracked=False)
        added = with_reference_objects(att, REF, PROC)
        charger = next(o for o in added.objects if o.id == "black_charger")
        self.assertTrue(charger.from_reference)
        self.assertIsNone(charger.track)
        self.assertEqual(charger.concept(), "black charger")
        self.assertEqual(added.steps, att.steps)
        self.assertEqual(len(att.objects), 2)

    def test_objects_the_procedure_does_not_use_are_not_searched_for(self):
        att = graph([obj("yellow_box", "container"), obj("red_cube")], [CUBE_STEP], tracked=False)
        self.assertNotIn("gray_block", [o.id for o in with_reference_objects(att, REF, PROC).objects])

    def test_nothing_is_added_when_every_object_was_read(self):
        att = graph(REF.objects, REF.steps, tracked=False)
        self.assertIs(with_reference_objects(att, REF, PROC), att)

    def test_renamed_object_counts_as_read(self):
        renamed = ObjectWithEvidence(id="power_adapter", color="black", shape="charger", kind="solid",
                                     tracking_description="black charger")
        att = graph([obj("yellow_box", "container"), obj("red_cube"), renamed], [CUBE_STEP], tracked=False)
        self.assertNotIn("black_charger", [o.id for o in with_reference_objects(att, REF, PROC).objects])

    def test_surface_is_not_searched_for(self):
        ref = graph([*REF.objects, obj("brown_table", "surface")],
                    [CUBE_STEP, CHARGER_STEP, StepWithEvidence(order=3, start_s=8, end_s=9, action="place", object="gray_block",
                                                               target="brown_table", relation="on")], FINALS, tracked=False)
        att = graph([obj("yellow_box", "container"), obj("red_cube"), obj("black_charger"), obj("gray_block")], ref.steps[:2], tracked=False)
        self.assertNotIn("brown_table", [o.id for o in with_reference_objects(att, ref, learn([ref])).objects])

    def test_live_tracking_searches_for_the_left_out_object(self):
        att = graph([obj("yellow_box", "container"), obj("red_cube")], [CUBE_STEP], tracked=False)
        seen = {}

        def fake(graph_in, _clip):
            seen["ids"] = [o.id for o in graph_in.objects]
            return graph_in.model_copy(update={"meta": graph_in.meta.model_copy(update={"track_seconds": 1.0})})
        with patch.object(live, "add_evidence", fake), patch.object(live, "overlay_frames", side_effect=OSError), \
                patch.object(live.iio, "immeta", return_value={"fps": 4}), patch.object(live, "_work") as work:
            work.return_value.__truediv__.return_value.__truediv__.return_value.parent.mkdir.return_value = None
            live.finalize("0" * 32, REF, att, PROC, "clip.mp4")
        self.assertIn("black_charger", seen["ids"])


class SkipTests(unittest.TestCase):
    def verdict(self, att):
        return compare(REF, att, procedure=PROC)

    def test_object_seen_elsewhere_is_a_clear_skip(self):
        charger = obj("black_charger", "solid", (0, 20, [800, 300, 900, 400]), from_reference=True)
        result = self.verdict(reading_without_charger(charger))
        self.assertEqual(result.verdict, "fail")
        self.assertTrue(any(i.kind == "skipped" for i in result.issues))

    def test_object_not_found_by_a_completed_search_goes_to_review_with_the_right_reason(self):
        result = self.verdict(reading_without_charger(obj("black_charger", from_reference=True)))
        self.assertEqual(result.verdict, "unsure")
        self.assertTrue(any("not found anywhere" in i.message for i in result.issues))
        self.assertFalse(any("hidden or out of view" in i.message for i in result.issues))

    def test_failed_search_is_not_described_as_not_found(self):
        att = reading_without_charger(obj("black_charger", from_reference=True))
        att.meta.tracking_errors["black_charger"] = "Tracking unavailable (TimeoutError)"
        result = self.verdict(att)
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(any("not found anywhere" in i.message for i in result.issues))

    def test_step_the_reading_missed_but_the_tracker_saw_goes_to_review_not_fail(self):
        charger = obj("black_charger", "solid", (0, 6.75, [800, 300, 900, 400]), (7, 20, [500, 500, 620, 620]), from_reference=True)
        result = self.verdict(reading_without_charger(charger))
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(any(i.kind == "skipped" for i in result.issues))
        self.assertTrue(any("does not report it" in i.message for i in result.issues))

    def test_same_guard_applies_when_the_reading_named_the_object(self):
        charger = obj("black_charger", "solid", (0, 6.75, [800, 300, 900, 400]), (7, 20, [500, 500, 620, 620]))
        self.assertEqual(self.verdict(reading_without_charger(charger)).verdict, "unsure")


class BaselineKindsTests(unittest.TestCase):
    def test_final_picture_uses_the_object_types_a_person_confirmed(self):
        ref = graph([obj("yellow_box", "container"), obj("white_liner", "surface"), obj("red_cube")],
                    [StepWithEvidence(order=1, start_s=1, end_s=2, action="place", object="white_liner", target="yellow_box", relation="inside"),
                     StepWithEvidence(order=2, start_s=3, end_s=4, action="place", object="red_cube", target="white_liner", relation="on")],
                    tracked=False)
        kinds = {"yellow_box": "container", "white_liner": "solid", "red_cube": "solid"}
        typed = with_confirmed_kinds(ref, learn([ref]))
        proc = learn([with_confirmed_kinds(ref.model_copy(deep=True), type("P", (), {"object_kinds": kinds})())])
        proc.object_kinds = kinds
        self.assertEqual(typed.objects[1].kind, "surface")
        self.assertEqual(with_confirmed_kinds(ref, proc).objects[1].kind, "solid")
        self.assertEqual(final_picture(ref, ref, proc), "pass")


if __name__ == "__main__":
    unittest.main()
