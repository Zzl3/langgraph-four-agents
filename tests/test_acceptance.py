import sqlite3
from src.engine import run


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


def test_plan(tmp_path):
    req={'theme':'字幕活动','budget':10,'deadline_hours':4}
    a=run('plan',req,runtime=str(tmp_path)); b=run('plan',req,runtime=str(tmp_path))
    assert a['result']['plan']['cost']==8 and b['result']['duplicate']
    assert all(x['status']=='planned' for x in a['result']['plan']['task_step_status'])
    assert [x['observation']['cost'] for x in a['trace'] if x['action']=='estimate_cost']==[12,8]
    assert run('plan',dict(req,budget=5),runtime=str(tmp_path))['result']['status']=='incomplete'
    assert run('plan',{'theme':'x'},runtime=str(tmp_path))['result']['status']=='incomplete'
    failure=run('plan',dict(req,fail_tools=['estimate_cost']),runtime=str(tmp_path))
    assert failure['result']['status']=='incomplete' and 'generate_script' in failure['result']['completed_steps']


def test_qa(tmp_path):
    docs=[{'id':'D01','date':'2025-01-01','title':'报销制度','text':'报销期限为30天。'}]
    assert run('qa',{'question':'费用多久报销','documents':docs},runtime=str(tmp_path))['result']['answer']==docs[0]['text']
    assert run('qa',{'question':'报销期限多久'},runtime=str(tmp_path))['result']['status']=='conflict'
    assert run('qa',{'question':'公司地址'},runtime=str(tmp_path))['result']['status']=='insufficient'
    assert run('qa',{'question':'期限多久','history':[{'content':'报销'}],'documents':docs},runtime=str(tmp_path))['result']['status']=='completed'
    assert run('qa',{'question':'报销','fail_tools':['read_doc']},runtime=str(tmp_path))['result']['status']=='incomplete'
    changed=[dict(docs[0],text='报销期限为45天。')]
    assert run('qa',{'question':'报销期限多久','documents':changed},runtime=str(tmp_path))['result']['answer']=='报销期限为45天。'


def test_qa_detects_quantity_conflict_across_different_sentence_shapes(tmp_path):
    docs=[
        {'id':'D01','date':'2025-01-01','title':'费用报销制度','text':'报销提交期限为30天。'},
        {'id':'D02','date':'2025-02-01','title':'补充制度','text':'报销申请资料须在15天内提交。'},
    ]
    result=run('qa',{'question':'报销期限多久','documents':docs},runtime=str(tmp_path))['result']
    assert result['status']=='conflict'
    assert [citation['id'] for citation in result['citations']]==['D01','D02']
    from src.tasks import documents_conflict
    unrelated=[dict(docs[0],text='年假期限为30天。'),docs[1]]
    assert not documents_conflict(unrelated)
    different_property=[dict(docs[1],text='报销审核期限为15天。')]
    assert not documents_conflict([docs[0],different_property[0]])
    ambiguous=[dict(docs[0],text='报销期限为30天。'),docs[1]]
    assert run('qa',{'question':'报销期限多久','documents':ambiguous},runtime=str(tmp_path/'ambiguous'))['result']['status']=='insufficient'


def test_qa_compares_each_number_with_its_own_property():
    from src.tasks import documents_conflict
    first={'title':'报销制度','text':'报销提交30天审核15天。'}
    same={'title':'补充报销制度','text':'报销提交30天审核15天！'}
    swapped={'title':'补充报销制度','text':'报销提交15天审核30天。'}
    assert documents_conflict([first,swapped])
    assert not documents_conflict([first,same])


