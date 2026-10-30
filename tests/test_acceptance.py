import sqlite3


from src.engine import run


def test_only_selected_schedule_graph_is_available():
    from src.engine import TASKS, build_graph
    import pytest
    assert TASKS == ('schedule',)
    graph=build_graph('schedule')
    nodes=set(graph.get_graph().nodes)
    assert {'schedule_decide','schedule_execute','schedule_limit'} <= nodes
    with pytest.raises(ValueError,match='未知任务'):
        build_graph('other')


def test_schedule(tmp_path):
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    a=run('schedule',req,runtime=str(tmp_path)); b=run('schedule',req,runtime=str(tmp_path))
    assert a['result']['status']=='pending_confirmation' and a['result']['suggested_time']=='19:00'
    assert a['result']['saved'] and not a['result']['published'] and b['result']['duplicate']
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as c: assert c.execute('select count(*) from drafts').fetchone()[0]==1
    assert run('schedule',dict(req,time='20:00'),runtime=str(tmp_path))['result']['status']=='ready'
    for mid in ('M02','M03'):
        assert run('schedule',dict(req,material_id=mid),runtime=str(tmp_path))['result']['status']=='incomplete'
    assert run('schedule',dict(req,save_fail=True),runtime=str(tmp_path))['result']['status']=='incomplete'


def test_limit(tmp_path):
    a=run('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00'},max_calls=1,runtime=str(tmp_path))
    assert a['result']['status']=='incomplete' and '上限' in a['result']['reason']


def test_multiple_materials_and_partial_review(tmp_path):
    req={'material_ids':['M01','M04','M03'],'date':'2026-11-01','time':'18:00',
         'available':['19:00','20:00']}
    result=run('schedule',req,runtime=str(tmp_path))['result']
    assert result['status']=='incomplete'
    assert [(x['id'],x['suggested_time'],x['saved']) for x in result['items']]==[
        ('M01','19:00',True),('M04','20:00',True),('M03',None,False)]
    assert all(x['confirmation'] for x in result['items'][:2])
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as c: assert c.execute('select count(*) from drafts').fetchone()[0]==2
    duplicate=run('schedule',req,runtime=str(tmp_path))['result']
    assert all(x['duplicate'] for x in duplicate['items'][:2])


def test_schedule_different_dates_and_requested_times(tmp_path):
    req={'items':[{'id':'M01','date':'2026-11-01','time':'18:00'},
                  {'id':'M04','date':'2026-11-02','time':'18:00'}],
         'slots_by_date':{'2026-11-01':{'occupied':['18:00'],'available':['19:00']},
                          '2026-11-02':{'occupied':[],'available':['19:00']}}}
    output=run('schedule',req,runtime=str(tmp_path))
    assert output['result']['status']=='pending_confirmation'
    assert [(i['date'],i['suggested_time']) for i in output['result']['items']]==[('2026-11-01','19:00'),('2026-11-02','18:00')]
    assert len([x for x in output['trace'] if x['action']=='query_slots'])==2


def test_idempotency_when_availability_changes(tmp_path):
    req={'material_id':'M01','date':'2026-11-01','time':'18:00','available':['19:00']}
    assert run('schedule',req,runtime=str(tmp_path))['result']['saved']
    second=run('schedule',dict(req,available=['20:00']),runtime=str(tmp_path))
    assert second['result']['status']=='incomplete' and second['result']['saved'] is False
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as c: assert c.execute('select count(*) from drafts').fetchone()[0]==1


def test_schedule_partial_save_failure_preserves_each_item(tmp_path):
    req={'material_ids':['M01','M04'],'date':'2026-11-01','time':'18:00','available':['19:00','20:00'],
         'fail_when':[{'action':'save_draft','args':{'draft':{'material_ids':['M04']}}}]}
    result=run('schedule',req,runtime=str(tmp_path))['result']
    assert result['status']=='incomplete' and result['saved'] is False
    assert result['items'][0]['saved'] is True and result['items'][0]['suggested_time']=='19:00'
    assert result['items'][1]['saved'] is False and 'save_draft' in result['items'][1]['save_error']


