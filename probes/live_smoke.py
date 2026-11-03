"""Exercise actual uploaded-video inference and tracking on a running deployment.

python probes/live_smoke.py --base-url https://YOUR-APP --cases correct correct wrong-place hidden
Calls the real models. No sample-result cache is used by the upload endpoint.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx2

ROOT = Path(__file__).resolve().parent.parent
CASES = {'correct': ('real_ok_3', 'real', {'pass', 'pass_notes'}),
         'wrong-place': ('real_wrongplace_1', 'real', {'fail'}),
         'hidden': ('pack_hidden', 'pack', {'unsure'})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=['correct', 'correct', 'wrong-place', 'hidden'])
    parser.add_argument('--out', type=Path, default=ROOT / 'runs/live_smoke.json')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    rows = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with httpx2.Client(timeout=30) as client:
        for case in args.cases:
            stem, reference, expected = CASES[case]
            clip = ROOT / f'web/static/media/{stem}_demo.mp4'
            start = time.monotonic()
            with clip.open('rb') as f:
                response = client.post(base + '/api/check', data={'reference_demo': reference},
                                       files={'attempt': (clip.name, f, 'video/mp4')})
            response.raise_for_status()
            job_id = response.json()['job']
            history, previous, network_failures = [], None, 0
            while time.monotonic() - start < 240:
                try:
                    response = client.get(base + '/api/jobs/' + job_id)
                except httpx2.TimeoutException:
                    network_failures += 1
                    if network_failures >= 3: raise
                    continue
                response.raise_for_status()
                job = response.json()
                state = (job['status'], job.get('stage'), job.get('result', {}).get('verdict'))
                if state != previous:
                    history.append({'seconds': round(time.monotonic() - start, 2), 'state': state})
                    print(case, history[-1], flush=True)
                    previous = state
                if job['status'] in ('done', 'error'): break
                time.sleep(1)
            else:
                raise TimeoutError(f'{case} did not complete in four minutes')
            row = {'case': case, 'seconds': round(time.monotonic() - start, 2), 'job': job_id,
                   'history': history, 'result': job.get('result'), 'status': job['status'], 'seen': job.get('seen')}
            rows.append(row)
            args.out.write_text(json.dumps({'type': 'live deployment smoke test', 'base_url': base,
                                           'results': rows}, indent=2), encoding='utf-8')
            assert job['status'] == 'done', job.get('error')
            result = job['result']
            assert result['verdict'] in expected, (case, result)
            assert result['tracking']['parser'] == 'sam_boxes_v1'
            assert result['tracking']['errors'] == {}, result['tracking']
            assert result['tracking']['objects_tracked'] > 0
            assert result['tracking_used'] and job.get('seen')
            response = client.get(base + (job['seen'] if job['seen'].startswith('/') else '/static/' + job['seen']))
            response.raise_for_status()
            print('PASS', case, row['seconds'], result['verdict'], result['tracking'], flush=True)
    print('LIVE_SMOKE_COMPLETE', len(rows), 'of', len(args.cases), flush=True)


if __name__ == '__main__':
    main()
