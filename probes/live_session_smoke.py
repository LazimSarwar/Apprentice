"""Stream a recorded fixture through the camera transport and real Meta services.

This is an integration test, not a test of a person using a webcam or an accuracy benchmark.
Run against a local server with an explicit model-spend cap.
"""
import argparse
import json
import math
import subprocess
import time
from pathlib import Path
import sys

import httpx2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from web.build_judging import pipeline_digest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--out',type=Path,default=ROOT/'runs/live_session_smoke.json')
    args=parser.parse_args()
    work=ROOT/'runs/session_smoke';work.mkdir(parents=True,exist_ok=True)
    clip=work/'recorded-camera.webm'
    subprocess.run(['ffmpeg','-y','-v','error','-i',str(ROOT/'web/static/media/real_ok_3_demo.mp4'),
                    '-vf','scale=640:-2','-an','-c:v','libvpx','-b:v','600k',str(clip)],check=True,timeout=30)
    data=clip.read_bytes();size=math.ceil(len(data)/6)
    start=time.monotonic();history=[]
    with httpx2.Client(timeout=30) as client:
        client.get(args.base_url+'/').raise_for_status()
        health=client.get(args.base_url+'/api/health');health.raise_for_status()
        build=health.json()['pipeline_sha256']
        assert build==pipeline_digest(),'Restart the server to test the current source tree'
        response=client.post(args.base_url+'/api/watch',data={'mode':'camera','reference_demo':'real'})
        response.raise_for_status();sid=response.json()['session']
        for n in range(6):
            response=client.post(f'{args.base_url}/api/watch/{sid}/chunk',content=data[n*size:(n+1)*size],headers={'Content-Type':'video/webm'})
            response.raise_for_status()
            if n in (2,5):client.post(f'{args.base_url}/api/watch/{sid}/read',json={}).raise_for_status()
            time.sleep(1)
        client.post(f'{args.base_url}/api/watch/{sid}/finish',json={}).raise_for_status()
        previous=None
        while time.monotonic()-start<240:
            response=client.get(f'{args.base_url}/api/watch/{sid}');response.raise_for_status();state=response.json()
            phase=(state['status'],state['reads'],state['reading'],state['failed'])
            if phase!=previous:
                history.append({'seconds':round(time.monotonic()-start,2),'state':phase})
                print(history[-1],flush=True);previous=phase
            if state['status'] in ('done','error'):break
            time.sleep(1)
        assert state['status']=='done',state.get('error')
        assert state['failed']==0,state
        assert state['final']['tracking_used'],state['final']
        assert state['final']['tracking']['errors']=={},state['final']['tracking']
        assert state['seen'],state
        overlay=client.get(args.base_url+state['seen']);overlay.raise_for_status()
        assert len(overlay.content)>1000
        other=httpx2.get(args.base_url+state['seen'])
        assert other.status_code==404,'Private recording was visible without the owner cookie'
        proof={'type':'live Meta integration with recorded camera transport','fresh_model_inference':True,
            'scope':'One existing public phone clip streamed as WebM; not a real-person webcam or accuracy benchmark.',
            'pipeline_sha256':build,'seconds':round(time.monotonic()-start,2),
            'history':history,'reads':state['reads'],'failed_reads':state['failed'],'result':state['final'],
            'overlay_bytes':len(overlay.content),'unauthorized_overlay_status':other.status_code,
            'session':sid,'status':state['status']}
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(proof,indent=2),encoding='utf-8')
        print('LIVE_SESSION_COMPLETE',proof['seconds'],state['final']['verdict'],flush=True)


if __name__=='__main__':main()