def test_qa_does_not_merge_conflicts_across_explicit_scopes(tmp_path):
    from src.tasks import documents_conflict
    domestic={'id':'D01','date':'2025-01-01','title':'国内差旅制度','text':'国内差旅报销提交期限为30天。'}
    overseas={'id':'D02','date':'2025-02-01','title':'海外差旅制度','text':'海外差旅报销提交期限为15天。'}
    assert documents_conflict([domestic,dict(overseas,text='海外差旅报销提交期限为30天。')]) is not True
    assert documents_conflict([domestic,overseas]) is None
    ambiguous={'title':'国内及海外差旅制度','text':'国内海外差旅报销提交期限为15天。'}
    assert documents_conflict([domestic,ambiguous]) is None
    result=run('qa',{'question':'差旅报销提交期限多久','documents':[domestic,overseas]},
               runtime=str(tmp_path))['result']
    assert result['status']=='insufficient'


def test_qa_insufficient_cannot_hide_detected_relevant_conflict(tmp_path):
    from src.tasks import validate_final
    req={'question':'报销期限多久'}
    output=run('qa',req,runtime=str(tmp_path))
    state={'task':'qa','request':req,'trace':output['trace']}
    result=validate_final(state,{'status':'insufficient'})
    assert result['status']=='conflict'
    assert len(result['citations'])==2 and result['gaps']==['适用范围或优先级']


def test_plan_deadline_gap_uses_remaining_runtime(tmp_path):
    import time
    from src.tasks import validate_final
    state={'task':'plan','request':{'theme':'测试','budget':10,'deadline_hours':3},
           'started_monotonic':time.monotonic()-7200,
           'trace':[{'action':'estimate_cost','args':{'option':'standard'},
                     'observation':{'ok':True,'option':'standard','cost':8,'hours':1.5,'steps':[]}}]}
    result=validate_final(state,{'status':'incomplete'})
    assert abs(result['deadline_gap_hours']-0.5)<0.001
    assert abs(result['option_gaps'][0]['deadline_gap_hours']-0.5)<0.001


def test_plan_absolute_deadline_gap_uses_same_remaining_time(monkeypatch):
    import src.tasks
    monkeypatch.setattr(src.tasks,'absolute_deadline_hours',lambda _:1.0)
    state={'task':'plan','request':{'theme':'测试','budget':10,'deadline':'2099-01-01T00:00:00+08:00'},
           'started_monotonic':0,
           'trace':[{'action':'estimate_cost','args':{'option':'standard'},
                     'observation':{'ok':True,'option':'standard','cost':8,'hours':1.5,'steps':[]}}]}
    result=src.tasks.validate_final(state,{'status':'incomplete'})
    assert result['deadline_gap_hours']==0.5


def test_video(tmp_path):
    req={'topic':'字幕','audience':'创作者','platform':'短视频','duration':30,'style':'简洁'}
    a=run('video',req,runtime=str(tmp_path)); assert a['result']['status']=='completed'
    assert [x['observation']['seconds'] for x in a['trace'] if x['action']=='estimate_duration']==[38,28]
    assert a['result']['source_ids']==['P01']
    assert run('video',{'topic':'字幕'},runtime=str(tmp_path))['result']['status']=='incomplete'
    assert run('video',dict(req,topic='不存在'),runtime=str(tmp_path))['result']['status']=='insufficient'
    assert run('video',dict(req,fail_tools=['estimate_duration']),runtime=str(tmp_path))['result']['status']=='incomplete'
    assert run('video',dict(req,topic='不存在',alternate_query='字幕'),runtime=str(tmp_path))['result']['status']=='completed'


def test_limit(tmp_path):
    a=run('qa',{'question':'报销'},max_calls=1,runtime=str(tmp_path))
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


