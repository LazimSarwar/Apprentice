"""Recognition is open vocabulary; reference identities require visual support."""
import unittest

from oneshot.extract import build_prompt
from oneshot.procedure import map_objects
from oneshot.schema import ObjectWithEvidence
from oneshot.check import compare
from web.live import _known
from web.views import report_view
from test_reliability import graph


def objects(*items):
    return graph().model_copy(update={'objects': [ObjectWithEvidence(**item) for item in items],
                                      'steps': [], 'final_relations': []})


class DynamicObjectTests(unittest.TestCase):
    def test_reused_id_with_conflicting_appearance_never_passes(self):
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                ref=graph(tracked=tracked);att=graph(tracked=tracked)
                att.objects[0].color='blue';att.objects[0].shape='mug'
                att.objects[0].tracking_description='blue ceramic mug'
                saved=att.model_dump()
                report=compare(ref,att)
                self.assertEqual(report.verdict,'unsure')
                self.assertFalse(report.verified)
                self.assertNotIn('red_block',report.mapping)
                self.assertTrue(any(i.kind=='identity' for i in report.issues))
                self.assertEqual(att.model_dump(),saved)
                self.assertEqual(report_view(report,att)['detections'][0]['status'],'ambiguous')

    def test_conflicting_target_identity_cannot_verify_containment(self):
        ref=graph(tracked=True);att=graph(tracked=True)
        ref.objects[1].kind='container';att.objects[1].kind='solid'
        report=compare(ref,att)
        self.assertFalse(report.passed)
        self.assertTrue(any(i.kind=='identity' and 'gray_tray' in i.objects for i in report.issues))

    def test_same_id_with_changed_marking_requires_review(self):
        ref=graph(tracked=True);att=graph(tracked=True)
        ref.objects[0].tracking_description='red block with blue logo'
        att.objects[0].tracking_description='red block with green logo'
        self.assertEqual(compare(ref,att).verdict,'unsure')

    def test_compatible_refinements_do_not_break_exact_identity(self):
        ref=graph(tracked=True);att=graph(tracked=True)
        ref.objects[0].tracking_description='red block'
        att.objects[0].tracking_description='Red wooden cube'
        att.objects[0].shape='cube';att.objects[1].color='grey'
        self.assertTrue(compare(ref,att).passed)

    def test_identity_conflict_does_not_accuse_a_wrong_placement(self):
        ref=graph(tracked=True);att=graph(tracked=True)
        att.objects[0].color='blue'
        att.steps[0].target=None;att.steps[0].relation='on'
        report=compare(ref,att)
        self.assertEqual(report.verdict,'unsure')
        self.assertFalse(any(i.severity=='fail' for i in report.issues))

    def test_reference_hint_preserves_appearance_without_expected_positions(self):
        g=graph(tracked=True)
        g.objects[0].tracking_description='red wooden block with a white star'
        hint=_known(g)[0]
        self.assertEqual(hint['tracking_description'],'red wooden block with a white star')
        self.assertNotIn('track',hint)
        self.assertNotIn('steps',hint)
        prompt=build_prompt([{**hint,'expected_location':'secret_expected_location'}])
        self.assertIn('white star',prompt)
        self.assertNotIn('secret_expected_location',prompt)

    def test_new_object_types_match_by_unique_visible_description(self):
        ref=objects({'id':'tea_cup','shape':'cup','kind':'container','tracking_description':'blue ceramic cup'},
                    {'id':'utensil','shape':'spoon','tracking_description':'silver teaspoon'})
        att=objects({'id':'blue_mug','shape':'mug','kind':'container','tracking_description':'blue ceramic cup'},
                    {'id':'small_spoon','shape':'spoon','tracking_description':'silver teaspoon'})
        self.assertEqual(map_objects(ref,att),{'blue_mug':'tea_cup','small_spoon':'utensil'})

    def test_same_colour_different_object_is_not_a_substitute(self):
        ref=objects({'id':'required_adapter','color':'white','shape':'charger'})
        att=objects({'id':'unexpected_card','color':'white','shape':'card'})
        self.assertEqual(map_objects(ref,att),{})

    def test_different_visible_markings_are_not_merged(self):
        ref=objects({'id':'card_a','color':'white','shape':'card','tracking_description':'white card with blue logo'})
        att=objects({'id':'card_b','color':'white','shape':'card','tracking_description':'white card with red logo'})
        self.assertEqual(map_objects(ref,att),{})

    def test_two_candidate_instances_cannot_claim_one_required_object(self):
        ref=objects({'id':'required_key','color':'silver','shape':'key'})
        att=objects({'id':'key_one','color':'silver','shape':'key'},
                    {'id':'key_two','color':'silver','shape':'key'})
        self.assertEqual(map_objects(ref,att),{})
        att.objects.reverse()
        self.assertEqual(map_objects(ref,att),{})

    def test_unique_legacy_shapes_still_match_without_rich_descriptions(self):
        ref=objects({'id':'block','color':'gray','shape':'block'})
        att=objects({'id':'cube','color':'grey','shape':'cube'})
        self.assertEqual(map_objects(ref,att),{'cube':'block'})

    def test_detection_panel_distinguishes_model_detection_from_tracking(self):
        ref=graph();att=graph()
        view=report_view(compare(ref,att),att)
        self.assertTrue(all(d['status']=='model_only' for d in view['detections']))
        att=graph(tracked=True)
        view=report_view(compare(ref,att),att)
        self.assertTrue(all(d['status']=='tracked' for d in view['detections']))
        self.assertEqual(view['detections'][0]['first_seen'],1)

    def test_duplicate_tracking_identity_is_visible_in_detection_panel(self):
        att=graph(tracked=True)
        att.objects[1].track=att.objects[0].track.model_copy(deep=True)
        view=report_view(compare(graph(),att),att)
        self.assertTrue(all(d['status']=='ambiguous' for d in view['detections']))
        self.assertFalse(view['verified'])
