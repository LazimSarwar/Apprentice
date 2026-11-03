"""Identity and description regressions; fixtures do not use personal videos."""
import unittest
from pathlib import Path
from unittest.mock import patch

from oneshot.check import compare
from oneshot.evidence import TrackingBatch
from oneshot.graph import add_evidence, build_task_graph, track_with_fallback
from oneshot.schema import Box, Object, RawGraph
from oneshot.tracking_identity import ambiguous_tracks
from test_reliability import graph


def duplicate_attempt():
    att = graph(tracked=True).model_copy(deep=True)
    item = att.objects[0]
    item.track.boxes = [Box(frame=i*30, t_s=i, box=[150,150,170,170]) for i in range(3)]
    item.track.first_s, item.track.last_s, item.track.visible_frames = 0, 2, 3
    other = item.model_copy(deep=True)
    other.id = 'white_card'
    other.track.sam_object_id = 'another-query-id'
    other.track.boxes = [b.model_copy(update={'box':[151,150,171,170]}) for b in other.track.boxes]
    att.objects.append(other)
    return att


class IdentityTests(unittest.TestCase):
    def test_jittered_duplicate_tracks_require_review_not_a_placement_accusation(self):
        att = duplicate_attempt()
        result = compare(graph(), att)
        self.assertEqual(result.verdict, 'unsure')
        self.assertFalse(any(i.kind == 'doubt' for i in result.issues))
        self.assertTrue(any('confused' in i.message for i in result.issues))
        self.assertIsNotNone(att.objects[0].track)  # Original evidence stays intact.

    def test_duplicate_tracks_cannot_prove_a_skip(self):
        att = duplicate_attempt()
        att.steps = []
        result = compare(graph(), att)
        self.assertEqual(result.verdict, 'unsure')
        self.assertFalse(any(i.kind == 'skipped' for i in result.issues))

    def test_duplicate_target_cannot_support_a_wrong_place(self):
        att = duplicate_attempt()
        att.steps[0].target = 'white_card'
        att.steps[0].relation = 'on'
        result = compare(graph(), att)
        self.assertFalse(any(i.kind == 'wrong_place' for i in result.issues))

    def test_real_conflicting_track_still_requires_review(self):
        result = compare(graph(), graph(tracked=True, position=[150,150,170,170]))
        self.assertTrue(any(i.kind == 'doubt' for i in result.issues))
        self.assertFalse(result.passed)

    def test_brief_overlap_during_stacking_does_not_merge_identities(self):
        att = duplicate_attempt()
        att.objects[-1].track.boxes[0].box = [30,30,50,50]
        att.objects[-1].track.boxes[1].box = [60,60,80,80]
        self.assertEqual(ambiguous_tracks(att.objects), {})

    def test_small_object_inside_large_container_is_not_a_duplicate(self):
        self.assertEqual(ambiguous_tracks(graph(tracked=True).objects), {})

    def test_one_matching_frame_does_not_merge_different_tracks(self):
        att = duplicate_attempt()
        att.objects[-1].track.boxes = att.objects[-1].track.boxes[-1:]
        self.assertEqual(ambiguous_tracks(att.objects), {})

    def test_identity_switch_after_occlusion_cannot_verify_the_final_state(self):
        att = duplicate_attempt()
        a, b = att.objects[0], att.objects[-1]
        a.track.boxes = [Box(frame=i,t_s=i/30,box=[20,20,30,30]) for i in range(181)]
        b.track.boxes = [Box(frame=i,t_s=i/30,box=[100,100,110,110] if i < 150 else [20,20,30,30])
                        for i in range(181)]
        for obj in (a,b):
            obj.track.last_s = 6
        self.assertEqual(ambiguous_tracks(att.objects), {})
        self.assertEqual(set(ambiguous_tracks(att.objects,5,6)), {a.id,b.id})
        att.video.duration_s = 6
        result = compare(graph(),att)
        self.assertFalse(result.verified)
        self.assertTrue(any('final placement' in i.message for i in result.issues))


class DescriptionTests(unittest.TestCase):
    def test_specific_visible_description_is_used_with_legacy_fallback(self):
        obj = Object(id='adapter', color='white', shape='block', tracking_description=' white  charger ')
        self.assertEqual(obj.concept(), 'white charger')
        self.assertEqual(Object(id='red',color='red',shape='block').concept(), 'red block')

    def test_duplicate_descriptions_ignore_case_and_spacing(self):
        objs = [Object(id='a', tracking_description='white charger'),
                Object(id='b', tracking_description=' White  Charger ')]
        with patch('oneshot.graph.track_objects', return_value=TrackingBatch()) as track:
            batch = track_with_fallback(Path('unused'), objs, 30)
        self.assertEqual(track.call_args.args[1], [])
        self.assertEqual(set(batch.errors), {'a','b'})

    def test_extraction_and_evidence_attachment_preserve_descriptions_and_kinds(self):
        g = graph()
        raw = RawGraph(objects=[o.model_dump(exclude={'track','tracked'}) for o in g.objects],
                       steps=[s.model_dump(exclude={'evidence'}) for s in g.steps], final_relations=[])
        raw.objects[0].tracking_description = 'red wooden block'
        raw.objects[1].kind = 'container'
        meta = {'provider':'hosted','model':'fixture','extract_seconds':1,'extract_cost_usd':0}
        with patch('oneshot.graph.small_mp4',return_value=Path('unused')), \
             patch('oneshot.graph.video_info',return_value=g.video), \
             patch('oneshot.graph.extract_graph',return_value=(raw,meta)):
            built = build_task_graph(Path('unused'), track=False)
        self.assertEqual(built.objects[0].tracking_description, 'red wooden block')
        self.assertEqual(built.objects[1].kind, 'container')
        with patch('oneshot.graph.video_info',return_value=g.video), \
             patch('oneshot.graph.track_with_fallback',return_value=TrackingBatch()):
            attached = add_evidence(built, Path('unused'))
        self.assertEqual(attached.objects[0].tracking_description, 'red wooden block')
        self.assertEqual(attached.objects[1].kind, 'container')

    def test_hidden_final_object_remains_unverified(self):
        att = graph(tracked=True).model_copy(deep=True)
        att.video.duration_s = 6
        att.objects[1].track.boxes.append(Box(frame=180,t_s=6,box=[10,10,70,70]))
        att.objects[1].track.last_s = 6
        result = compare(graph(), att)
        self.assertFalse(result.passed)
        self.assertTrue(any('final placement' in i.message for i in result.issues))
