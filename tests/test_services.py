"""Offline lifecycle, accounting, private API and real-media cache regressions."""
import asyncio
import io
import json
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi.testclient import TestClient
from oneshot.clients import ConfigurationError, record_usage
from oneshot.media import small_mp4
from oneshot.procedure import learn, Rule
from oneshot.sim.executor import execute
from web import budget, live, storage, watch
from web.server import app
from test_reliability import graph


class IsolatedServices(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, key, value in ((budget,"FILE",self.root/"budget.json"),
                                  (storage,"ROOT",self.root/"private"),
                                  (live,"_jobs",{}),(live,"_graphs",{}),(live,"_pending",{}),
                                  (watch,"_sessions",{})):
            mocked=patch.object(target,key,value);mocked.start();self.addCleanup(mocked.stop)
        self.job = 'a'*32

    def create(self):
        return storage.create(self.job)


class BudgetTests(IsolatedServices):
    def test_tracking_retries_are_each_accounted(self):
        from oneshot import evidence
        api=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=TimeoutError('provider timeout'))))
        with self.assertRaises(TimeoutError):
            with budget.call('tracking retry estimate',.4):
                with patch.object(evidence.asyncio,'sleep',new=AsyncMock()):
                    asyncio.run(evidence._track_retrying(api,'fixture','red block',30))
        self.assertEqual(api.responses.create.await_count,2)
        # Interrupted work conservatively retains its entire reservation.
        self.assertEqual(budget.spent(),.4)
        self.assertTrue(json.loads(budget.FILE.read_text())['events'][-1]['estimated'])

    def test_reservations_exclude_other_inflight_work(self):
        with patch.object(budget,"CAP_USD",1):
            with budget.call("first",.75) as meter:
                self.assertEqual(budget.status()['reserved_usd'],.75)
                with self.assertRaises(budget.BudgetError):
                    with budget.call("second",.75): pass
                meter.observe(.10)
            self.assertEqual(budget.spent(),.10)
            self.assertEqual(budget.status()['reserved_usd'],0)

    def test_invalid_model_output_does_not_erase_usage(self):
        with self.assertRaises(ValueError):
            with budget.call("invalid output",.5):
                record_usage(.02)
                raise ValueError("invalid JSON after billed response")
        self.assertEqual(budget.spent(),.5)

    def test_unknown_usage_keeps_reservation(self):
        with budget.call("unknown",.5): record_usage(None)
        self.assertEqual(budget.spent(),.5)

    def test_corrupt_ledger_fails_closed(self):
        budget.FILE.write_text('{broken')
        with self.assertRaises(budget.BudgetError): budget.ensure()
        self.assertEqual(budget.FILE.read_text(),'{broken')

    def test_restart_accounts_for_interrupted_requests(self):
        budget.FILE.write_text(json.dumps({'spent':1,'reserved':{'request':.5},'events':[]}))
        budget.recover_reservations()
        self.assertEqual(budget.spent(),1.5)
        self.assertEqual(budget.status()['reserved_usd'],0)