def test_tool_evidence_and_unverified_claims(tmp_path):
    from src.tasks import execute_tool, validate_final
    req={'material_id':'M03','date':'2026-11-01','time':'18:00'}
    state={'request':req,'task':'schedule','runtime':str(tmp_path),'trace':[]}
    read=execute_tool('schedule','read_material',{'id':'M03'},state)
    state['trace'].append({'action':'read_material','args':{'id':'M03'},'observation':read})
    forged=dict(read['material'],content='正常')
    assert not execute_tool('schedule','check_rules',{'material':forged},state)['ok']
    assert validate_final(state,{'status':'ready','items':[],'saved':True})['status']=='incomplete'
    scene={'seconds':1,'narration':'效率提升80%', 'source_id':'P01'}
    video={'request':{},'task':'video','runtime':str(tmp_path),'trace':[]}
    assert not execute_tool('video','check_brand',{'scenes':[scene],'source_ids':['P01']},video)['passed']
    duration=execute_tool('video','estimate_duration',{'scenes':[{'seconds':1,'narration':'字'*40}]},video)
    assert duration['seconds']==10


def test_model_failure_preserves_prior_trace(tmp_path, monkeypatch):
    import src.tasks
    original=src.tasks.mock_decide
    def fail_after_first(state):
        if state['trace']: raise RuntimeError('test failure')
        return original(state)
    monkeypatch.setattr(src.tasks,'mock_decide',fail_after_first)
    result=run('qa',{'question':'报销'},runtime=str(tmp_path))
    assert result['result']['status']=='incomplete'
    assert len(result['trace'])==1 and result['trace'][0]['action']=='search_docs'


def test_real_adapter_transmits_observations(tmp_path, monkeypatch):
    """A fake HTTP client verifies adapter wiring only, not real model performance."""
    import json
    from types import SimpleNamespace
    import openai
    from src.tasks import mock_decide
    seen=[]
    class FakeClient:
        def __init__(self, **kwargs):
            self.chat=SimpleNamespace(completions=self)
        def close(self):
            pass
        def create(self, **kwargs):
            payload=json.loads(kwargs['messages'][1]['content'])
            seen.append(payload)
            decision=mock_decide({'request':payload['request'],'task':payload['task'],'trace':payload['observations']})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(decision,ensure_ascii=False)))])
    monkeypatch.setenv('OPENAI_API_KEY','test')
    monkeypatch.setenv('OPENAI_MODEL','fake')
    monkeypatch.setenv('OPENAI_TIMEOUT_SECONDS','5')
    monkeypatch.setattr(openai,'OpenAI',FakeClient)
    output=run('plan',{'theme':'字幕','budget':10,'deadline_hours':4},model='real',runtime=str(tmp_path))
    assert output['result']['status']=='completed'
    assert output['result']['plan']['cost']==8
    assert any(any(x['action']=='estimate_cost' and x['observation']['cost']==12 for x in p['observations']) for p in seen)


def test_unrelated_question_does_not_trigger_reimbursement(tmp_path):
    result=run('qa',{'question':'公司成立多久了'},runtime=str(tmp_path))['result']
    assert result['status']=='insufficient'


def test_plan_replans_after_transient_failure(tmp_path):
    req={'theme':'字幕活动','budget':10,'deadline_hours':4,'fail_once':['estimate_cost']}
    result=run('plan',req,runtime=str(tmp_path))
    assert result['result']['status']=='completed' and result['result']['plan']['option']=='economy'
    attempts=result['result']['estimate_attempts']
    assert [(a['option'],a['status']) for a in attempts]==[('standard','failed'),('economy','completed')]
    assert sum(x['action']=='generate_script' for x in result['trace'])==1


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


def test_qa_generic_documents_and_complete_read_requirement(tmp_path):
    from src.tasks import validate_final
    docs=[{'id':'FAQ-1','date':'2026-01-01','title':'字幕设置','text':'字幕可以手动开启。'}]
    result=run('qa',{'question':'字幕如何开启？','documents':docs},runtime=str(tmp_path))
    assert result['result']['status']=='completed' and result['result']['answer']==docs[0]['text']
    found={'ok':True,'matches':[{'id':'D01'},{'id':'D02'}]}
    first={'id':'D01','date':'2025-01-01','text':'30天'}
    state={'task':'qa','trace':[{'action':'search_docs','observation':found},
           {'action':'read_doc','observation':{'ok':True,'document':first}}]}
    proposal={'status':'completed','answer':'30天','citations':[{'id':'D01','date':'2025-01-01','excerpt':'30天'}]}
    assert validate_final(state,proposal)['status']=='incomplete'


