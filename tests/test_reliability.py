"""Offline regression checks for confidence, procedure validation and web integration.

Run: python -m unittest discover -s tests -v
No model calls or personal video files are required.
"""
import json
import unittest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from oneshot.check import compare
from oneshot.procedure import learn, pair_id
from oneshot.schema import RawGraph, TaskGraph
from web import live, storage
from web.views import report_view
from oneshot import evidence


def graph(*, tracked=False, position=None, missing=False):
    objects = [{"id": "red_block", "color": "red", "shape": "block"},
               {"id": "gray_tray", "color": "gray", "shape": "tray"}]
    if tracked:
        for o, box in zip(objects, [position or [20, 20, 30, 30], [10, 10, 70, 70]]):
            boxes = [] if missing and o["id"] == "red_block" else [
                {"frame": 30, "t_s": 1.0, "box": box}, {"frame": 60, "t_s": 2.0, "box": box}]
            o["tracked"] = bool(boxes)
            o["track"] = {"concept": o["id"], "sam_object_id": o["id"], "first_s": 1,
                          "last_s": 2, "visible_frames": len(boxes), "boxes": boxes} if boxes else None
    return TaskGraph.model_validate({
        "source": "fixture.mp4", "video": {"width": 200, "height": 200, "duration_s": 3, "fps": 30},
        "objects": objects,
        "steps": [{"order": 1, "start_s": 0, "end_s": 1, "action": "place", "object": "red_block",
                   "target": "gray_tray", "relation": "inside"}],
        "final_relations": [], "meta": {"model": "fixture", "track_seconds": 1 if tracked else None}})


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, name, value in [(live.budget, "FILE", Path(self.temp.name)/"budget.json"),
                                     (storage, "ROOT", Path(self.temp.name)/"private")]:
            mocked = patch.object(target, name, value)
            mocked.start(); self.addCleanup(mocked.stop)
    def test_empty_reference_never_passes(self):
        ref = graph().model_copy(update={"steps": [], "final_relations": []})
        result = compare(ref, graph())
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)
        self.assertEqual(result.issues[0].kind, "invalid_reference")

    def test_model_only_pass_is_explicitly_unverified(self):
        result = compare(graph(), graph())
        self.assertTrue(result.passed)
        self.assertFalse(result.verified)
        self.assertEqual(result.evidence_status, "model_only")

    def test_agreeing_tracker_corroborates(self):
        result = compare(graph(), graph(tracked=True))
        self.assertTrue(result.passed)
        self.assertTrue(result.verified)
        self.assertTrue(result.tracking_used)
        self.assertEqual(result.evidence_status, "corroborated")

    def test_conflicting_tracker_abstains_instead_of_passing(self):
        result = compare(graph(), graph(tracked=True, position=[150, 150, 160, 160]))
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)
        self.assertFalse(result.verified)
        self.assertTrue(any(i.kind == "doubt" and i.severity == "unknown" for i in result.issues))

    def test_unseen_reached_step_cannot_pass(self):
        result = compare(graph(), graph(tracked=True, missing=True))
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(result.passed)

    def test_missing_untracked_object_is_not_certain_skip(self):
        att = graph(tracked=True, missing=True).model_copy(update={"steps": []})
        result = compare(graph(), att)
        self.assertEqual(result.verdict, "unsure")
        self.assertFalse(any(i.kind == "skipped" for i in result.issues))

    def test_visible_missing_step_is_a_skip(self):
        att = graph(tracked=True).model_copy(update={"steps": []})
        result = compare(graph(), att)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.issues[0].kind, "skipped")

    def test_tracking_attempt_with_no_tracks_is_inconclusive(self):
        att = graph().model_copy(deep=True)
        att.meta.track_seconds = 1
        self.assertEqual(compare(graph(), att).verdict, "unsure")

    def test_final_picture_does_not_prove_required_order(self):
        ref = TaskGraph.model_validate_json((live.REFERENCE).read_text(encoding="utf-8"))
        proc = live.builtin()[1]
        att = ref.model_copy(update={"steps": []})
        result = compare(ref, att, procedure=proc)
        self.assertEqual(result.verdict, "unsure")
        self.assertTrue(any("when this step" in i.message for i in result.issues))

    def test_report_view_keeps_confidence_fields(self):
        result = report_view(compare(graph(), graph()), graph())
        self.assertEqual(result["evidence_status"], "model_only")
        self.assertFalse(result["tracking_used"])

    def test_sample_rule_changes_really_change_verdict(self):
        _, proc = live.builtin()
        self.assertEqual(live.recheck({}, demo=["pack_order"])["pack_order"]["verdict"], "fail")
        free = {pair_id(r.before, r.after): "free" for r in proc.pairs}
        self.assertEqual(live.recheck(free, demo=["pack_order"])["pack_order"]["verdict"], "pass")

    def test_tracking_jobs_count_toward_queue_capacity(self):
        jobs = {str(i): {"status": "checked"} for i in range(live.MAX_WAITING)}
        with patch.object(live, "_jobs", jobs), patch.object(live.budget, "ensure"):
            with self.assertRaises(live.UploadError):
                live.ensure_capacity()
            with self.assertRaises(live.UploadError):
                live.submit("extra", Path("unused.mp4"), None)

    def test_queued_job_rechecks_budget_before_model_calls(self):
        with patch.object(live, "_jobs", {"test": {"status": "queued"}}), \
             patch.object(live.budget, "ensure", side_effect=live.budget.BudgetError("Budget exhausted")), \
             patch.object(live, "build_task_graph") as build, patch.object(live.traceback,"print_exc"):
            live._run("test", Path("unused.mp4"), None, None)
            self.assertEqual(live.get("test")["status"], "error")
            build.assert_not_called()

    def test_tracker_stream_has_a_finite_deadline(self):
        async def stalled(*args):
            await asyncio.sleep(60)
        with patch.object(evidence, "_track_all", stalled), patch.object(evidence, "TRACK_TIMEOUT_SECONDS", 0.01):
            result = evidence.track_objects(Path("unused.mp4"), ["red block"], 30)
            self.assertIn("red block", result.errors)
            self.assertIsNone(result.tracks["red block"])

    def test_tracking_failure_withholds_acceptance_and_keeps_initial_assessment(self):
        with patch.object(live, "_jobs", {"test": {"status": "queued"}}), \
             patch.object(live, "_graphs", {}), patch.object(live.budget, "ensure"), \
             patch.object(live.budget, "charge"), patch.object(live, "_check_length", return_value=3), \
             patch.object(live, "builtin", return_value=(graph(), learn([graph()]))), \
             patch.object(live, "build_task_graph", return_value=graph()), \
             patch.object(live, "_work", return_value=Path(self.temp.name)), \
             patch.object(live, "small_mp4", return_value=Path("unused.mp4")), \
             patch.object(live, "_web_video", return_value="fixture.mp4"), \
             patch.object(live, "add_evidence", side_effect=TimeoutError("tracking deadline")), \
             patch.object(live.traceback, "print_exc"):
            live._run("test", Path("unused.mp4"), None, None)
            job = live.get("test")
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"]["evidence_status"], "inconclusive")
            self.assertFalse(job["result"]["passed"])
            self.assertEqual(job["result"]["verdict"], "unsure")
            self.assertEqual(job["preliminary_result"]["evidence_status"], "model_only")
            self.assertFalse(job["result"]["verified"])
            self.assertIsNone(job["seen"])
            rechecked = live.recheck({}, job="test")["test"]
            self.assertEqual(rechecked["verdict"], "unsure")
            self.assertFalse(rechecked["passed"])


