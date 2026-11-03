"""The tracker checks motion, order and covered items independently of the video reading."""
import unittest

from oneshot.check import arrival_time, compare, never_moved
from oneshot.procedure import learn, pair_id
from oneshot.schema import Box, GraphMeta, ObjectWithEvidence, Relation, StepWithEvidence, TaskGraph, Track, VideoInfo

DURATION = 20.0
BOX = [100, 100, 700, 700]
LINER = [150, 150, 650, 650]
CUBE = [300, 300, 450, 450]
CHARGER = [500, 500, 620, 620]
CARD = [290, 290, 460, 460]
OFF = [800, 700, 880, 780]
LINER_KEY = ("inside", "white_liner", "yellow_box")
CHARGER_KEY = ("inside", "black_charger", "yellow_box")


def track(name, *spans):
    """Each span is (start, end, box), sampled every quarter second."""
    boxes = []
    for start, end, box in spans:
        t = start
        while t <= end + 1e-9:
            boxes.append(Box(frame=int(round(t * 4)), t_s=round(t, 2), box=list(box)))
            t += 0.25
    boxes.sort(key=lambda b: b.frame)
    return Track(concept=name, sam_object_id=name, first_s=boxes[0].t_s, last_s=boxes[-1].t_s,
                 visible_frames=len(boxes), boxes=boxes)


def obj(name, kind, *spans):
    color, shape = name.split("_")
    return ObjectWithEvidence(id=name, color=color, shape=shape, kind=kind, tracking_description=f"{color} {shape}",
                              tracked=bool(spans), track=track(name, *spans) if spans else None)


def step(order, start, end, name, target, relation):
    return StepWithEvidence(order=order, start_s=start, end_s=end, action="place", object=name, target=target, relation=relation)


def graph(objects, steps, finals, tracked=True):
    return TaskGraph(source="x", video=VideoInfo(width=1000, height=800, fps=4, frames=80, duration_s=DURATION),
                     objects=objects, steps=steps,
                     final_relations=[Relation(relation=r, a=a, b=b) for r, a, b in finals],
                     meta=GraphMeta(model="test", track_seconds=1.0 if tracked else None))


FINALS = [("inside", "white_liner", "yellow_box"), ("on", "red_cube", "white_liner"),
          ("inside", "black_charger", "yellow_box"), ("on", "blue_card", "red_cube")]
STEPS = [step(1, 1, 3, "white_liner", "yellow_box", "inside"), step(2, 4, 6, "red_cube", "white_liner", "on"),
         step(3, 7, 9, "black_charger", "yellow_box", "inside"), step(4, 10, 12, "blue_card", "red_cube", "on")]


def objects(**over):
    made = dict(box=obj("yellow_box", "container", (0, DURATION, BOX)),
                liner=obj("white_liner", "solid", (0, 2.75, OFF), (3, DURATION, LINER)),
                cube=obj("red_cube", "solid", (0, 5.75, OFF), (6, DURATION, CUBE)),
                charger=obj("black_charger", "solid", (0, 8.75, OFF), (9, DURATION, CHARGER)),
                card=obj("blue_card", "solid", (0, 11.75, OFF), (12, DURATION, CARD)))
    made.update(over)
    return list(made.values())


REFERENCE = graph(objects(), STEPS, FINALS, tracked=False)
PROCEDURE = learn([REFERENCE]).with_overrides({pair_id(LINER_KEY, CHARGER_KEY): "required"})


def check(att):
    return compare(REFERENCE, att, procedure=PROCEDURE)


class ArrivalTests(unittest.TestCase):
    def test_arrival_is_when_the_object_reaches_its_resting_place(self):
        self.assertEqual(arrival_time(track("x", (0, 4.75, OFF), (5, DURATION, CUBE)), DURATION), 5)

    def test_object_there_from_the_start_shows_no_arrival(self):
        self.assertIsNone(arrival_time(track("x", (0, DURATION, CUBE)), DURATION))

    def test_track_lost_well_before_the_end_gives_no_arrival(self):
        self.assertIsNone(arrival_time(track("x", (0, 3, OFF), (3.25, 9, CUBE)), DURATION))

    def test_partly_covered_sliver_is_not_read_as_the_object_moving(self):
        sliver = [440, 300, 450, 310]
        self.assertEqual(arrival_time(track("x", (1, 1.75, OFF), (2, 8, CUBE), (8.25, DURATION, sliver)), DURATION), 2)

    def test_never_moved_needs_boxes_both_sides_of_the_step(self):
        still = track("x", (0, DURATION, CUBE))
        self.assertTrue(never_moved(still, STEPS[1]))
        self.assertFalse(never_moved(track("x", (4.5, DURATION, CUBE)), STEPS[1]))
        self.assertFalse(never_moved(track("x", (0, 3, OFF), (6, DURATION, CUBE)), STEPS[1]))