class LifecycleTests(IsolatedServices):
    def test_missing_key_worker_reaches_terminal_error(self):
        folder=self.create()
        live._jobs[self.job]={'status':'queued'}
        with patch.object(live,'_check_length',return_value=3), \
             patch.object(live,'builtin',return_value=(graph(),learn([graph()]))), \
             patch.object(live,'build_task_graph',side_effect=ConfigurationError('missing key')), \
             patch.object(live.traceback,'print_exc'):
            live._run(self.job,folder/'unused.mp4',None,None)
        self.assertEqual(live.get(self.job)['status'],'error')

    def test_empty_simulation_is_incomplete(self):
        g=graph().model_copy(update={'steps':[],'final_relations':[]})
        with patch('oneshot.sim.executor.build_scene',return_value=SimpleNamespace()), \
             patch('oneshot.sim.executor.Panda',return_value=SimpleNamespace(frames=[])):
            result=execute(g)
        self.assertFalse(result.success)
        self.assertFalse(result.completed)

    def test_all_dropped_goals_cannot_become_empty_success(self):
        from oneshot.schema import Relation
        g=graph().model_copy(update={'steps':[], 'final_relations':[
            Relation(relation='inside',a='red_block',b='gray_tray')]})
        with patch('oneshot.sim.executor.build_scene') as scene:
            result=execute(g)
        scene.assert_not_called()
        self.assertFalse(result.success)

    def test_custom_reference_waits_for_human_confirmation(self):
        folder=self.create();ref=graph();proc=learn([ref])
        attempt=folder/'attempt.mp4';reference=folder/'reference.mp4'
        live._jobs[self.job]={'status':'queued'}
        with patch.object(live,'_check_length'),patch.object(live,'_learn_own',return_value=(ref,proc)), \
             patch.object(live,'_continue') as continuation:
            live._run(self.job,attempt,reference,None)
        continuation.assert_not_called()
        self.assertEqual(live.get(self.job)['status'],'awaiting_confirmation')
        self.assertEqual(storage.record(self.job)['procedure'],json.loads(json.dumps(proc.to_dict())))

    def test_camera_finalization_uses_shared_tracking_gate(self):
        folder=self.create();ref=graph();proc=learn([ref])
        s=watch.Session(self.job,'camera',ref,proc,folder/'camera.webm',video='fixture.mp4')
        tracked=graph(tracked=True)
        expected={'verdict':'pass','tracking_used':True}
        with patch.object(watch,'cut',return_value=folder/'whole.mp4'), \
             patch.object(live,'_check_length'),patch.object(live,'_extract',return_value=ref), \
             patch.object(watch,'small_mp4',return_value=folder/'small.mp4'), \
             patch.object(live,'finalize',return_value=(tracked,expected,'seen.mp4',None)) as gate:
            watch._finish(s)
        gate.assert_called_once()
        self.assertEqual(s.final,expected)
        self.assertEqual(s.status,'done')

    def test_camera_limit_rejects_append_without_writing(self):
        folder=self.create()
        s=watch.Session(self.job,'camera',graph(),learn([graph()]),folder/'camera.webm',started=time.monotonic()-live.MAX_SECONDS-1)
        watch._sessions[self.job]=s
        with self.assertRaises(live.UploadError):watch.append(self.job,b'chunk')
        self.assertFalse(s.src.exists())

    def test_read_limit_does_not_allocate_snapshot(self):
        folder=self.create();src=folder/'camera.webm';src.write_bytes(b'camera')
        s=watch.Session(self.job,'camera',graph(),learn([graph()]),src,reads=watch.MAX_READS)
        watch._sessions[self.job]=s
        with patch.object(watch.shutil,'copyfile') as copy:
            watch.read(self.job)
        copy.assert_not_called()
        self.assertEqual(s.seq,0)

    def test_cancelling_session_prevents_new_work(self):
        folder=self.create();s=watch.Session(self.job,'camera',graph(),learn([graph()]),folder/'camera.webm')
        watch._sessions[self.job]=s;watch.cancel(self.job)
        with patch.object(watch._pool,'submit') as submit:watch.read(self.job)
        submit.assert_not_called()

    def test_session_admission_limit_is_enforced(self):
        self.create()
        for i in range(watch.MAX_ACTIVE):
            sid=str(i)*32
            storage.create(sid)
            watch._sessions[sid]=watch.Session(sid,'camera',graph(),learn([graph()]),self.root/f'{i}.webm')
        with self.assertRaises(live.UploadError):watch.start('camera')

    def test_concurrent_session_admission_reserves_slots_atomically(self):
        def start(_):
            try: return watch.start('camera')['session']
            except live.UploadError: return None
        with ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(start,range(6)))
        self.assertEqual(sum(r is not None for r in results),watch.MAX_ACTIVE)

    def test_private_storage_quota_is_enforced_before_upload_write(self):
        with patch.object(storage,'MAX_STORAGE_BYTES',1),self.assertRaises(ValueError):
            live.save_upload(io.BytesIO(b'video'),'attempt.mp4',self.job,'attempt')
        self.assertFalse((storage.directory(self.job)/'raw'/'attempt.mp4').exists())

    def test_expiry_evicts_private_files_and_memory(self):
        folder=self.create();(folder/'clip.mp4').write_bytes(b'private')
        live._jobs[self.job]={'status':'done'}
        data=storage.record(self.job);data['expires']=time.time()-1
        storage._write(folder/'record.json',data)
        live.cleanup()
        self.assertFalse(folder.exists())
        self.assertNotIn(self.job,live._jobs)

    def test_procedure_confirmation_resumes_saved_attempt(self):
        folder=self.create();ref=graph();proc=learn([ref])
        live._jobs[self.job]={'status':'awaiting_confirmation'}
        live._pending[self.job]=(folder/'attempt.mp4',ref,proc)
        with patch.object(live._pool,'submit') as submit:
            live.confirm(self.job,{'procedure':proc.to_dict()})
        self.assertTrue(live.get(self.job)['procedure_confirmed'])
        submit.assert_called_once()
        self.assertEqual(storage.record(self.job)['history'][-2]['event'],'procedure_confirmed')

    def test_worker_update_after_restart_preserves_reference_metadata(self):
        self.create()
        live._jobs[self.job]={'status':'awaiting_confirmation','custom_reference':True,'reference_demo':'real'}
        live._set(self.job,stage='Confirm')
        live._jobs.clear()
        live._set(self.job,status='queued')
        self.assertTrue(live.get(self.job)['custom_reference'])
        self.assertEqual(live.get(self.job)['reference_demo'],'real')

    def test_invalid_or_cyclic_procedure_is_rejected(self):
        proc=learn([graph()]);step=proc.steps[0].key
        proc.pairs=[Rule(step,step,'required','you','invalid')]
        with self.assertRaises(live.UploadError):live.validate_procedure(proc,graph())

    def test_followup_retains_confirmed_procedure_and_parent_history(self):
        self.create();ref=graph();proc=learn([ref])
        live._jobs[self.job]={'status':'done','procedure_confirmed':True}
        live._graphs[self.job]=(ref,ref,proc)
        child='b'*32;folder=storage.create(child)
        with patch.object(live._pool,'submit') as submit:
            live.submit_followup(self.job,child,folder/'attempt.mp4')
        self.assertTrue(live.get(child)['procedure_confirmed'])
        self.assertEqual(storage.record(child)['parent_job'],self.job)
        self.assertEqual(storage.record(self.job)['history'][-1]['event'],'followup_requested')
        submit.assert_called_once()


