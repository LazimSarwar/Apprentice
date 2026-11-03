"""The packaged baseline must match this build and keep its unfavourable results."""
import json
import unittest

from oneshot.check import compare
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from web.baseline import final_picture, model_only, refresh
from web.build_judging import APP, DEMO, MEDIA, pipeline_digest


def builtin():
    ref = TaskGraph.model_validate_json((APP / "web/reference.graph.json").read_text(encoding="utf-8"))
    proc = Procedure.from_dict(json.loads((APP / "web/reference.procedure.json").read_text(encoding="utf-8")))
    return ref, proc


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((MEDIA / "baseline_comparison.json").read_text(encoding="utf-8"))

    def test_report_matches_this_build_without_model_calls(self):
        self.assertEqual(self.data["pipeline_sha256"], pipeline_digest())
        self.assertFalse(self.data["fresh_model_inference"])

    def test_report_can_be_reproduced_from_packaged_readings(self):
        before = (MEDIA / "baseline_comparison.json").read_text(encoding="utf-8")
        self.assertEqual(refresh(), json.loads(before))

    def test_final_picture_accepts_what_the_checker_rejects(self):
        ref, proc = builtin()
        att = TaskGraph.model_validate_json((DEMO / "pack_order.graph.json").read_text(encoding="utf-8"))
        self.assertEqual(final_picture(ref, att, proc), "pass")
        self.assertEqual(compare(ref, att, procedure=proc).verdict, "fail")

    def test_final_picture_rejects_a_missing_required_step(self):
        ref, proc = builtin()
        att = TaskGraph.model_validate_json((DEMO / "pack_skip_card.graph.json").read_text(encoding="utf-8"))
        self.assertEqual(final_picture(ref, att, proc), "fail")

    def test_model_only_copy_has_no_tracking_and_leaves_the_original_alone(self):
        att = TaskGraph.model_validate_json((DEMO / "pack_ok.graph.json").read_text(encoding="utf-8"))
        stripped = model_only(att)
        self.assertTrue(all(o.track is None for o in stripped.objects))
        self.assertIsNone(stripped.meta.track_seconds)
        self.assertTrue(any(o.track for o in att.objects))

    def test_unfavourable_phone_result_stays_visible(self):
        phone = self.data["phone"]["summary"]
        self.assertEqual(phone["cases"], 9)
        self.assertEqual(phone["order_only"], 0)
        reading = phone["systems"]["model_reading"]
        tracked = phone["systems"]["with_tracking"]
        self.assertGreater(tracked["correct_review"] + tracked["incorrect_review"],
                           reading["correct_review"] + reading["incorrect_review"])
        self.assertTrue(any("not a held-out benchmark" in limit for limit in self.data["limits"]))


if __name__ == "__main__":
    unittest.main()
