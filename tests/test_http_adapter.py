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
        output = run('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00'},model='real',runtime=str(tmp_path))
        assert output['result']['status']=='incomplete' and '决策器失败' in output['result']['reason']
        assert output['final_decision']['decision_source']=='controller_error'
        assert 'proposal' not in output['final_decision'] and 'model_reason' not in output['final_decision']
        assert len(output['trace'])==1 and output['trace'][0]['action']=='read_material'
        assert server.received[-1]['payload']['task']=='schedule'


def test_http_invalid_model_json_is_recorded_as_controller_error(tmp_path, monkeypatch):
    with local_model_service(malformed_after_observations=1) as server:
        configure(monkeypatch,server)
        output=run('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00'},model='real',runtime=str(tmp_path))
        assert output['result']['status']=='incomplete'
        assert output['final_decision']['decision_source']=='controller_error'
        assert 'JSONDecodeError' in output['final_decision']['controller_result']['reason']
        assert 'proposal' not in output['final_decision'] and 'model_reason' not in output['final_decision']
        assert len(output['trace'])==1 and output['trace'][0]['decision_source']=='real'


def test_http_timeout_is_explicit_and_bounded(tmp_path, monkeypatch):
    with local_model_service(delay_seconds=0.5) as server:
        configure(monkeypatch,server)
        monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS','0.05')
        result=run('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00'},model='real',runtime=str(tmp_path))
        assert result['result']['status']=='incomplete'
        assert 'APITimeoutError' in result['result']['reason']
        assert result['final_decision']['decision_source']=='controller_error'
        assert len(server.received)==1


def test_invalid_timeout_does_not_call_provider(tmp_path, monkeypatch):
    with local_model_service() as server:
        configure(monkeypatch,server)
        monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS','nan')
        result=run('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00'},model='real',runtime=str(tmp_path))
        assert result['result']['status']=='incomplete' and 'OPENAI_TIMEOUT_SECONDS' in result['result']['reason']
        assert result['final_decision']['decision_source']=='controller_error'
        assert server.received==[]
