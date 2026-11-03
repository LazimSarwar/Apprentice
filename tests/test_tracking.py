"""Protocol and failure-isolation tests; no network calls."""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from meta_sam_parser import CompletedOutcome, ResponsesStreamSourceError, parse_responses_stream, video_segmentation_format

from oneshot import evidence
from oneshot.box_parser import BoxParser, BoxVideoFormat
from oneshot.graph import track_with_fallback
from oneshot.schema import Object
from test_reliability import graph
from oneshot.check import compare


MASK = "!!!!!&O.lVP"  # Canonical one-pixel mask generated once for this synthetic fixture.
LINE = f"<7f>0<|box;x1=2;y1=3;x2=4;y2=5;w=20;h=20;c=0.8|><|mask;x=0;y=0;data=1,1,{MASK}|>\n"


def events(text=LINE, terminal=True, deltas=True, text_done=True):
    lane = {"item_id": "fixture", "output_index": 0, "content_index": 0}
    out = []
    if deltas:
        for start in range(0, len(text), 11):
            out.append({"type": "response.output_text.delta", "delta": text[start:start + 11], **lane})
    if text_done:
        out.append({"type": "response.output_text.done", "text": text, **lane})
    out.append({"type": "response.content_part.done", "part": {"type": "output_text", "text": text}, **lane})
    if terminal:
        out.append({"type": "response.completed"})
    return out


class FakeStream:
    def __init__(self, data): self.data, self.closed = data, False
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    def __aiter__(self):
        async def source():
            for e in self.data: yield e
        return source()


async def parsed(data, format):
    async def source():
        for e in data: yield e
    async with parse_responses_stream(source(), format) as parser:
        return await parser.final_result()


class ParserTests(unittest.TestCase):
    def test_box_coordinates_exactly_match_official_parser(self):
        old = asyncio.run(parsed(events(), video_segmentation_format()))
        new = asyncio.run(parsed(events(), BoxVideoFormat()))
        self.assertEqual([b for b in old.records if b.kind == "box"], list(new.records))
        self.assertEqual(new.records[0].right, 5)  # Inclusive API edge becomes half-open.
        self.assertEqual(new.records[0].bottom, 6)

    def test_mask_decoder_is_never_called(self):
        with patch('meta_sam_parser._segmentation.decode_mask_to_raster', side_effect=AssertionError('unused pixels decoded')):
            result = asyncio.run(parsed(events(), BoxVideoFormat()))
        self.assertEqual(len(result.records), 1)

    def test_all_chunk_boundaries_and_multiple_objects(self):
        text = LINE.rstrip() + ",1<|box;w=20;h=20;y2=9;x2=9;y1=8;x1=8|><|mask;x=0;y=0;data=1,1," + MASK + "|>\n"
        for split in range(len(text) + 1):
            p = BoxParser(); p.push(text[:split]); p.push(text[split:])
            result = p.finish(CompletedOutcome()).result
            self.assertEqual([b.object_id for b in result.records], ['0', '1'])

    def test_final_text_without_deltas(self):
        result = asyncio.run(parsed(events(deltas=False), BoxVideoFormat()))
        self.assertEqual(len(result.records), 1)

    def test_invalid_geometry_and_protocol_are_rejected(self):
        for text in [LINE.replace('x2=4', 'x2=40'), LINE.replace('x1=2', 'x1=-2'),
                     LINE.replace('x1=2', 'x1=nan'), LINE.replace('x1=2', 'x1=2;x1=3'),
                     LINE.replace('<7f>', '<-1f>'), LINE.replace('|>\n', '\n'),
                     LINE.replace('0<|box', '<|box')]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                p = BoxParser(); p.push(text); p.finish(CompletedOutcome())


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_stream_closes_and_incomplete_response_is_rejected(self):
        stream = FakeStream(events(terminal=False))
        api = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=stream)))
        with self.assertRaises(evidence.IncompleteTrackingError):
            await evidence._track_one(api, 'file', 'red block', 30)
        self.assertTrue(stream.closed)

    async def test_legacy_content_part_finalizer_supported(self):
        stream = FakeStream(events(text_done=False))
        api = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=stream)))
        result = await evidence._track_one(api, 'file', 'red block', 30)
        self.assertEqual(result.boxes[0].frame, 7)
        self.assertTrue(stream.closed)

    async def test_transient_failure_retries_once(self):
        call = AsyncMock(side_effect=[TimeoutError(), None])
        with patch.object(evidence, '_track_one', call):
            self.assertIsNone(await evidence._track_retrying(None, 'file', 'red block', 30))
        self.assertEqual(call.await_count, 2)

    async def test_permanent_protocol_error_is_not_retried(self):
        call = AsyncMock(side_effect=ValueError('malformed'))
        with patch.object(evidence, '_track_one', call), self.assertRaises(ValueError):
            await evidence._track_retrying(None, 'file', 'red block', 30)
        self.assertEqual(call.await_count, 1)

    async def test_midstream_connection_reset_retries_once(self):
        call = AsyncMock(side_effect=[ResponsesStreamSourceError('next', OSError('reset')), None])
        with patch.object(evidence, '_track_one', call):
            self.assertIsNone(await evidence._track_retrying(None, 'file', 'red block', 30))
        self.assertEqual(call.await_count, 2)

    async def test_global_deadline_keeps_a_completed_object(self):
        async def partly_completed(clip, concepts, fps, file_id, batch):
            batch.tracks['good'] = graph(tracked=True).objects[0].track
            batch.completed.add('good')
            await asyncio.sleep(60)
        # The synchronous wrapper runs its own loop, so exercise it in a worker.
        with patch.object(evidence, '_track_all', partly_completed):
            batch = await asyncio.to_thread(evidence.track_objects, Path('unused'), ['good', 'slow'], 30,
                                            timeout_seconds=0.01)
        self.assertIsNotNone(batch.tracks['good'])
        self.assertNotIn('good', batch.errors)
        self.assertIn('slow', batch.errors)

    async def test_one_failed_object_preserves_other_completed_tracks(self):
        api = AsyncMock()
        api.__aenter__.return_value = api
        async def result(client, file, concept, fps):
            if concept == 'bad': raise ValueError('bad output')
            return graph(tracked=True).objects[0].track
        batch = evidence.TrackingBatch(tracks={'good': None, 'bad': None})
        with patch.object(evidence, 'AsyncOpenAI', return_value=api), \
             patch.object(evidence, 'require_env', return_value='fixture'), \
             patch.object(evidence, '_track_retrying', result):
            await evidence._track_all(Path('unused'), ['good', 'bad'], 30, 'already-uploaded', batch)
        self.assertIsNotNone(batch.tracks['good'])
        self.assertIn('good', batch.completed)
        self.assertIn('bad', batch.errors)


