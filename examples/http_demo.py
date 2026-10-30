"""Exercise the OpenAI SDK adapter against a local rule-based simulator, not an LLM."""
import json
import os
import tempfile
from pathlib import Path

from examples.fake_model_service import local_model_service
from src.engine import run


def generate():
    out_dir=Path('examples/outputs')
    keys=('OPENAI_API_KEY','OPENAI_MODEL','OPENAI_BASE_URL','OPENAI_TIMEOUT_SECONDS')
    original={key:os.environ.get(key) for key in keys}
    cases=(('http_schedule_busy',{'material_id':'M01','date':'2026-11-01','time':'18:00'}),
           ('http_schedule_free',{'material_id':'M01','date':'2026-11-01','time':'20:00'}))
    try:
        with local_model_service() as server, tempfile.TemporaryDirectory() as temp:
            os.environ.update({'OPENAI_API_KEY':'local-test-key','OPENAI_MODEL':'local-simulator',
                               'OPENAI_BASE_URL':f'http://127.0.0.1:{server.server_port}/v1',
                               'OPENAI_TIMEOUT_SECONDS':'5'})
            for name,request in cases:
                start=len(server.received)
                output=run('schedule',request,model='real',runtime=str(Path(temp)/name))
                rounds=[]
                for received in server.received[start:]:
                    payload=received['payload']
                    rounds.append({'remaining_calls':payload['remaining_calls'],
                                   'last_observation':payload['observations'][-1] if payload['observations'] else None,
                                   'decision':received['decision']})
                result={'service_kind':'local_http_simulator','task':'schedule',
                        'notice':'决策由离线规则产生，真实 SDK HTTP 通信；不是大模型决策效果证据。',
                        'request':request,'rounds':rounds,'output':output}
                (out_dir/f'{name}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    finally:
        for key,value in original.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value


if __name__ == '__main__':
    generate()