def test_malformed_model_result_preserves_trace(tmp_path, monkeypatch):
    import src.tasks
    original=src.tasks.mock_decide
    def malformed(state):
        if state['trace']:
            return {'action':'finish','result':{'status':'ready','items':[42],'saved':True,'published':False}}
        return original(state)
    monkeypatch.setattr(src.tasks,'mock_decide',malformed)
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    output=run('schedule',req,runtime=str(tmp_path))
    assert output['result']['status']=='incomplete' and output['result']['code']=='unverified_result'
    assert output['final_decision']['reason']==output['result']['reason']
    assert output['final_decision']['proposal']=={'status':'ready','items':[42],'saved':True,'published':False}
    assert output['final_decision']['decision_source']=='mock'
    assert len(output['trace'])==1


def test_schedule_user_metadata_and_rules(tmp_path):
    req={'material_id':'M99','date':'2026-11-01','time':'20:00',
         'materials':{'M99':{'id':'M99','title':'自定义素材','content':'宣传内容包含夸张词'}},
         'blocked_terms':['夸张']}
    blocked=run('schedule',req,runtime=str(tmp_path))['result']
    assert blocked['status']=='incomplete' and blocked['items'][0]['review_status']=='blocked'
    assert any('夸张' in x for x in blocked['items'][0]['risk_reasons'])
    req['materials']['M99']['content']='普通说明'
    assert run('schedule',req,runtime=str(tmp_path))['result']['status']=='ready'
    req['materials']['M99']['id']='M01'
    assert run('schedule',req,runtime=str(tmp_path))['result']['status']=='incomplete'


def test_alternative_slot_cannot_claim_consent(tmp_path):
    from src.tasks import execute_tool
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    result=run('schedule',req,runtime=str(tmp_path/'first'))
    save_entry=next(x for x in result['trace'] if x['action']=='save_draft')
    trace=[x for x in result['trace'] if x['action']!='save_draft']
    draft=dict(save_entry['args']['draft'],confirmation='用户已确认')
    state={'task':'schedule','request':req,'trace':trace,'runtime':str(tmp_path/'second')}
    obs=execute_tool('schedule','save_draft',{'draft':draft},state)
    assert obs['ok'] is False and '确认' in obs['error']


def test_separate_requests_respect_local_draft_reservations(tmp_path):
    first={'material_id':'M01','date':'2026-11-01','time':'20:00'}
    second={'material_id':'M04','date':'2026-11-01','time':'20:00'}
    one=run('schedule',first,runtime=str(tmp_path))
    two=run('schedule',second,runtime=str(tmp_path))
    again=run('schedule',first,runtime=str(tmp_path))
    assert one['result']['suggested_time']=='20:00'
    assert two['result']['suggested_time']=='19:00' and two['result']['status']=='pending_confirmation'
    assert again['result']['duplicate'] and again['result']['suggested_time']=='20:00'
    query=next(x for x in two['trace'] if x['action']=='query_slots')
    assert query['observation']['local_draft_slots']==['20:00']