def test_schedule_partial_save_failure_preserves_each_item(tmp_path):
    req={'material_ids':['M01','M04'],'date':'2026-11-01','time':'18:00','available':['19:00','20:00'],
         'fail_when':[{'action':'save_draft','args':{'draft':{'material_ids':['M04']}}}]}
    result=run('schedule',req,runtime=str(tmp_path))['result']
    assert result['status']=='incomplete' and result['saved'] is False
    assert result['items'][0]['saved'] is True and result['items'][0]['suggested_time']=='19:00'
    assert result['items'][1]['saved'] is False and 'save_draft' in result['items'][1]['save_error']


def test_absolute_deadline_with_timezone_and_repeat(tmp_path):
    req={'theme':'字幕活动','budget':10,'deadline':'2099-01-01T12:00:00+08:00'}
    first=run('plan',req,runtime=str(tmp_path)); second=run('plan',req,runtime=str(tmp_path))
    assert first['result']['status']=='completed'
    assert first['result']['plan']['deadline']==req['deadline'] and second['result']['duplicate']
    assert run('plan',dict(req,deadline='2000-01-01T00:00:00Z'),runtime=str(tmp_path))['result']['status']=='incomplete'
    assert run('plan',dict(req,deadline='2099-01-01T12:00:00'),runtime=str(tmp_path))['result']['status']=='incomplete'


def test_video_too_short_fails_without_fabricated_duration(tmp_path):
    req={'topic':'字幕','audience':'创作者','platform':'短视频','duration':0.5,'style':'简洁'}
    result=run('video',req,runtime=str(tmp_path))
    assert result['result']['status']=='incomplete' and result['result']['seconds']>req['duration']


def test_qa_retrieval_match_is_not_sufficient_answer(tmp_path):
    docs=[{'id':'P01','date':'2026-01-01','title':'字幕功能','text':'支持字幕。'}]
    result=run('qa',{'question':'字幕支持哪些语言？','documents':docs},runtime=str(tmp_path))['result']
    assert result['status']=='insufficient' and result['citations'][0]['excerpt']=='支持字幕。'


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
    assert len(output['trace'])==1


def test_qa_topic_match_does_not_answer_unrelated_detail(tmp_path):
    docs=[{'id':'P01','date':'2026-01-01','title':'字幕功能','text':'支持字幕。'}]
    result=run('qa',{'question':'字幕能自动识别声音吗？','documents':docs},runtime=str(tmp_path))['result']
    assert result['status']=='insufficient'


def test_video_shots_and_different_durations(tmp_path):
    req={'topic':'字幕','audience':'新手','platform':'短视频','duration':60,'style':'活泼'}
    long=run('video',req,runtime=str(tmp_path))
    assert long['result']['status']=='completed' and len(long['result']['scenes'])==3
    assert long['result']['scenes'][0]['narration']=='新手，来看看这个功能！'
    assert long['result']['scenes'][1]['visual']=='竖屏文字卡片'
    assert sum(x['action']=='estimate_duration' for x in long['trace'])==1
    short=run('video',dict(req,duration=3),runtime=str(tmp_path))
    assert short['result']['status']=='completed' and len(short['result']['scenes'])==1
    assert short['result']['scenes'][0]['narration']=='支持字幕'
    assert short['result']['seconds']<=3


def test_video_brand_rule_failure_is_not_success(tmp_path):
    req={'topic':'字幕','audience':'创作者','platform':'短视频','duration':30,'style':'简洁',
         'brand_rules':{'banned_phrases':['怎么使用']}}
    result=run('video',req,runtime=str(tmp_path))
    assert result['result']['status']=='incomplete'
    assert any('禁止' in risk for risk in result['result']['risks'])
    check=next(x for x in result['trace'] if x['action']=='check_brand')
    assert not check['observation']['passed']


