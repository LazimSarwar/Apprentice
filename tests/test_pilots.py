"""Prospective trials must preserve labels, privacy, failures and first submissions."""
import io
import json
import time
import unittest
from unittest.mock import patch

from web import live, pilots, storage, watch
from oneshot.check import compare
from oneshot.procedure import learn
from web.views import report_view
from test_services import IsolatedServices
from test_reliability import graph


class PilotTests(IsolatedServices):
    def setUp(self):
        super().setUp()
        self.ref, self.proc = live.builtin('pack')
        self.proc.final_goals = [s.key for s in self.proc.steps]
        self.roles = {'liner':0,'product':1,'accessory':2,'card':3}
        self.create()
        live._jobs[self.job] = {'id':self.job,'status':'ready','procedure_only':True,'procedure_confirmed':True,'title':'Packing'}
        storage.update(self.job, reference_graph=self.ref.model_dump(),procedure=self.proc.to_dict(),job=live._jobs[self.job])
        self.pilot = pilots.create(self.job,'frozen-code',roles=self.roles)['pilot']

    def test_packing_protocol_requires_matching_reference_and_role_assignment(self):
        for roles in (None, {'liner':0,'product':1,'accessory':2,'card':2}):
            with self.assertRaises(live.UploadError):
                pilots.create(self.job,'frozen-code',roles=roles)
        changed=self.proc.with_overrides({f"{'/'.join(self.proc.steps[0].key)} -> {'/'.join(self.proc.steps[2].key)}":'free'})
        storage.update(self.job,procedure=changed.to_dict())
        with self.assertRaises(live.UploadError):
            pilots.create(self.job,'frozen-code',roles=self.roles)

    def test_editing_reference_preserves_frozen_trial(self):
        before=storage.record(self.pilot)['plan_sha256']
        live.edit_reference(self.job)
        self.assertEqual(live.get(self.job)['status'],'awaiting_confirmation')
        self.assertEqual(storage.record(self.pilot)['plan_sha256'],before)
        self.assertEqual(pilots.report(self.pilot,'frozen-code')['summary']['completed'],0)

    def test_reference_only_confirmation_does_not_start_an_attempt(self):
        refid='c'*32;storage.create(refid)
        live._jobs[refid]={'status':'awaiting_confirmation','procedure_only':True}
        live._pending[refid]=(None,self.ref,self.proc)
        with patch.object(live._pool,'submit') as submit:
            live.confirm(refid,{'procedure':self.proc.to_dict()})
        submit.assert_not_called()
        self.assertEqual(live.get(refid)['status'],'ready')
        self.assertEqual(live.load_reference(refid)[1].to_dict(),self.proc.to_dict())

    def test_ready_reference_can_start_camera_without_an_attempt_graph(self):
        ref,proc,own=watch._reference(self.job)
        self.assertTrue(own)
        self.assertEqual(proc.steps[0].key,self.proc.steps[0].key)

    def test_empty_trial_never_implies_completed_validation(self):
        report=pilots.report(self.pilot,'frozen-code')
        self.assertEqual(report['summary']['planned'],9)
        self.assertEqual(report['summary']['completed'],0)
        self.assertIsNone(report['summary']['median_self_timed_review_difference_seconds'])
        self.assertEqual(report['summary']['participant_codes_self_reported'],[])

    def test_labels_frozen_before_inference_and_not_passed_to_pipeline(self):
        before=storage.record(self.pilot)['plan_sha256']
        with patch.object(live,'submit_known') as submit:
            row=pilots.submit(self.pilot,'P02','B',io.BytesIO(b'new clip'),'attempt.mp4','frozen-code',label_confirmed=True)
        args,kwargs=submit.call_args
        self.assertEqual(args[2].model_dump(),self.ref.model_dump())
        self.assertNotIn('expected',kwargs)
        self.assertEqual(set(kwargs['metadata']),{'pilot','pilot_case'})
        self.assertEqual(storage.record(self.pilot)['plan_sha256'],before)
        self.assertEqual(storage.record(self.pilot)['plan']['case_labels']['P02'],'order')

    def test_source_change_refuses_new_inference(self):
        with patch.object(live,'submit_known') as submit,self.assertRaises(live.UploadError):
            pilots.submit(self.pilot,'P01','B',io.BytesIO(b'x'),'attempt.mp4','changed-code',label_confirmed=True)
        submit.assert_not_called()
        self.assertFalse(pilots.report(self.pilot,'changed-code')['source_matches'])

    def test_frozen_plan_tampering_is_detected(self):
        data=storage.record(self.pilot);data['plan']['case_labels']['P02']='pass'
        storage._write(storage.directory(self.pilot)/'record.json',data)
        with self.assertRaises(live.UploadError):pilots.report(self.pilot,'frozen-code')

    def test_human_label_confirmation_is_required(self):
        with self.assertRaises(live.UploadError):
            pilots.submit(self.pilot,'P01','B',io.BytesIO(b'x'),'attempt.mp4','frozen-code')

    def test_first_submission_cannot_be_replaced_by_a_better_result(self):
        with patch.object(live,'submit_known'):
            pilots.submit(self.pilot,'P01','B',io.BytesIO(b'first'),'attempt.mp4','frozen-code',label_confirmed=True)
            with self.assertRaises(live.UploadError):
                pilots.submit(self.pilot,'P01','B',io.BytesIO(b'second'),'attempt.mp4','frozen-code',label_confirmed=True)

    def test_duplicate_recording_is_not_an_independent_attempt(self):
        with patch.object(live,'submit_known'):
            pilots.submit(self.pilot,'P01','B',io.BytesIO(b'same recording'),'attempt.mp4','frozen-code',label_confirmed=True)
            with self.assertRaises(live.UploadError):
                pilots.submit(self.pilot,'P07','B',io.BytesIO(b'same recording'),'attempt.mp4','frozen-code',label_confirmed=True)

    def test_review_and_false_accept_are_counted_separately(self):
        data=storage.record(self.pilot)
        simple=graph()
        passing=report_view(compare(simple,simple),simple)
        review={**passing,'passed':False,'verdict':'unsure','evidence_status':'inconclusive'}
        submissions={}
        for cid,result in [('P02',passing),('P06',review),('P09',passing)]:
            jid={'P02':'d','P06':'e','P09':'f'}[cid]*32;storage.create(jid)
            live._jobs[jid]={'status':'done','result':result}
            storage.update(jid,event='job_updated',job=live._jobs[jid])
            submissions[cid]={'job':jid,'participant':'B','submitted':time.time()-2,'retakes':0}
        storage.update(self.pilot,submissions=submissions)
        summary=pilots.report(self.pilot,'frozen-code')['summary']
        self.assertEqual(summary['false_accepts'],1)
        self.assertEqual(summary['reviews'],1)
        self.assertEqual(summary['review_cases_accepted'],1)
        self.assertEqual(summary['matched_expected'],1)
        self.assertEqual(summary['diagnostically_resolved'],0)

    def test_timing_has_no_invented_savings_and_keeps_waiting_separate(self):
        with patch.object(live,'submit_known'):
            pilots.submit(self.pilot,'P01','B',io.BytesIO(b'clip'),'attempt.mp4','frozen-code',label_confirmed=True)
        pilots.timing(self.pilot,'P01',20,7)
        summary=pilots.report(self.pilot,'frozen-code')['summary']
        self.assertEqual(summary['paired_review_timings'],1)
        self.assertEqual(summary['median_self_timed_review_difference_seconds'],13)
        self.assertIsNone(summary['median_model_wall_seconds'])

    def test_other_browser_cannot_read_trial(self):
        token=storage.owner.set('different-browser')
        try:
            with self.assertRaises(storage.AccessError):pilots.report(self.pilot,'frozen-code')
        finally:storage.owner.reset(token)