class ExtractionValidationTests(unittest.TestCase):
    def raw(self):
        g = graph()
        return {"objects": [{"id": o.id} for o in g.objects],
                "steps": [s.model_dump(exclude={"evidence"}) for s in g.steps], "final_relations": []}

    def test_valid_graph(self):
        self.assertEqual(len(RawGraph.model_validate(self.raw()).steps), 1)

    def test_duplicate_object_ids(self):
        d = self.raw(); d["objects"].append(d["objects"][0])
        with self.assertRaises(ValidationError): RawGraph.model_validate(d)

    def test_unknown_target(self):
        d = self.raw(); d["steps"][0]["target"] = "invented"
        with self.assertRaises(ValidationError): RawGraph.model_validate(d)

    def test_bad_timestamps(self):
        for start, end in [(-1, 1), (2, 1), (1, 1), (0, float("inf")), (float("nan"), 1)]:
            d = self.raw(); d["steps"][0].update(start_s=start, end_s=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                RawGraph.model_validate(d)

    def test_unknown_final_object(self):
        d = self.raw(); d["final_relations"] = [{"relation": "on", "a": "invented", "b": "gray_tray"}]
        with self.assertRaises(ValidationError): RawGraph.model_validate(d)


class JudgeEvidenceTests(unittest.TestCase):
    def test_phone_reference_is_available_for_uploads(self):
        ref, proc = live.builtin("real")
        self.assertEqual(len(proc.steps), 2)
        self.assertEqual(len(ref.objects), 2)

    def test_real_results_exclude_reference_and_keep_abstentions(self):
        data = json.loads((live.WEB / "static/media/real_results.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["attempts"]), 9)
        self.assertFalse(any(a["id"] in ("real_ref", "real_ok_1") for a in data["attempts"]))
        self.assertEqual(data["summary"]["abstentions"], sum(a["verdict"] == "unsure" for a in data["attempts"]))
        self.assertTrue(data["self_check"]["excluded_from_score"])
        self.assertFalse(data["self_check"]["passed"])
        self.assertFalse(data["method"]["fresh_model_inference"])

    def test_real_sample_graphs_follow_current_confidence_policy(self):
        ref, proc = live.builtin("real")
        results = json.loads((live.WEB / "static/media/real_results.json").read_text(encoding="utf-8"))
        for row in results["attempts"]:
            att = TaskGraph.model_validate_json((live.DEMO / f"{row['id']}.graph.json").read_text(encoding="utf-8"))
            with self.subTest(case=row["id"]):
                report = compare(ref, att, procedure=proc)
                self.assertEqual(report.verdict, row["verdict"])
                self.assertEqual(report.evidence_status, row["evidence_status"])

    def test_real_video_hashes_are_unique_and_complete(self):
        import hashlib
        data = json.loads((live.WEB / "static/media/real_results.json").read_text(encoding="utf-8"))
        hashes = []
        for a in data["attempts"]:
            path = live.WEB / "static" / a["demo"]
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(actual, a["clip_sha256"])
            hashes.append(actual)
        self.assertEqual(len(set(hashes)), 9)


if __name__ == "__main__":
    unittest.main()
