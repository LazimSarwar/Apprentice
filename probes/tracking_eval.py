"""Recheck the original sixteen-case batch after repairing tracking.

Fourteen successful extraction caches are replayed; the two previous timeout cases
receive fresh extraction and tracking. The original report is retained separately.
This is a development repair check, not sixteen new unseen examples.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import sys
import time
from pathlib import Path

from common import ROOT
from pack_eval import correct
from make_pack_clip import make

sys.path.insert(0, str(ROOT))
from oneshot.check import compare
from oneshot.graph import add_evidence, build_task_graph
from oneshot.procedure import Procedure
from oneshot.schema import TaskGraph
from web.views import report_view


def pipeline_digest():
    from web.build_judging import pipeline_digest as digest
    return digest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-tracks', action='store_true', help='Replay the completed repair tracks without new model calls')
    args = parser.parse_args()
    old_dir = ROOT / 'runs/judging_eval'
    previous = json.loads((ROOT / 'web/static/media/judging_eval.json').read_text(encoding='utf-8'))
    plan_path = old_dir / 'plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8')) if plan_path.exists() else previous['plan']
    fixtures = ROOT / 'web/demo/judging'
    work = ROOT / 'runs/tracking_repair'
    work.mkdir(parents=True, exist_ok=True)
    ref = TaskGraph.model_validate_json((ROOT / 'web/reference.graph.json').read_text(encoding='utf-8'))
    proc = Procedure.from_dict(json.loads((ROOT / 'web/reference.procedure.json').read_text(encoding='utf-8')))
    known = [{'id': o.id, 'color': o.color, 'shape': o.shape} for o in ref.objects]
    fingerprint = pipeline_digest()
    rows = []
    for case in plan['cases']:
        start = time.monotonic()
        row = dict(case)
        try:
            cache = (fixtures if args.reuse_tracks else old_dir) / f"{case['id']}.graph.json"
            if not cache.exists(): cache = fixtures / f"{case['id']}.graph.json"
            if cache.exists():
                att = TaskGraph.model_validate_json(cache.read_text(encoding='utf-8'))
                row['extraction'] = 'replayed original cache'
            else:
                tracked = work / f"{case['id']}.tracked.graph.json"
                if args.reuse_tracks:
                    tracked = fixtures / f"{case['id']}.repaired.graph.json"
                if not tracked.exists() and args.reuse_tracks:
                    tracked = fixtures / f"{case['id']}.repaired.graph.json"
                raw = work / f"{case['id']}.untracked.graph.json"
                if args.reuse_tracks and tracked.exists():
                    att = TaskGraph.model_validate_json(tracked.read_text(encoding='utf-8'))
                    row['tracking_request'] = 'replayed completed repair tracking'
                elif raw.exists():
                    att = TaskGraph.model_validate_json(raw.read_text(encoding='utf-8'))
                else:
                    if args.reuse_tracks:
                        raise RuntimeError('Offline repair fixture missing; run without --reuse-tracks to rebuild it')
                    clip = old_dir / f"{case['id']}.mp4"
                    if not clip.exists():
                        clip.parent.mkdir(parents=True, exist_ok=True)
                        make(case['kind'], clip, seed=case['seed'])
                    print(case['id'], 'extracting', flush=True)
                    att = build_task_graph(clip, work_dir=work,
                                           known_objects=known, track=False)
                    raw.write_text(att.model_dump_json(), encoding='utf-8')
                if row.get('tracking_request') != 'replayed completed repair tracking':
                    clip = work / 'clips' / f"{case['id']}.720p.mp4"
                    if not clip.exists():
                        from oneshot.media import small_mp4
                        source = old_dir / f"{case['id']}.mp4"
                        if not source.exists(): make(case['kind'], source, seed=case['seed'])
                        clip = small_mp4(source, work / 'clips')
                    print(case['id'], 'tracking with box parser', flush=True)
                    att = add_evidence(att, clip)
                    tracked.write_text(att.model_dump_json(), encoding='utf-8')
                    row['tracking_request'] = 'new live tracking'
                row['extraction'] = 'new extraction of former timeout case'
            report = compare(ref, att, procedure=proc)
            row.update(correct=correct(case['kind'], report), result=report_view(report, att),
                       tracking_errors=att.meta.tracking_errors, tracking_seconds=att.meta.track_seconds)
        except Exception as exc:
            row.update(correct=False, error=f'{type(exc).__name__}: {str(exc)[:200]}')
        row['seconds'] = round(time.monotonic() - start, 2)
        rows.append(row)
        assert pipeline_digest() == fingerprint, 'Pipeline changed during verification'
        report = {'type': 'development repair replay', 'pipeline_sha256': fingerprint,
                  'previous_summary': previous['summary'], 'original_plan': plan,
                  'scope': 'Same sixteen generated clips: fourteen extraction caches replayed, two former timeout cases extracted and tracked again.',
                  'results': rows, 'summary': {'total': len(plan['cases']), 'completed': len(rows),
                  'matched_expected': sum(r['correct'] for r in rows),
                  'pipeline_errors': sum('error' in r for r in rows),
                  'object_tracking_errors': sum(len(r.get('tracking_errors', {})) for r in rows),
                  'abstentions': sum(r.get('result', {}).get('verdict') == 'unsure' for r in rows)}}
        (work / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(case['id'], row.get('result', {}).get('verdict', row.get('error')), row['seconds'], flush=True)
    (ROOT / 'web/static/media/tracking_validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('SUMMARY', report['summary'], flush=True)


if __name__ == '__main__':
    main()
