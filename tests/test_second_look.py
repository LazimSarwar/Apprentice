"""A reported problem is double-checked, and one reading can never overrule another into a pass."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oneshot import media, second_look
from oneshot.check import compare
from oneshot.procedure import learn
from oneshot.schema import Box, GraphMeta, ObjectWithEvidence, Relation, StepWithEvidence, TaskGraph, Track, VideoInfo
from web import live

CARD = ("on", "blue_card", "red_cube")
FINALS = [Relation(relation="on", a="red_cube", b="white_liner"), Relation(relation="on", a="blue_card", b="red_cube")]


def obj(name, kind="solid"):
    color, shape = name.split("_")
    return ObjectWithEvidence(id=name, color=color, shape=shape, kind=kind, tracking_description=f"{color} {shape}")


def step(order, end, name, target, relation="on"):
    return StepWithEvidence(order=order, start_s=end - 1, end_s=end, action="place", object=name, target=target, relation=relation)


def graph(steps, finals, looks=()):
    return TaskGraph(source="x", video=VideoInfo(width=640, height=480, fps=10, frames=300, duration_s=30),
                     objects=[obj("white_liner"), obj("red_cube"), obj("blue_card")], steps=steps, final_relations=finals,
                     meta=GraphMeta(model="test", second_looks=list(looks)))


REF = graph([step(1, 4, "red_cube", "white_liner"), step(2, 9, "blue_card", "red_cube")], FINALS)
PROC = learn([REF])
# The reading says the card went on the liner.
WRONG = graph([step(1, 4, "red_cube", "white_liner"), step(2, 9, "blue_card", "white_liner")],
              [FINALS[0], Relation(relation="on", a="blue_card", b="white_liner")])
# The reading reports no card step at all.
SKIP = graph([step(1, 4, "red_cube", "white_liner")], [FINALS[0]])


def look(outcome, key=CARD, kind="wrong_place"):
    return {"key": list(key), "kind": kind, "looks": [], "outcome": outcome}


def replying(*meanings):
    """A fake model that picks the option carrying a given meaning, in order of calls."""
    answers = list(meanings)

    def ask(_clip, prompt):
        wanted = answers.pop(0)
        if isinstance(wanted, Exception):
            raise wanted
        if wanted == "bad":
            return "not json at all"
        options = dict(re.findall(r"^([A-E])\. (.+)$", prompt, re.M))
        for letter, text in options.items():
            if wanted == "expected" and text.startswith("The blue card is on the red cube"):
                return json.dumps({"choice": letter, "what_you_see": "seen"})
            if wanted == "other" and "somewhere else" in text or (wanted == "other" and " not " in text):
                return json.dumps({"choice": letter, "what_you_see": "seen"})
            if wanted == "unclear" and text.startswith("You cannot"):
                return json.dumps({"choice": letter, "what_you_see": "seen"})
        raise AssertionError(prompt)
    return ask


def fake_cut(_clip, _start, _end, out):
    return out


class CompareTests(unittest.TestCase):
    def check(self, att):
        return compare(REF, att, procedure=PROC)

    def test_problem_without_a_closer_look_is_reported_as_before(self):
        result = self.check(WRONG)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.issues[0].check_key, list(CARD))

    def test_confirmed_problem_stays_a_failure(self):
        att = WRONG.model_copy(update={"meta": GraphMeta(model="t", second_looks=[look("confirmed")])})
        self.assertEqual(self.check(att).verdict, "fail")

    def test_disputed_problem_goes_to_review_and_never_to_a_pass(self):
        att = WRONG.model_copy(update={"meta": GraphMeta(model="t", second_looks=[look("disputed")])})
        result = self.check(att)
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)
        self.assertTrue(any("two closer looks show the blue card on the red cube" in i.message for i in result.issues))
        self.assertFalse(any(i.severity == "fail" for i in result.issues))

    def test_unclear_looks_go_to_review(self):
        att = WRONG.model_copy(update={"meta": GraphMeta(model="t", second_looks=[look("unclear")])})
        result = self.check(att)
        self.assertEqual(result.verdict, "unsure")
        self.assertTrue(any("did not agree" in i.message for i in result.issues))

    def test_look_that_could_not_run_leaves_the_problem_as_it_was(self):
        att = WRONG.model_copy(update={"meta": GraphMeta(model="t", second_looks=[look("not_checked")])})
        self.assertEqual(self.check(att).verdict, "fail")

    def test_disputed_skip_goes_to_review(self):
        att = SKIP.model_copy(update={"meta": GraphMeta(model="t", second_looks=[look("disputed", kind="skipped")])})
        result = self.check(att)
        self.assertEqual(result.verdict, "unsure")
        self.assertTrue(any("first reading missed this step" in i.message for i in result.issues))

    def test_other_real_failures_still_fail_when_one_problem_is_disputed(self):
        ref = graph(REF.steps + [step(3, 12, "white_liner", "red_cube")], FINALS)
        att = WRONG.model_copy(update={"meta": GraphMeta(model="t", second_looks=[look("disputed")])})
        result = compare(ref, att, procedure=learn([ref]))
        self.assertEqual(result.verdict, "fail")
        self.assertTrue(any(i.kind == "skipped" for i in result.issues))

    def test_rule_recheck_keeps_the_closer_look_result(self):
        att = WRONG.model_copy(update={"meta": GraphMeta(model="t", second_looks=[look("disputed")])})
        stored = TaskGraph.model_validate(json.loads(att.model_dump_json()))
        self.assertEqual(self.check(stored).verdict, "unsure")


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.issue = compare(REF, WRONG, procedure=PROC).issues[0]

    def test_only_placement_failures_are_checked(self):
        kinds = {i.kind for i in second_look.to_check(compare(REF, WRONG, procedure=PROC))}
        self.assertEqual(kinds, {"wrong_place"})
        order_ref = graph([step(1, 4, "red_cube", "white_liner"), step(2, 9, "blue_card", "red_cube")], FINALS)
        swapped = graph([step(1, 4, "blue_card", "red_cube"), step(2, 9, "red_cube", "white_liner")], FINALS)
        self.assertEqual(second_look.to_check(compare(order_ref, swapped, procedure=learn([order_ref]))), [])

    def test_number_of_problems_checked_is_bounded(self):
        report = compare(REF, WRONG, procedure=PROC)
        many = [type(self.issue)("wrong_place", "fail", "x", 1.0, check_key=["on", f"o{i}", "t"]) for i in range(9)]
        report.issues = many
        self.assertEqual(len(second_look.to_check(report)), second_look.MAX_PROBLEMS)

    def test_question_does_not_say_what_the_first_reading_was(self):
        prompt, meanings = second_look.build_question(self.issue, WRONG, (6.0, 12.0))
        self.assertNotIn("first reading", prompt.lower())
        self.assertNotIn("expected", prompt.lower())
        self.assertNotIn("reference", prompt.lower())
        self.assertIn("The blue card is on the red cube", prompt)
        self.assertIn("The blue card is on the white liner", prompt)
        self.assertEqual(sorted(meanings.values()), ["expected", "other", "other", "unclear"])

    def test_option_order_varies_but_is_repeatable(self):
        places = set()
        for span in ((1.0, 5.0), (2.0, 6.0), (3.0, 7.0), (4.0, 8.0), (5.0, 9.0), (6.0, 10.0), (7.0, 11.0)):
            first = second_look.build_question(self.issue, WRONG, span)
            self.assertEqual(first, second_look.build_question(self.issue, WRONG, span))
            places.add(next(k for k, v in first[1].items() if v == "expected"))
        self.assertGreater(len(places), 1)

    def test_short_and_wide_windows_are_inside_the_recording(self):
        self.assertEqual(second_look.windows(self.issue, 30.0), [(6.0, 12.0), (1.0, 29.0)])
        self.assertEqual(second_look.windows(self.issue, 10.0), [(6.0, 9.0), (1.0, 9.0)])
        skip = compare(REF, SKIP, procedure=PROC).issues[0]
        self.assertEqual(second_look.windows(skip, 30.0), [(21.0, 29.0), (4.0, 29.0)])

    def test_skip_expected_near_the_end_still_gets_two_looks(self):
        skip = compare(REF, SKIP, procedure=PROC).issues[0]
        late = type(skip)(skip.kind, skip.severity, skip.message, 8.5, check_key=skip.check_key)
        self.assertEqual(second_look.windows(late, 10.0), [(1.0, 9.0), (0.0, 9.0)])

    def test_prompt_allows_for_a_hand_covering_the_camera(self):
        prompt, _ = second_look.build_question(self.issue, WRONG, (6.0, 12.0))
        self.assertIn("LAST moment", prompt)
        self.assertIn("covers the camera", prompt)

    def test_bad_model_output_is_unclear(self):
        self.assertEqual(second_look.read_answer("nonsense", {"A": "expected"})[0], "unclear")
        self.assertEqual(second_look.read_answer('{"choice": "Z"}', {"A": "expected"})[0], "unclear")
        self.assertEqual(second_look.read_answer('["A"]', {"A": "expected"})[0], "unclear")

    def test_outcome_needs_two_agreeing_looks(self):
        self.assertEqual(second_look.outcome(["other", "other"]), "confirmed")
        self.assertEqual(second_look.outcome(["expected", "expected"]), "disputed")
        self.assertEqual(second_look.outcome(["other", "expected"]), "unclear")
        self.assertEqual(second_look.outcome(["other", "unclear"]), "unclear")
        self.assertEqual(second_look.outcome(["other"]), "not_checked")
        self.assertEqual(second_look.outcome(["other", "error"]), "not_checked")


class RunTests(unittest.TestCase):
    def run_with(self, *meanings, **kwargs):
        report = compare(REF, WRONG, procedure=PROC)
        return second_look.run(report, WRONG, Path("clip.mp4"), Path("work"), ask=replying(*meanings), cut=fake_cut, **kwargs)

    def test_two_looks_that_show_the_problem_confirm_it(self):
        self.assertEqual(self.run_with("other", "other")[0]["outcome"], "confirmed")

    def test_two_looks_that_contradict_it_dispute_it(self):
        found = self.run_with("expected", "expected")[0]
        self.assertEqual(found["outcome"], "disputed")
        self.assertEqual(len(found["looks"]), 2)

    def test_failed_call_is_recorded_and_does_not_raise(self):
        self.assertEqual(self.run_with(RuntimeError("down"), "other")[0]["outcome"], "not_checked")

    def test_bad_output_makes_the_result_unclear(self):
        self.assertEqual(self.run_with("bad", "other")[0]["outcome"], "unclear")

    def test_spending_guard_wraps_every_model_call(self):
        calls = []

        class Guard:
            def __enter__(self): calls.append("in")
            def __exit__(self, *a): return False
        self.run_with("other", "other", guard=Guard)
        self.assertEqual(len(calls), 2)

    def test_no_problems_means_no_calls(self):
        report = compare(REF, REF, procedure=PROC)
        self.assertEqual(second_look.run(report, REF, Path("c"), Path("w"), ask=lambda *_: self.fail("called"), cut=fake_cut), [])

    def test_deadline_records_looks_as_not_checked(self):
        import threading
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(_clip, _prompt):
            release.wait(5)
            return "{}"
        report = compare(REF, WRONG, procedure=PROC)
        found = second_look.run(report, WRONG, Path("c"), Path("w"), ask=slow, cut=fake_cut, deadline=0.05)
        self.assertEqual(found[0]["outcome"], "not_checked")


def with_tracks(att):
    """Each object arrives at its own spot, so the tracker supports the reading."""
    spots = {"white_liner": (0, [100, 100, 400, 400]), "red_cube": (4, [120, 120, 220, 220]), "blue_card": (9, [260, 260, 360, 360])}
    objects = []
    for o in att.objects:
        start, box = spots[o.id]
        boxes = [Box(frame=i, t_s=i * 0.5, box=[500, 400, 600, 470] if i * 0.5 < start else box) for i in range(60)]
        objects.append(o.model_copy(update={"tracked": True, "track": Track(concept=o.id, sam_object_id=o.id, first_s=0, last_s=29.5, visible_frames=60, boxes=boxes)}))
    return att.model_copy(update={"objects": objects, "meta": att.meta.model_copy(update={"track_seconds": 1.0})})


class LiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name)

    def test_finalize_runs_looks_and_keeps_them_for_rule_changes(self):
        tracked = with_tracks(WRONG)
        job = "a" * 32
        with patch.object(live, "add_evidence", lambda g, _c: tracked), \
                patch.object(second_look, "run", return_value=[look("disputed")]) as run, \
                patch.object(live, "overlay_frames", side_effect=OSError), patch.object(live.iio, "immeta", return_value={"fps": 10}), \
                patch.object(live, "_work", return_value=self.work), patch.object(live.budget, "call"):
            kept, result, _, _ = live.finalize(job, REF, WRONG, PROC, "clip.mp4")
        self.addCleanup(lambda: (live._jobs.pop(job, None), live._graphs.pop(job, None)))
        run.assert_called_once()
        self.assertEqual(result["verdict"], "unsure")
        self.assertEqual(kept.meta.second_looks[0]["outcome"], "disputed")
        self.assertEqual(live._graphs[job][1].meta.second_looks[0]["outcome"], "disputed")
        self.assertEqual(result["second_looks"][0]["outcome"], "disputed")

    def test_finalize_skips_looks_when_no_problem_is_reported(self):
        tracked = with_tracks(REF)
        job = "b" * 32
        with patch.object(live, "add_evidence", lambda g, _c: tracked), patch.object(second_look, "run") as run, \
                patch.object(live, "overlay_frames", side_effect=OSError), patch.object(live.iio, "immeta", return_value={"fps": 10}), \
                patch.object(live, "_work", return_value=self.work), patch.object(live.budget, "call"):
            live.finalize(job, REF, REF, PROC, "clip.mp4")
        self.addCleanup(lambda: (live._jobs.pop(job, None), live._graphs.pop(job, None)))
        run.assert_not_called()


class SegmentTests(unittest.TestCase):
    def test_segment_cuts_the_requested_stretch(self):
        seen = {}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(media, "_encode", lambda command, target: seen.update(command=command) or target):
            media.segment(Path("in.mp4"), 2.5, 6.0, Path(folder) / "out.mp4")
        command = seen["command"]
        self.assertEqual(command[command.index("-ss") + 1], "2.50")
        self.assertEqual(command[command.index("-t") + 1], "3.50")
        self.assertIn("-an", command)

    def test_segment_rejects_a_backwards_clip(self):
        with self.assertRaises(ValueError):
            media.segment(Path("in.mp4"), 6.0, 2.0, Path("out.mp4"))


if __name__ == "__main__":
    unittest.main()