def test_invalid_inputs_do_not_bypass_constraints(tmp_path):
    req={'material_id':'M01','date':'2026-11-99','time':'18:00'}
    assert run('schedule',req,runtime=str(tmp_path))['result']['status']=='incomplete'
    req={'material_id':'M01','date':'2026-11-01','time':'18:00','available':['26:00']}
    assert run('schedule',req,runtime=str(tmp_path))['result']['status']=='incomplete'
    for bad in (float('nan'),float('inf'),True):
        assert run('plan',{'theme':'字幕','budget':bad,'deadline_hours':4},runtime=str(tmp_path))['result']['status']=='incomplete'
        video={'topic':'字幕','audience':'创作者','platform':'短视频','duration':bad,'style':'简洁'}
        assert run('video',video,runtime=str(tmp_path))['result']['status']=='incomplete'
    assert run('schedule',{'items':[{'id':'M01','date':'2026-11-01','time':'18:00'},'bad']},runtime=str(tmp_path))['result']['status']=='incomplete'


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


def test_qa_different_evidence_is_not_always_conflict(tmp_path):
    docs=[{'id':'D1','date':'2026-01-01','title':'字幕语言','text':'字幕支持中文。'},
          {'id':'D2','date':'2026-01-02','title':'字幕语言','text':'字幕不支持阿拉伯语。'}]
    result=run('qa',{'question':'字幕支持哪些语言？','documents':docs},runtime=str(tmp_path))['result']
    assert result['status']=='insufficient' and len(result['citations'])==2
    conflict=[{'id':'D1','date':'2026-01-01','title':'远程办公','text':'允许远程办公。'},
              {'id':'D2','date':'2026-01-02','title':'远程办公','text':'不允许远程办公。'}]
    result=run('qa',{'question':'允许远程办公吗？','documents':conflict},runtime=str(tmp_path))['result']
    assert result['status']=='conflict'


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


def test_plan_custom_catalog_respects_topic_and_replans(tmp_path):
    catalog={'materials':[{'id':'FOOD','description':'家常菜素材','keywords':['美食']},
                          {'id':'SUB','description':'字幕素材','keywords':['字幕']}],
             'options':{'premium':{'steps':[{'name':'拍摄','cost':12,'hours':2}]},
                        'basic':{'steps':[{'name':'剪辑','cost':8,'hours':2}]}}}
    req={'theme':'美食活动','budget':10,'deadline_hours':4,'plan_data':catalog}
    result=run('plan',req,runtime=str(tmp_path))
    assert result['result']['status']=='completed'
    assert result['result']['plan']['material_id']=='FOOD' and result['result']['plan']['option']=='basic'
    searched=next(x['observation']['materials'] for x in result['trace'] if x['action']=='search_material')
    assert [x['id'] for x in searched]==['FOOD']
    missing=run('plan',dict(req,theme='游戏活动'),runtime=str(tmp_path))
    assert missing['result']['status']=='incomplete' and '素材' in missing['result']['reason']
    impossible=run('plan',dict(req,deadline_hours=1),runtime=str(tmp_path))['result']
    assert impossible['status']=='incomplete' and abs(impossible['deadline_gap_hours']-1)<0.001


def test_plan_invalid_cost_is_not_recorded_as_success(tmp_path):
    catalog={'materials':[{'id':'A','description':'字幕功能','keywords':['字幕']}],
             'options':{'bad':{'steps':[{'name':'拍摄','cost':-1,'hours':1}]},
                        'safe':{'steps':[{'name':'剪辑','cost':4,'hours':1}]}}}
    result=run('plan',{'theme':'字幕','budget':10,'deadline_hours':4,'plan_data':catalog},runtime=str(tmp_path))
    assert result['result']['status']=='completed' and result['result']['plan']['option']=='safe'
    attempts=result['result']['estimate_attempts']
    assert [(x['option'],x['status']) for x in attempts]==[('bad','failed'),('safe','completed')]
    assert '无效' in attempts[0]['result']['error']


