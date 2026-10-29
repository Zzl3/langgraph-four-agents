"""End-to-end SDK transport against a local simulator, not an LLM evaluation."""
from src.engine import run
from examples.fake_model_service import local_model_service


def configure(monkeypatch, server):
    monkeypatch.setenv('OPENAI_API_KEY','local-test-key')
    monkeypatch.setenv('OPENAI_MODEL','local-simulator')
    monkeypatch.setenv('OPENAI_BASE_URL',f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS','5')


def test_http_sdk_sends_observations_for_different_schedule_paths(tmp_path, monkeypatch):
    with local_model_service() as server:
        configure(monkeypatch,server)
        busy = run('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00'},
                   model='real',runtime=str(tmp_path/'busy'))
        free = run('schedule',{'material_id':'M01','date':'2026-11-01','time':'20:00'},
                   model='real',runtime=str(tmp_path/'free'))
        assert busy['result']['status']=='pending_confirmation' and busy['result']['suggested_time']=='19:00'
        assert free['result']['status']=='ready' and free['result']['suggested_time']=='20:00'
        saves = [r for r in server.received if r['decision'] and r['decision']['action']=='save_draft']
        assert len(saves)==2
        assert [r['decision']['args']['draft']['suggested_time'] for r in saves]==['19:00','20:00']
        for entry in saves:
            assert entry['body']['response_format']=={'type':'json_object'}
            assert any(x['action']=='query_slots' for x in entry['payload']['observations'])


def test_http_provider_error_preserves_prior_tool_trace(tmp_path, monkeypatch):
    with local_model_service(fail_after_observations=1) as server:
        configure(monkeypatch,server)
        output = run('qa',{'question':'报销期限多久'},model='real',runtime=str(tmp_path))
        assert output['result']['status']=='incomplete' and '决策器失败' in output['result']['reason']
        assert len(output['trace'])==1 and output['trace'][0]['action']=='search_docs'
        assert server.received[-1]['payload']['required_query']=='报销期限多久'


def test_http_sdk_runs_remaining_three_tasks(tmp_path, monkeypatch):
    with local_model_service() as server:
        configure(monkeypatch,server)
        plan=run('plan',{'theme':'字幕活动','budget':10,'deadline_hours':4},model='real',runtime=str(tmp_path/'plan'))
        qa=run('qa',{'question':'报销期限多久'},model='real',runtime=str(tmp_path/'qa'))
        video=run('video',{'topic':'字幕','audience':'创作者','platform':'短视频','duration':30,'style':'简洁'},
                  model='real',runtime=str(tmp_path/'video'))
        assert plan['result']['status']=='completed' and plan['result']['plan']['cost']==8
        assert qa['result']['status']=='conflict' and len(qa['result']['citations'])==2
        assert video['result']['status']=='completed' and video['result']['seconds']==28
        observations=[item['payload'] for item in server.received]
        assert any(item['task']=='plan' and any(x['observation'].get('cost')==12 for x in item['observations']) for item in observations)
        assert any(item['task']=='video' and any(x['observation'].get('seconds')==38 for x in item['observations']) for item in observations)
        assert all(item.get('required_query')=='报销期限多久' for item in observations if item['task']=='qa')
        qa_payloads=[item for item in observations if item['task']=='qa']
        assert qa_payloads[1]['required_document_reads']==['D01','D02']
        assert qa_payloads[2]['required_document_reads']==['D02']
        assert qa_payloads[3]['evidence_status']=={
            'all_matches_read':True,'possible_conflict':True,'read_document_ids':['D01','D02']}
        assert qa_payloads[3]['next_required_actions']==[]


def test_http_timeout_is_explicit_and_bounded(tmp_path, monkeypatch):
    with local_model_service(delay_seconds=0.5) as server:
        configure(monkeypatch,server)
        monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS','0.05')
        result=run('qa',{'question':'报销期限多久'},model='real',runtime=str(tmp_path))
        assert result['result']['status']=='incomplete'
        assert 'APITimeoutError' in result['result']['reason']
        assert len(server.received)==1


def test_invalid_timeout_does_not_call_provider(tmp_path, monkeypatch):
    with local_model_service() as server:
        configure(monkeypatch,server)
        monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS','nan')
        result=run('qa',{'question':'报销期限多久'},model='real',runtime=str(tmp_path))
        assert result['result']['status']=='incomplete' and 'OPENAI_TIMEOUT_SECONDS' in result['result']['reason']
        assert server.received==[]


def test_http_adapter_receives_resumed_trace(tmp_path, monkeypatch):
    first={'theme':'字幕','budget':10,'deadline_hours':4,'fail_tools':['generate_script']}
    output=run('plan',first,runtime=str(tmp_path),run_id='http-resume')
    assert output['result']['status']=='incomplete'
    with local_model_service() as server:
        configure(monkeypatch,server)
        retry={'theme':'字幕','budget':10,'deadline_hours':4}
        resumed=run('plan',retry,model='real',runtime=str(tmp_path),run_id='http-resume',resume=True)
        assert resumed['result']['status']=='completed'
        assert server.received[0]['payload']['resume_context']
        assert server.received[0]['payload']['observations'][-1]['observation']['ok'] is False
        assert [x['decision_source'] for x in resumed['trace'][:3]]==['mock','mock','real']