class PrivateApiTests(IsolatedServices):
    def setUp(self):
        super().setUp()
        self.client=TestClient(app)
        self.client.get('/')  # Establish a browser owner cookie.
        self.token=storage.owner.set(self.client.cookies.get('oneshot_owner'))
        self.addCleanup(storage.owner.reset,self.token)

    def test_recent_checks_are_private_and_survive_memory_reset(self):
        self.create()
        live._set(self.job, status='done', result={'verdict':'unsure'})
        live._jobs.clear()
        response=self.client.get('/api/jobs')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['checks'][0]['verdict'],'unsure')
        self.assertNotIn('owner',response.text)
        other=TestClient(app)
        self.assertEqual(other.get('/api/jobs').json()['checks'],[])

    def test_recent_checks_exclude_expired_records_and_reference_only_jobs(self):
        self.create();live._set(self.job,status='ready',procedure_only=True)
        expired='b'*32;storage.create(expired);live._set(expired,status='done')
        storage.update(expired,expires=time.time()-1)
        self.assertEqual(self.client.get('/api/jobs').json()['checks'],[])

    def test_download_retains_machine_assessment_and_all_human_reviews(self):
        self.create()
        machine={'verdict':'unsure','verified':False}
        live._set(self.job,status='done',result=machine)
        for decision in ('needs_evidence','accept'):
            response=self.client.post(f'/api/jobs/{self.job}/review',json={'decision':decision,'note':'Inspected recording'})
            self.assertEqual(response.status_code,200)
        report=self.client.get(f'/api/jobs/{self.job}/report').json()
        self.assertEqual(report['machine_assessment'],machine)
        self.assertEqual(report['human_review']['decision'],'accept')
        self.assertEqual([r['decision'] for r in report['human_reviews']],['needs_evidence','accept'])
        self.assertNotIn('owner',report)
        self.assertEqual(TestClient(app).get(f'/api/jobs/{self.job}/report').status_code,404)

    def test_download_rejects_unfinished_check(self):
        self.create();live._set(self.job,status='running')
        self.assertEqual(self.client.get(f'/api/jobs/{self.job}/report').status_code,409)

    def test_other_browser_cannot_read_job_or_video(self):
        folder=self.create();(folder/'encoded').mkdir();(folder/'encoded'/'clip.mp4').write_bytes(b'private')
        live._jobs[self.job]={'status':'done'}
        other=TestClient(app)
        self.assertEqual(other.get(f'/api/jobs/{self.job}').status_code,404)
        self.assertEqual(other.get(f'/api/assets/{self.job}/clip.mp4').status_code,404)
        response=self.client.get(f'/api/assets/{self.job}/clip.mp4')
        self.assertEqual(response.content,b'private')
        self.assertIn('no-store',response.headers['cache-control'])

    def test_legacy_static_upload_urls_are_blocked(self):
        self.assertEqual(self.client.get('/static/uploads/anything.mp4').status_code,404)

    def test_json_requests_have_a_smaller_limit_than_video_uploads(self):
        response=self.client.post('/api/recheck',content=b'{}',headers={'Content-Length':str(2*1024*1024)})
        self.assertEqual(response.status_code,413)

    def test_completed_job_can_be_deleted_with_its_artifacts(self):
        folder=self.create();(folder/'recording').write_bytes(b'data')
        live._jobs[self.job]={'status':'done'}
        response=self.client.delete(f'/api/jobs/{self.job}')
        self.assertEqual(response.status_code,200)
        self.assertFalse(folder.exists())

    def test_failed_second_upload_removes_the_first_upload(self):
        with patch.object(live,'ensure_capacity'):
            response=self.client.post('/api/check',files={
                'attempt':('attempt.mp4',b'video','video/mp4'),
                'reference':('bad.txt',b'bad','text/plain')})
        self.assertEqual(response.status_code,400)
        self.assertFalse(any(storage.ROOT.rglob('attempt.mp4')))

    def test_human_review_never_changes_machine_verification(self):
        self.create();live._jobs[self.job]={'status':'done','result':{'passed':False,'verified':False}}
        response=self.client.post(f'/api/jobs/{self.job}/review',json={'decision':'accept','note':'Inspected independently'})
        self.assertEqual(response.status_code,200)
        self.assertFalse(live.get(self.job)['result']['verified'])
        self.assertEqual(storage.record(self.job)['human_review']['source'],'human')

    def test_completed_session_event_stream_closes_with_final_state(self):
        folder=self.create();s=watch.Session(self.job,'camera',graph(),learn([graph()]),folder/'camera.webm',status='done')
        watch._sessions[self.job]=s
        response=self.client.get(f'/api/watch/{self.job}/events')
        self.assertEqual(response.status_code,200)
        self.assertIn('"status": "done"',response.text)
        self.assertIn('text/event-stream',response.headers['content-type'])