def test_plan_cannot_save_spoofed_material(tmp_path):
    from src.tasks import execute_tool
    req={'theme':'字幕','budget':10,'deadline_hours':4}
    result=run('plan',req,runtime=str(tmp_path/'original'))
    save=next(x for x in result['trace'] if x['action']=='save_plan')
    plan=dict(save['args']['plan'],material_id='FORGED')
    state={'request':req,'task':'plan','trace':result['trace'],'runtime':str(tmp_path/'forged')}
    obs=execute_tool('plan','save_plan',{'plan':plan},state)
    assert not obs['ok']


def test_plan_decimal_costs_do_not_spuriously_exceed_budget(tmp_path):
    catalog={'materials':[{'id':'A','description':'字幕功能','keywords':['字幕']}],
             'options':{'only':{'steps':[{'name':'脚本','cost':0.1,'hours':0.1},
                                          {'name':'剪辑','cost':0.2,'hours':0.2}]}}}
    result=run('plan',{'theme':'字幕','budget':0.3,'deadline_hours':0.31,'plan_data':catalog},runtime=str(tmp_path))
    assert result['result']['status']=='completed'
    assert result['result']['plan']['cost']==0.3 and result['result']['plan']['hours']==0.3


def test_plan_different_user_catalog_is_a_different_request(tmp_path):
    import copy
    catalog={'materials':[{'id':'A','description':'字幕功能','keywords':['字幕']}],
             'options':{'one':{'steps':[{'name':'剪辑','cost':4,'hours':1}]}}}
    req={'theme':'字幕','budget':10,'deadline_hours':4,'plan_data':catalog}
    first=run('plan',req,runtime=str(tmp_path))
    changed=copy.deepcopy(req)
    changed['plan_data']['options']['one']['steps'][0]['cost']=5
    second=run('plan',changed,runtime=str(tmp_path))
    assert first['result']['plan']['cost']==4 and second['result']['plan']['cost']==5
    assert run('plan',req,runtime=str(tmp_path))['result']['duplicate']
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        assert conn.execute("select count(*) from drafts where kind='plan'").fetchone()[0]==2


def test_relative_deadline_decreases_during_run(tmp_path):
    import time
    from src.tasks import execute_tool
    req={'theme':'字幕','budget':10,'deadline_hours':4}
    output=run('plan',req,runtime=str(tmp_path/'first'))
    saved=next(x for x in output['trace'] if x['action']=='save_plan')
    state={'task':'plan','request':req,'trace':output['trace'],'runtime':str(tmp_path/'late'),
           'started_monotonic':time.monotonic()-5*3600}
    response=execute_tool('plan','save_plan',saved['args'],state)
    assert not response['ok'] and '截止时间' in response['error']


def test_qa_uses_recent_user_context_without_assistant_pollution(tmp_path):
    docs=[{'id':'R','date':'2026-01-01','title':'报销制度','text':'报销期限为30天。'},
          {'id':'L','date':'2026-01-01','title':'年假制度','text':'年假期限为5天。'},
          {'id':'S','date':'2026-01-01','title':'字幕设置','text':'字幕可以手动开启。'}]
    history=[{'role':'user','content':'报销期限多久？'}, {'role':'assistant','content':'年假期限为99天。'},
             {'role':'user','content':'年假制度'}, {'role':'assistant','content':'报销期限为99天。'}]
    result=run('qa',{'question':'期限多久','history':history,'documents':docs},runtime=str(tmp_path))
    assert result['result']['status']=='completed' and result['result']['citations'][0]['id']=='L'
    query=next(x['args']['query'] for x in result['trace'] if x['action']=='search_docs')
    assert '年假制度' in query and '报销' not in query
    independent=run('qa',{'question':'字幕如何开启？','history':history,'documents':docs},runtime=str(tmp_path))
    assert independent['result']['status']=='completed' and independent['result']['citations'][0]['id']=='S'
    missing=run('qa',{'question':'公司地址在哪里？','history':history,'documents':docs},runtime=str(tmp_path))
    assert missing['result']['status']=='insufficient'