class GraphTrackingTests(unittest.TestCase):
    def test_fallback_reuses_upload_and_remaining_time_budget(self):
        primary = evidence.TrackingBatch(tracks={'red block': None}, completed={'red block'}, file_id='shared')
        fallback = evidence.TrackingBatch(tracks={'red rectangle': None}, completed={'red rectangle'})
        with patch('oneshot.graph.track_objects', side_effect=[primary, fallback]) as track:
            track_with_fallback(Path('unused'), [Object(id='red', color='red', shape='block')], 30)
        self.assertEqual(track.call_args_list[1].kwargs['file_id'], 'shared')
        self.assertLessEqual(track.call_args_list[1].kwargs['timeout_seconds'], 120)

    def test_failed_requests_do_not_start_colour_fallback(self):
        failed = evidence.TrackingBatch(tracks={'red block': None}, errors={'red block': 'timeout'})
        with patch('oneshot.graph.track_objects', return_value=failed) as track:
            batch = track_with_fallback(Path('unused'), [Object(id='red', color='red', shape='block')], 30)
        self.assertEqual(track.call_count, 1)
        self.assertIn('red', batch.errors)

    def test_failed_target_cannot_turn_into_a_partial_pass(self):
        att = graph(tracked=True).model_copy(deep=True)
        att.meta.tracking_errors['gray_tray'] = 'timeout'
        att.objects[1].track = None
        result = compare(graph(), att)
        self.assertEqual(result.verdict, 'unsure')
        self.assertFalse(result.passed)


class RepairEvidenceTests(unittest.TestCase):
    def test_published_repair_fingerprint_and_counts(self):
        from web.build_judging import APP, pipeline_digest
        report = json.loads((APP / 'web/static/media/tracking_validation.json').read_text(encoding='utf-8'))
        # Preserve the historical repair artifact; a distinct replay certifies new code.
        current = json.loads((APP / 'web/static/media/current_validation.json').read_text(encoding='utf-8'))
        self.assertEqual(current['pipeline_sha256'], pipeline_digest())
        self.assertEqual(current['historical_pipeline_sha256'], report['pipeline_sha256'])
        self.assertFalse(current['fresh_model_inference'])
        self.assertEqual(report['summary']['completed'], 16)
        self.assertEqual(report['summary']['matched_expected'], 16)
        self.assertEqual(report['summary']['pipeline_errors'], 0)
        self.assertEqual(report['summary']['object_tracking_errors'], 0)

    def test_all_packaged_repair_graphs_replay_offline(self):
        from web import live
        from oneshot.schema import TaskGraph
        ref, proc = live.builtin()
        report = json.loads((live.WEB / 'static/media/tracking_validation.json').read_text(encoding='utf-8'))
        for row in report['results']:
            path = live.DEMO / 'judging' / f"{row['id']}.graph.json"
            if not path.exists(): path = path.with_name(f"{row['id']}.repaired.graph.json")
            att = TaskGraph.model_validate_json(path.read_text(encoding='utf-8'))
            with self.subTest(case=row['id']):
                result = compare(ref, att, procedure=proc)
                self.assertEqual(result.verdict, row['result']['verdict'])
                self.assertEqual(att.meta.tracking_errors, {})