def test_concurrent_schedule_conflict_requeries(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    import src.tasks
    original=src.tasks.stored_schedule_drafts
    barrier=Barrier(2, timeout=10)
    mutex=Lock()
    count=0
    def synchronized_first_queries(runtime):
        nonlocal count
        snapshot=original(runtime)
        with mutex:
            count+=1
            wait=count<=2
        if wait: barrier.wait()
        return snapshot
    monkeypatch.setattr(src.tasks,'stored_schedule_drafts',synchronized_first_queries)
    def execute(mid):
        return run('schedule',{'material_id':mid,'date':'2026-11-01','time':'20:00'},runtime=str(tmp_path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(execute,mid) for mid in ('M01','M04')]
        results=[future.result(timeout=15) for future in futures]
    assert all(x['result']['saved'] for x in results)
    assert {x['result']['suggested_time'] for x in results}=={'19:00','20:00'}
    assert any(any(step['observation'].get('code')=='slot_conflict' for step in x['trace']) for x in results)
    assert any(sum(step['action']=='query_slots' for step in x['trace'])==2 for x in results)
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as c:
        assert c.execute("select count(*) from drafts where kind='schedule'").fetchone()[0]==2


def test_existing_blocked_item_does_not_hide_its_draft(tmp_path):
    first={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    assert run('schedule',first,runtime=str(tmp_path))['result']['suggested_time']=='19:00'
    req={'items':[{'id':'M01','date':'2026-11-01','time':'18:00'},
                  {'id':'M04','date':'2026-11-01','time':'19:00'}],
         'materials':{'M01':{'id':'M01','title':'素材','content':'绝对有效'},
                      'M04':{'id':'M04','title':'素材','content':'普通说明'}}}
    result=run('schedule',req,runtime=str(tmp_path))['result']
    assert result['status']=='incomplete'
    assert result['items'][0]['review_status']=='blocked'
    assert result['items'][1]['suggested_time']=='20:00' and result['items'][1]['saved']


def test_corrupt_persisted_draft_does_not_get_ignored(tmp_path):
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        conn.execute('create table drafts(kind text, key text, value text, primary key(kind,key))')
        conn.execute('insert into drafts values(?,?,?)',('schedule','bad','{}'))
    result=run('schedule',{'material_id':'M01','date':'2026-11-01','time':'20:00'},runtime=str(tmp_path))
    assert result['result']['status']=='incomplete'
    assert any(x['action']=='query_slots' and not x['observation']['ok'] for x in result['trace'])
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        assert conn.execute('select count(*) from drafts').fetchone()[0]==1


def test_legacy_conflicting_drafts_are_not_ignored(tmp_path):
    import json
    from src.tasks import schedule_draft_key
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        conn.execute('create table drafts(kind text, key text, value text, primary key(kind,key))')
        for mid in ('M01','M04'):
            item={'id':mid,'date':'2026-11-01','time':'20:00'}
            draft={'material_ids':[mid],'date':item['date'],'requested_time':item['time'],
                   'suggested_time':'20:00','status':'ready','confirmation':None,'published':False}
            conn.execute('insert into drafts values(?,?,?)',('schedule',schedule_draft_key(item),json.dumps(draft,ensure_ascii=False,sort_keys=True)))
    output=run('schedule',{'material_id':'M01','date':'2026-11-01','time':'20:00'},runtime=str(tmp_path))
    assert output['result']['status']=='incomplete'
    assert any('草稿时段冲突' in step['observation'].get('error','') for step in output['trace'])


def test_limit_preserves_actual_saved_draft(tmp_path):
    req={'material_id':'M01','date':'2026-11-01','time':'20:00'}
    result=run('schedule',req,max_calls=4,runtime=str(tmp_path))
    assert result['result']['status']=='incomplete' and result['result']['saved'] is True
    assert result['result']['items'][0]['saved'] is True and 'save_draft' in result['result']['completed_steps']
    again=run('schedule',req,runtime=str(tmp_path))
    assert again['result']['duplicate'] is True


def test_draft_rejects_extra_claims_and_false_conflict(tmp_path):
    from src.tasks import execute_tool, pending_confirmation
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    run_result=run('schedule',req,runtime=str(tmp_path/'first'))
    save=next(x for x in run_result['trace'] if x['action']=='save_draft')
    trace=[x for x in run_result['trace'] if x['action']!='save_draft']
    state={'task':'schedule','request':req,'trace':trace,'runtime':str(tmp_path/'attack')}
    original=save['args']['draft']
    for forged in (dict(original,confirmed=True),
                   {k:v for k,v in original.items() if k!='published'},
                   dict(original,confirmation=original['confirmation']+'，用户已经许可')):
        assert not execute_tool('schedule','save_draft',{'draft':forged},state)['ok']
    assert not (tmp_path/'attack'/'drafts.sqlite3').exists()
    free_req={'material_id':'M01','date':'2026-11-01','time':'20:00'}
    free=run('schedule',free_req,runtime=str(tmp_path/'free'))
    pre_save=[x for x in free['trace'] if x['action']!='save_draft']
    draft={'material_ids':['M01'],'date':'2026-11-01','requested_time':'20:00','suggested_time':'19:00',
           'status':'pending_confirmation','confirmation':pending_confirmation('20:00','19:00'),'published':False}
    state={'task':'schedule','request':free_req,'trace':pre_save,'runtime':str(tmp_path/'attack')}
    obs=execute_tool('schedule','save_draft',{'draft':draft},state)
    assert not obs['ok'] and '原要求时段可用' in obs['error']


def test_schedule_summary_is_rebuilt_from_tool_evidence(tmp_path):
    from src.tasks import validate_final, partial_schedule
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    output=run('schedule',req,runtime=str(tmp_path))
    state={'task':'schedule','request':req,'trace':output['trace']}
    invented={'status':'pending_confirmation','items':[{'id':'FAKE','confirmed':True}],
              'saved':True,'published':False,'message':'已经发布'}
    result=validate_final(state,invented)
    assert result['items'][0]['id']=='M01' and result['items'][0]['confirmation'].endswith('等待用户确认')
    assert 'confirmed' not in result['items'][0] and 'message' not in result and result['published'] is False
    rejected=validate_final(state,dict(invented,status='ready'))
    assert rejected['status']=='incomplete' and rejected['items'][0]['saved'] is True
    failed_retry={'action':'save_draft','args':next(x['args'] for x in output['trace'] if x['action']=='save_draft'),
                  'observation':{'ok':False,'saved':False,'error':'模拟重试失败'}}
    partial=partial_schedule(req,output['trace']+[failed_retry])
    assert partial[0]['saved'] is True and partial[0]['save_error']=='模拟重试失败'


def test_schedule_does_not_repeat_forged_confirmation_in_reasons(tmp_path, monkeypatch):
    import src.tasks
    original=src.tasks.mock_decide
    def deceptive(state):
        decision=original(state)
        if decision['action']=='save_draft': decision['reason']='用户已同意修改，发布成功'
        if decision['action']=='finish': decision['reason']='已经发布'
        return decision
    monkeypatch.setattr(src.tasks,'mock_decide',deceptive)
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    output=run('schedule',req,runtime=str(tmp_path))
    assert output['result']['status']=='pending_confirmation' and output['result']['published'] is False
    save=next(x for x in output['trace'] if x['action']=='save_draft')
    assert '发布成功' not in save['reason'] and '已同意' not in save['reason']
    assert '已经发布' not in output['final_decision']['reason']
    assert '已经发布' not in output['final_decision']['model_reason']


def test_trace_file_is_incremental_and_never_overwrites(tmp_path, monkeypatch):
    import json
    import src.tasks
    trace_file=tmp_path/'trace.jsonl'
    original=src.tasks.execute_tool
    observed=[]
    def inspect(task, action, args, state):
        events=[json.loads(line) for line in trace_file.read_text(encoding='utf-8').splitlines()]
        observed.append(len([e for e in events if e['event']=='tool']))
        return original(task,action,args,state)
    monkeypatch.setattr(src.tasks,'execute_tool',inspect)
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    output=run('schedule',req,runtime=str(tmp_path/'runtime'),trace_file=trace_file)
    events=[json.loads(line) for line in trace_file.read_text(encoding='utf-8').splitlines()]
    assert output['result']['status']=='pending_confirmation'
    assert observed==list(range(len(output['trace'])))
    assert events[0]['event']=='start' and events[-1]['event']=='finish'
    assert [e['entry'] for e in events if e['event']=='tool']==output['trace']
    original_content=trace_file.read_text(encoding='utf-8')
    failed=run('schedule',req,runtime=str(tmp_path/'other'),trace_file=trace_file)
    assert failed['result']['status']=='incomplete' and trace_file.read_text(encoding='utf-8')==original_content


def test_trace_write_failure_does_not_hide_saved_result(tmp_path, monkeypatch):
    import src.engine
    def fail_write(path,event):
        raise OSError('simulated trace failure')
    monkeypatch.setattr(src.engine,'append_trace',fail_write)
    req={'material_id':'M01','date':'2026-11-01','time':'18:00'}
    output=run('schedule',req,runtime=str(tmp_path/'runtime'),trace_file=tmp_path/'trace.jsonl')
    assert output['result']['saved'] is True and output['result']['status']=='pending_confirmation'
    assert '轨迹写入失败' in output['trace_error'] and len(output['trace'])==4


def test_compact_observations_preserves_distinct_results():
    from src.engine import compact_observations
    base={'action':'query_slots','args':{'date':'2026-11-01'},'observation':{'ok':True,'occupied':['18:00']},'reason':'查询'}
    trace=[dict(base,step=i) for i in (1,2,3)]
    trace.append({'step':4,'action':'query_slots','args':{'date':'2026-11-01'},
                  'observation':{'ok':True,'occupied':['18:00','19:00']},'reason':'重查'})
    compact=compact_observations(trace)
    assert len(compact)==2 and compact[0]['step']==3 and compact[0]['consecutive_repetitions']==3
    assert compact[1]['observation']['occupied']==['18:00','19:00']
    assert all('consecutive_repetitions' not in x for x in trace)