class MotionTests(unittest.TestCase):
    def test_step_reported_for_an_object_that_never_moved_goes_to_review(self):
        result = check(graph(objects(cube=obj("red_cube", "solid", (0, DURATION, CUBE))), STEPS, FINALS))
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)
        self.assertTrue(any("staying in one place" in i.message for i in result.issues))
        self.assertEqual(next(m for m in result.milestones if "cube" in m["text"])["evidence"], "still")

    def test_correct_attempt_with_visible_moves_passes(self):
        result = check(graph(objects(), STEPS, FINALS))
        self.assertEqual(result.verdict, "pass")
        self.assertFalse(any(m["evidence"] == "still" for m in result.milestones))


class OrderTests(unittest.TestCase):
    def rule(self, result):
        return next(c for c in result.order_checks if c["key_before"] == list(LINER_KEY) and c["key_after"] == list(CHARGER_KEY))

    def test_tracker_agrees_with_a_correct_order(self):
        result = check(graph(objects(), STEPS, FINALS))
        self.assertEqual(self.rule(result)["tracker"], "agrees")

    def test_reading_says_right_order_but_tracker_shows_wrong_goes_to_review(self):
        moved = objects(liner=obj("white_liner", "solid", (0, 9.75, OFF), (10, DURATION, LINER)),
                        charger=obj("black_charger", "solid", (0, 1.75, OFF), (2, DURATION, CHARGER)))
        result = check(graph(moved, STEPS, FINALS))
        self.assertEqual(self.rule(result)["tracker"], "conflicts")
        self.assertEqual(result.verdict, "unsure")
        self.assertTrue(any("disagree about whether" in i.message for i in result.issues))

    def test_reading_misses_a_removal_that_the_tracker_shows(self):
        # Liner in, out again, charger in, liner back. The reading reports only the end result.
        moved = objects(liner=obj("white_liner", "solid", (0, 2.75, OFF), (3, 4.75, LINER), (5, 7.75, OFF), (8, DURATION, LINER)),
                        charger=obj("black_charger", "solid", (0, 5.75, OFF), (6, DURATION, CHARGER)))
        result = check(graph(moved, STEPS, FINALS))
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)

    def test_reading_says_wrong_order_but_tracker_shows_right_goes_to_review_not_fail(self):
        late_first = [step(1, 1, 3, "black_charger", "yellow_box", "inside"), step(2, 4, 6, "white_liner", "yellow_box", "inside"),
                      step(3, 7, 9, "red_cube", "white_liner", "on"), step(4, 10, 12, "blue_card", "red_cube", "on")]
        result = check(graph(objects(), late_first, FINALS))
        self.assertEqual(self.rule(result)["tracker"], "conflicts")
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(any(i.kind == "order" and i.severity == "fail" for i in result.issues))

    def test_reading_and_tracker_both_show_wrong_order_fails(self):
        late_first = [step(1, 1, 3, "black_charger", "yellow_box", "inside"), step(2, 4, 6, "white_liner", "yellow_box", "inside"),
                      step(3, 7, 9, "red_cube", "white_liner", "on"), step(4, 10, 12, "blue_card", "red_cube", "on")]
        moved = objects(charger=obj("black_charger", "solid", (0, 2.75, OFF), (3, DURATION, CHARGER)),
                        liner=obj("white_liner", "solid", (0, 5.75, OFF), (6, DURATION, LINER)),
                        cube=obj("red_cube", "solid", (0, 8.75, OFF), (9, DURATION, CUBE)))
        result = check(graph(moved, late_first, FINALS))
        self.assertEqual(self.rule(result)["tracker"], "agrees")
        self.assertEqual(result.verdict, "fail")

    def test_tracker_too_close_or_unknown_never_changes_the_verdict(self):
        close = objects(charger=obj("black_charger", "solid", (0, 2.75, OFF), (3, DURATION, CHARGER)))
        self.assertEqual(self.rule(check(graph(close, STEPS, FINALS)))["tracker"], "too_close")
        self.assertEqual(check(graph(close, STEPS, FINALS)).verdict, "pass")
        hidden = objects(charger=obj("black_charger", "solid", (0, 8.75, OFF), (9, 14, CHARGER)))
        self.assertEqual(self.rule(check(graph(hidden, STEPS, FINALS)))["tracker"], "unknown")

    def test_no_tracking_means_no_order_checks(self):
        untracked = [obj(name, kind) for name, kind in (("yellow_box", "container"), ("white_liner", "solid"),
                     ("red_cube", "solid"), ("black_charger", "solid"), ("blue_card", "solid"))]
        self.assertEqual(check(graph(untracked, STEPS, FINALS, tracked=False)).order_checks, [])


