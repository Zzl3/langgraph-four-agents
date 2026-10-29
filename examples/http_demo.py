"""Generate SDK-over-HTTP traces against a local simulated service, not a real model."""
import json
import os
import tempfile
from pathlib import Path

from src.engine import run
from examples.fake_model_service import local_model_service


def generate():
    out_dir=Path('examples/outputs')
    keys=('OPENAI_API_KEY','OPENAI_MODEL','OPENAI_BASE_URL','OPENAI_TIMEOUT_SECONDS')
    original={key:os.environ.get(key) for key in keys}
    cases=[('http_schedule_busy',{'material_id':'M01','date':'2026-11-01','time':'18:00'}),
           ('http_schedule_free',{'material_id':'M01','date':'2026-11-01','time':'20:00'})]
    try:
        with local_model_service() as server, tempfile.TemporaryDirectory() as temp:
            os.environ['OPENAI_API_KEY']='local-test-key'
            os.environ['OPENAI_MODEL']='local-simulator'
            os.environ['OPENAI_BASE_URL']=f'http://127.0.0.1:{server.server_port}/v1'
            os.environ['OPENAI_TIMEOUT_SECONDS']='5'
            for name,request in cases:
                start=len(server.received)
                output=run('schedule',request,model='real',runtime=str(Path(temp)/name))
                rounds=[]
                for received in server.received[start:]:
                    payload=received['payload']
                    rounds.append({'remaining_calls':payload['remaining_calls'],
                                   'last_observation':payload['observations'][-1] if payload['observations'] else None,
                                   'decision':received['decision']})
                result={'service_kind':'local_http_simulator',
                        'notice':'决策由离线规则产生，真实 SDK HTTP 通信；不是大模型决策效果证据。',
                        'request':request,'rounds':rounds,'output':output}
                (out_dir/f'{name}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    finally:
        for key,value in original.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value

if __name__ == '__main__':
    generate()