def test_qa_cannot_skip_required_baseline_search():
    from src.tasks import validate_final
    doc={'id':'D01','date':'2025-01-01','title':'报销制度','text':'报销期限为30天。'}
    search={'action':'search_docs','args':{'query':'30天'},'observation':{'ok':True,'matches':[{'id':'D01'}]}}
    read={'action':'read_doc','args':{'id':'D01'},'observation':{'ok':True,'document':doc}}
    state={'task':'qa','request':{'question':'报销期限多久'},'trace':[search,read]}
    result={'status':'completed','answer':doc['text'],'citations':[{'id':'D01','date':doc['date'],'excerpt':doc['text']}]}
    assert validate_final(state,result)['status']=='incomplete'


def test_video_uses_audience_platform_and_style_without_extra_claims(tmp_path):
    base={'topic':'字幕','audience':'运营人员','platform':'横屏','duration':30,'style':'简洁'}
    horizontal=run('video',base,runtime=str(tmp_path))['result']
    assert horizontal['status']=='completed'
    assert horizontal['scenes'][0]['narration']=='运营同学，一起看看这项功能。'
    assert horizontal['scenes'][1]['visual']=='文字卡片'
    vertical=run('video',dict(base,audience='新手',platform='抖音',style='活泼'),runtime=str(tmp_path))['result']
    assert vertical['status']=='completed'
    assert vertical['scenes'][0]['narration']=='新手，来看看这个功能！'
    assert vertical['scenes'][1]['visual']=='竖屏文字卡片'
    assert [s['narration'] for s in vertical['scenes'] if s['source_id']]==['支持字幕']


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


def test_plan_rejects_invalid_dependencies_and_fake_completion(tmp_path):
    from src.tasks import execute_tool
    catalog={'materials':[{'id':'A','description':'字幕功能','keywords':['字幕']}],
             'options':{'invalid':{'steps':[{'name':'剪辑','cost':2,'hours':1,'depends_on':['脚本']},
                                           {'name':'脚本','cost':1,'hours':1}]},
                        'valid':{'steps':[{'name':'脚本','cost':1,'hours':1,'depends_on':[]},
                                         {'name':'剪辑','cost':2,'hours':1,'depends_on':['脚本']}]}}}
    req={'theme':'字幕','budget':10,'deadline_hours':4,'plan_data':catalog}
    output=run('plan',req,runtime=str(tmp_path/'initial'))
    assert output['result']['status']=='completed' and output['result']['plan']['option']=='valid'
    assert output['result']['estimate_attempts'][0]['status']=='failed'
    assert output['result']['plan']['steps'][1]['depends_on']==['脚本']
    save=next(x for x in output['trace'] if x['action']=='save_plan')
    state={'task':'plan','request':req,'trace':output['trace'],'runtime':str(tmp_path/'attack')}
    forged=dict(save['args']['plan'],executed=True)
    assert not execute_tool('plan','save_plan',{'plan':forged},state)['ok']
    assert not (tmp_path/'attack'/'drafts.sqlite3').exists()
    bad_catalog={'materials':catalog['materials'],'options':{'only':{'steps':[{'name':'脚本','cost':1,'hours':1,'status':'completed'}]}}}
    bad=run('plan',dict(req,plan_data=bad_catalog),runtime=str(tmp_path/'bad'))
    assert bad['result']['status']=='incomplete'


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