class MediaCacheTests(IsolatedServices):
    def test_same_basename_and_changed_content_get_distinct_caches(self):
        left=self.root/'left';right=self.root/'right';left.mkdir();right.mkdir()
        a=left/'attempt.mp4';b=right/'attempt.mp4';a.write_bytes(b'A');b.write_bytes(b'B')
        def encode(command,**kwargs):Path(command[-1]).write_bytes(b'encoded')
        with patch('oneshot.media.subprocess.run',side_effect=encode):
            pa=small_mp4(a,self.root/'cache');pb=small_mp4(b,self.root/'cache')
            a.write_bytes(b'new');pc=small_mp4(a,self.root/'cache')
        self.assertEqual(len({pa,pb,pc}),3)

    def test_timeout_never_publishes_a_partial_cache(self):
        video=self.root/'attempt.mp4';video.write_bytes(b'input')
        def fail(command,**kwargs):
            self.assertGreater(kwargs['timeout'],0)
            Path(command[-1]).write_bytes(b'partial')
            raise subprocess.TimeoutExpired('ffmpeg',1)
        with patch('oneshot.media.subprocess.run',side_effect=fail),self.assertRaises(subprocess.TimeoutExpired):
            small_mp4(video,self.root/'cache')
        self.assertEqual(list((self.root/'cache').iterdir()),[])
