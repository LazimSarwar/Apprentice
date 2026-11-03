"""A required earlier step must still hold at the moment the later step happens."""
import unittest

from oneshot.check import compare, holds_now
from oneshot.procedure import learn, pair_id
from oneshot.schema import GraphMeta, ObjectWithEvidence, Relation, StepWithEvidence, TaskGraph, VideoInfo
from oneshot.watch import Watch

LINER = ("inside", "white_liner", "yellow_box")
CUBE = ("on", "red_cube", "white_liner")
CHARGER = ("inside", "black_charger", "yellow_box")
FINALS = [Relation(relation="inside", a="white_liner", b="yellow_box"),
          Relation(relation="on", a="red_cube", b="white_liner"),
          Relation(relation="inside", a="black_charger", b="yellow_box")]


def objects():
    return [ObjectWithEvidence(id="yellow_box", color="yellow", shape="box", kind="container"),
            ObjectWithEvidence(id="white_liner", color="white", shape="sheet", kind="solid"),
            ObjectWithEvidence(id="red_cube", color="red", shape="block", kind="solid"),
            ObjectWithEvidence(id="black_charger", color="black", shape="charger", kind="solid"),
            ObjectWithEvidence(id="brown_table", color="brown", shape="table", kind="surface")]


def step(order, end, obj, target=None, relation=None, action="place"):
    return StepWithEvidence(order=order, start_s=end - 0.5, end_s=end, action=action, object=obj,
                            target=target, relation=relation)


def task(steps, finals=FINALS, extra=()):
    return TaskGraph(source="x", video=VideoInfo(width=640, height=480, fps=10, frames=200, duration_s=20),
                     objects=objects() + list(extra), steps=steps, final_relations=finals, meta=GraphMeta(model="test"))


REFERENCE = task([step(1, 2, "white_liner", "yellow_box", "inside"),
                  step(2, 4, "red_cube", "white_liner", "on"),
                  step(3, 6, "black_charger", "yellow_box", "inside")])


def procedure(strength="required"):
    return learn([REFERENCE]).with_overrides({pair_id(LINER, CHARGER): strength})


def liner_out_then_charger(removal):
    return task([step(1, 2, "white_liner", "yellow_box", "inside"), removal,
                 step(3, 5, "black_charger", "yellow_box", "inside"),
                 step(4, 7, "white_liner", "yellow_box", "inside"),
                 step(5, 9, "red_cube", "white_liner", "on")])


class OrderAtPlacementTests(unittest.TestCase):
    def test_required_step_undone_before_the_later_step_fails(self):
        att = liner_out_then_charger(step(2, 3, "white_liner", "brown_table", "on"))
        result = compare(REFERENCE, att, procedure=procedure())
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.passed)
        issue = next(i for i in result.issues if i.kind == "order" and i.severity == "fail")
        self.assertIn("no longer in place", issue.message)
        self.assertEqual(issue.time, 5)

    def test_pick_up_with_no_destination_counts_as_undone(self):
        att = liner_out_then_charger(step(2, 3, "white_liner", action="pick"))
        self.assertEqual(compare(REFERENCE, att, procedure=procedure()).verdict, "fail")

    def test_target_less_put_down_counts_as_undone(self):
        att = liner_out_then_charger(step(2, 3, "white_liner"))
        self.assertEqual(compare(REFERENCE, att, procedure=procedure()).verdict, "fail")

    def test_same_final_picture_followed_correctly_passes(self):
        self.assertEqual(compare(REFERENCE, REFERENCE, procedure=procedure()).verdict, "pass")

    def test_removed_and_restored_before_the_later_step_does_not_fail(self):
        att = task([step(1, 2, "white_liner", "yellow_box", "inside"),
                    step(2, 3, "white_liner", "brown_table", "on"),
                    step(3, 4, "white_liner", "yellow_box", "inside"),
                    step(4, 6, "red_cube", "white_liner", "on"),
                    step(5, 8, "black_charger", "yellow_box", "inside")])
        result = compare(REFERENCE, att, procedure=procedure())
        self.assertTrue(result.passed)
        self.assertFalse(any("no longer in place" in i.message for i in result.issues))

    def test_order_that_does_not_matter_is_not_enforced_at_the_moment(self):
        att = liner_out_then_charger(step(2, 3, "white_liner", "brown_table", "on"))
        result = compare(REFERENCE, att, procedure=procedure("free"))
        self.assertFalse(any("no longer in place" in i.message for i in result.issues))

    def test_less_specific_reading_of_the_same_placement_is_not_a_removal(self):
        # The model later describes the cube only as "in the box"; nothing says it left the liner.
        card = [ObjectWithEvidence(id="blue_card", color="blue", shape="card", kind="solid")]
        ref = task(REFERENCE.steps + [step(4, 8, "blue_card", "red_cube", "on")], extra=card)
        att = task(REFERENCE.steps + [step(4, 7, "red_cube", "yellow_box", "inside"),
                                      step(5, 9, "blue_card", "red_cube", "on")], extra=card)
        result = compare(ref, att, procedure=learn([ref]))
        self.assertFalse(any("no longer in place" in i.message for i in result.issues))

    def test_step_the_reference_itself_moved_on_from_is_not_required_to_stay(self):
        ref = task([step(1, 2, "red_cube", "brown_table", "on"),
                    step(2, 4, "red_cube", "white_liner", "on"),
                    step(3, 6, "black_charger", "yellow_box", "inside")],
                   [Relation(relation="on", a="red_cube", b="white_liner"),
                    Relation(relation="inside", a="black_charger", b="yellow_box")])
        table = ("on", "red_cube", "surface")
        proc = learn([ref]).with_overrides({pair_id(table, CHARGER): "required"})
        result = compare(ref, ref, procedure=proc)
        self.assertFalse(any("no longer in place" in i.message for i in result.issues))
        self.assertTrue(result.passed)

    def test_near_live_warning_uses_the_same_rule(self):
        watch = Watch(REFERENCE, procedure())
        watch.update(liner_out_then_charger(step(2, 3, "white_liner", "brown_table", "on")), 6)
        self.assertTrue(any("no longer in place" in a["message"] for a in watch.view()["alerts"]))

    def test_holds_now_is_false_for_an_object_in_the_hand(self):
        self.assertFalse(holds_now(LINER, {"white_liner": None}))
        self.assertTrue(holds_now(LINER, {"white_liner": LINER}))


if __name__ == "__main__":
    unittest.main()
