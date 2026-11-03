"""Re-evaluate packaged fixtures under today's checker without fresh inference.

Historical live/repair reports remain unchanged. This artifact explicitly describes
an offline replay and records its current source fingerprint and every verdict.
"""
import json
from pathlib import Path

from oneshot.check import compare
from oneshot.schema import TaskGraph
from web.build_judging import MEDIA, DEMO, pipeline_digest
from web.live import builtin
from web.views import report_view


def refresh():
    ref, proc = builtin()
    original = json.loads((MEDIA / 'tracking_validation.json').read_text(encoding='utf-8'))
    rows = []
    for prior in original['results']:
        path = DEMO / 'judging' / f"{prior['id']}.graph.json"
        if not path.exists(): path = path.with_name(f"{prior['id']}.repaired.graph.json")
        att = TaskGraph.model_validate_json(path.read_text(encoding='utf-8'))
        report = compare(ref, att, procedure=proc)
        # This measures expected outcomes on the same development fixtures.
        kind = prior['kind']
        fail_types = {i.kind for i in report.issues if i.severity == 'fail'}
        expected = {'order':'order','skip_liner':'skipped','skip_card':'skipped','wrong_place':'wrong_place'}
        correct = (report.verdict == 'pass' if kind in ('ok','ok_alt') else
                   report.verdict == 'pass_notes' if kind == 'loose' else
                   report.verdict == 'unsure' if kind == 'hidden' else
                   report.verdict == 'fail' and expected[kind] in fail_types)
        rows.append({'id':prior['id'], 'kind':kind, 'correct':correct,
                     'result':report_view(report,att), 'original_verdict':prior['result']['verdict']})
    data = {'type':'offline development replay', 'fresh_model_inference':False,
            'pipeline_sha256':pipeline_digest(), 'historical_pipeline_sha256':original['pipeline_sha256'],
            'scope':'Same sixteen packaged generated fixtures, checked under the current acceptance policy.',
            'summary':{'total':len(rows),'matched_expected':sum(r['correct'] for r in rows),
                       'abstentions':sum(r['result']['verdict']=='unsure' for r in rows)},'results':rows}
    (MEDIA/'current_validation.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
    print('Current offline replay:',data['summary'])
    return data


if __name__ == '__main__': refresh()