def test_plan_summary_does_not_invent_execution_or_status(tmp_path):
    from src.tasks import validate_final
    req={'theme':'字幕','budget':10,'deadline_hours':4}
    output=run('plan',req,runtime=str(tmp_path))
    plan=output['result']['plan']
    state={'task':'plan','request':req,'trace':output['trace']}
    invented={'status':'completed','plan':plan,'saved':True,'duplicate':True,
              'message':'活动已经执行','step_status':{'generate_script':'failed'}}
    result=validate_final(state,invented)
    assert 'message' not in result and result['duplicate'] is False
    assert result['step_status']['generate_script']=='completed'
    assert all(x['status']=='planned' for x in result['plan']['task_step_status'])
    incomplete=validate_final(state,{'status':'incomplete','saved':False,'reason':'未知原因'})
    assert incomplete['saved'] is True and incomplete['plan']==plan


def test_video_result_removes_unchecked_claims(tmp_path):
    from src.tasks import validate_final
    req={'topic':'字幕','audience':'创作者','platform':'短视频','duration':30,'style':'简洁'}
    output=run('video',req,runtime=str(tmp_path))
    state={'task':'video','request':req,'trace':output['trace']}
    proposed=dict(output['result'],marketing_claim='效率提升80%')
    clean=validate_final(state,proposed)
    assert clean['status']=='completed' and 'marketing_claim' not in clean
    unfinished=validate_final(state,{'status':'incomplete','reason':'产品效率提升80%',
                                     'source_ids':['FAKE'],'scenes':[{'narration':'效率提升80%'}]})
    assert unfinished['status']=='incomplete' and '80%' not in unfinished['reason']
    assert 'scenes' not in unfinished and 'FAKE' not in unfinished['source_ids']


def test_qa_final_output_drops_uncited_extra_claims(tmp_path):
    from src.tasks import validate_final
    docs=[{'id':'D01','date':'2025-01-01','title':'报销制度','text':'报销期限为30天。'}]
    req={'question':'报销期限多久','documents':docs}
    output=run('qa',req,runtime=str(tmp_path))
    state={'task':'qa','request':req,'trace':output['trace']}
    proposed=dict(output['result'],summary='报销期限为15天',hidden_claim='无依据内容')
    clean=validate_final(state,proposed)
    assert clean['answer']=='报销期限为30天。' and 'summary' not in clean and 'hidden_claim' not in clean
    invalid=validate_final(state,{'status':'ready','answer':'报销期限为15天'})
    assert invalid['status']=='incomplete' and 'answer' not in invalid
    incomplete=validate_final(state,{'status':'incomplete','reason':'报销期限为15天','answer':'报销期限为15天'})
    assert '15天' not in incomplete['reason'] and 'answer' not in incomplete


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


def test_repeated_immutable_read_is_blocked_but_refreshable_slot_query_is_not():
    from src.engine import repeated_successful_call
    trace=[{'action':'read_doc','args':{'id':'D01'},'observation':{'ok':True}}]
    assert repeated_successful_call('read_doc',{'id':'D01'},trace)
    assert not repeated_successful_call('read_doc',{'id':'D02'},trace)
    assert not repeated_successful_call('query_slots',{'date':'2026-11-01'},[
        {'action':'query_slots','args':{'date':'2026-11-01'},'observation':{'ok':True}}])


def test_model_observations_deduplicate_repeated_results_without_losing_distinct_evidence():
    from src.engine import model_observations
    trace=[
        {'step':1,'action':'search_docs','args':{'query':'q'},'observation':{'ok':True,'matches':['D1','D2']}},
        {'step':2,'action':'read_doc','args':{'id':'D1'},'observation':{'ok':True,'text':'30 days'}},
        {'step':3,'action':'search_docs','args':{'query':'q'},'observation':{'ok':True,'matches':['D1','D2']}},
        {'step':4,'action':'read_doc','args':{'id':'D2'},'observation':{'ok':True,'text':'15 days'}},
    ]
    compact=model_observations(trace)
    assert [(x['action'],x['args']) for x in compact]==[
        ('search_docs',{'query':'q'}),('read_doc',{'id':'D1'}),('read_doc',{'id':'D2'})]
    assert compact[0]['repetitions']==2
    assert len(trace)==4