class CoveredItemTests(unittest.TestCase):
    def cube_hidden(self, last=13.0):
        return obj("red_cube", "solid", (0, 5.75, OFF), (6, last, CUBE))

    def cube_state(self, result):
        return next(s for s in result.final_states if s["key"][1] == "red_cube")

    def test_item_hidden_by_the_required_step_on_top_of_it_passes(self):
        result = check(graph(objects(cube=self.cube_hidden()), STEPS, FINALS))
        self.assertEqual(self.cube_state(result)["evidence"], "covered")
        self.assertEqual(result.verdict, "pass")
        self.assertTrue(result.passed)

    def test_sliver_left_while_being_covered_still_counts(self):
        cube = obj("red_cube", "solid", (0, 5.75, OFF), (6, 12.5, CUBE), (12.75, 15, [445, 300, 450, 320]))
        self.assertEqual(self.cube_state(check(graph(objects(cube=cube), STEPS, FINALS)))["evidence"], "covered")

    def test_item_that_vanished_before_anything_covered_it_needs_review(self):
        result = check(graph(objects(cube=self.cube_hidden(last=8.0)), STEPS, FINALS))
        self.assertNotEqual(self.cube_state(result)["evidence"], "covered")
        self.assertEqual(result.verdict, "unsure")

    def test_covering_object_elsewhere_needs_review(self):
        away = obj("blue_card", "solid", (0, 11.75, OFF), (12, DURATION, [520, 150, 640, 260]))
        result = check(graph(objects(cube=self.cube_hidden(), card=away), STEPS, FINALS))
        self.assertNotEqual(self.cube_state(result)["evidence"], "covered")
        self.assertFalse(result.passed)

    def test_hand_or_unrequired_object_over_the_item_needs_review(self):
        steps = STEPS[:3]
        finals = FINALS[:3]
        result = check(graph(objects(cube=self.cube_hidden()), steps, finals))
        self.assertNotEqual(self.cube_state(result)["evidence"], "covered")
        self.assertFalse(result.passed)

    def test_placement_never_confirmed_cannot_be_covered(self):
        # The cube is never seen on the liner, so a card over it proves nothing about its placement.
        cube = obj("red_cube", "solid", (0, 5.75, OFF), (6, 13, [800, 100, 900, 200]))
        card = obj("blue_card", "solid", (0, 11.75, OFF), (12, DURATION, [790, 90, 910, 210]))
        result = check(graph(objects(cube=cube, card=card), STEPS, FINALS))
        self.assertNotEqual(self.cube_state(result)["evidence"], "covered")
        self.assertFalse(result.passed)

    def test_chain_of_covered_items(self):
        liner = obj("white_liner", "solid", (0, 2.75, OFF), (3, 7.5, LINER))
        cube = obj("red_cube", "solid", (0, 5.75, OFF), (6, 13, CUBE))
        result = check(graph(objects(liner=liner, cube=cube), STEPS, FINALS))
        self.assertEqual(self.cube_state(result)["evidence"], "covered")
        self.assertEqual(next(s for s in result.final_states if s["key"][1] == "white_liner")["evidence"], "covered")


if __name__ == "__main__":
    unittest.main()
