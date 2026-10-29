"""Generate reproducible offline evidence; uses a fresh temporary SQLite store per case."""
import json
import tempfile
from pathlib import Path
from src.engine import run

cases = {
 'schedule_normal':('schedule',{'material_id':'M01','date':'2026-11-01','time':'20:00'}),
 'schedule_conflict':('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00'}),
 'schedule_multiple':('schedule',{'material_ids':['M01','M04','M03'],'date':'2026-11-01','time':'18:00','available':['19:00','20:00']}),
 'schedule_dates':('schedule',{'items':[{'id':'M01','date':'2026-11-01','time':'18:00'},{'id':'M04','date':'2026-11-02','time':'18:00'}],
                                'slots_by_date':{'2026-11-01':{'occupied':['18:00'],'available':['19:00']},'2026-11-02':{'occupied':[],'available':['19:00']}}}),
 'schedule_missing':('schedule',{'material_id':'M02','date':'2026-11-01','time':'18:00'}),
 'schedule_risk':('schedule',{'material_id':'M03','date':'2026-11-01','time':'18:00'}),
 'schedule_failure':('schedule',{'material_id':'M01','date':'2026-11-01','time':'18:00','save_fail':True}),
 'schedule_partial_failure':('schedule',{'material_ids':['M01','M04'],'date':'2026-11-01','time':'18:00','available':['19:00','20:00'],
                                        'fail_when':[{'action':'save_draft','args':{'draft':{'material_ids':['M04']}}}]}),
 'schedule_custom_material':('schedule',{'material_id':'M99','date':'2026-11-01','time':'20:00',
                                         'materials':{'M99':{'id':'M99','title':'自定义素材','content':'普通说明'}}}),
 'plan_normal':('plan',{'theme':'字幕活动','budget':10,'deadline_hours':4}),
 'plan_missing':('plan',{'theme':'字幕活动'}),
 'plan_failure':('plan',{'theme':'字幕活动','budget':10,'deadline_hours':4,'fail_tools':['estimate_cost']}),
 'plan_recover':('plan',{'theme':'字幕活动','budget':10,'deadline_hours':4,'fail_once':['estimate_cost']}),
 'plan_over_budget':('plan',{'theme':'字幕活动','budget':5,'deadline_hours':4}),
 'plan_deadline':('plan',{'theme':'字幕活动','budget':10,'deadline':'2099-01-01T12:00:00+08:00'}),
 'plan_no_material':('plan',{'theme':'游戏活动','budget':10,'deadline_hours':4}),
 'plan_custom':('plan',{'theme':'美食活动','budget':10,'deadline_hours':4,
                       'plan_data':{'materials':[{'id':'FOOD','description':'家常菜素材','keywords':['美食']},
                                                  {'id':'SUB','description':'字幕素材','keywords':['字幕']}],
                                    'options':{'premium':{'steps':[{'name':'拍摄','cost':12,'hours':2}]},
                                               'basic':{'steps':[{'name':'剪辑','cost':8,'hours':2}]}}}}),
 'plan_decimal':('plan',{'theme':'字幕','budget':0.3,'deadline_hours':0.31,
                        'plan_data':{'materials':[{'id':'A','description':'字幕功能','keywords':['字幕']}],
                                     'options':{'only':{'steps':[{'name':'脚本','cost':0.1,'hours':0.1},
                                                                  {'name':'剪辑','cost':0.2,'hours':0.2}]}}}}),
 'qa_normal':('qa',{'question':'费用多久报销','documents':[{'id':'D01','date':'2025-01-01','title':'报销制度','text':'报销期限为30天。'}]}),
 'qa_conflict':('qa',{'question':'报销期限多久'}),
 'qa_missing':('qa',{'question':'公司地址在哪里'}),
 'qa_failure':('qa',{'question':'报销','fail_tools':['read_doc']}),
 'qa_custom':('qa',{'question':'字幕如何开启？','documents':[{'id':'FAQ-1','date':'2026-01-01','title':'字幕设置','text':'字幕可以手动开启。'}]}),
 'qa_unsupported_detail':('qa',{'question':'字幕支持哪些语言？','documents':[{'id':'P01','date':'2026-01-01','title':'字幕功能','text':'支持字幕。'}]}),
 'qa_distinct_not_conflict':('qa',{'question':'字幕支持哪些语言？','documents':[{'id':'D1','date':'2026-01-01','title':'字幕语言','text':'字幕支持中文。'},
                                                                            {'id':'D2','date':'2026-01-02','title':'字幕语言','text':'字幕不支持阿拉伯语。'}]}),
 'qa_followup_context':('qa',{'question':'期限多久','history':[{'role':'user','content':'报销期限多久？'},
                   {'role':'assistant','content':'年假期限为99天。'}, {'role':'user','content':'年假制度'},
                   {'role':'assistant','content':'报销期限为99天。'}],
                   'documents':[{'id':'R','date':'2026-01-01','title':'报销制度','text':'报销期限为30天。'},
                                {'id':'L','date':'2026-01-01','title':'年假制度','text':'年假期限为5天。'}]}),
 'video_normal':('video',{'topic':'字幕','audience':'创作者','platform':'短视频','duration':30,'style':'简洁'}),
 'video_missing':('video',{'topic':'字幕'}),
 'video_no_material':('video',{'topic':'不存在','audience':'创作者','platform':'短视频','duration':30,'style':'简洁'}),
 'video_failure':('video',{'topic':'字幕','audience':'创作者','platform':'短视频','duration':30,'style':'简洁','fail_tools':['estimate_duration']}),
 'video_too_short':('video',{'topic':'字幕','audience':'创作者','platform':'短视频','duration':0.5,'style':'简洁'}),
 'video_rule_failure':('video',{'topic':'字幕','audience':'创作者','platform':'短视频','duration':30,'style':'简洁','brand_rules':{'banned_phrases':['怎么使用']}}),
}
base=Path('examples')
for name,(task,request) in cases.items():
    (base/'inputs'/f'{name}.json').write_text(json.dumps(request,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with tempfile.TemporaryDirectory() as tmp:
        output=run(task,request,runtime=tmp)
    (base/'outputs'/f'{name}.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

for name in ('schedule_conflict', 'plan_normal'):
    task, request = cases[name]
    with tempfile.TemporaryDirectory() as tmp:
        first = run(task, request, runtime=tmp)
        second = run(task, request, runtime=tmp)
    (base/'outputs'/f'{name}_repeat.json').write_text(json.dumps({'first':first,'repeat':second},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

first_request = {'theme':'字幕活动','budget':10,'deadline_hours':4,'fail_tools':['generate_script']}
retry_request = {'theme':'字幕活动','budget':10,'deadline_hours':4}
for name,request in (('plan_resume_initial',first_request),('plan_resume_retry',retry_request)):
    (base/'inputs'/f'{name}.json').write_text(json.dumps(request,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
with tempfile.TemporaryDirectory() as runtime:
    first = run('plan',first_request,runtime=runtime,run_id='resume-demo')
    resumed = run('plan',retry_request,runtime=runtime,run_id='resume-demo',resume=True)
(base/'outputs'/'plan_resume.json').write_text(json.dumps({'first':first,'resumed':resumed},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

# Deliberately synchronize the first read in each thread so the persisted trace
# includes an actual SQLite write race and the losing agent's re-query.
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import src.tasks as task_tools

requests = [{'material_id':mid,'date':'2026-11-01','time':'20:00'} for mid in ('M01','M04')]
(base/'inputs'/'schedule_concurrent.json').write_text(json.dumps(requests,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
barrier, lock, count = Barrier(2, timeout=10), Lock(), [0]
original = task_tools.stored_schedule_drafts
def sync_first_reads(runtime):
    snapshot = original(runtime)
    with lock:
        count[0] += 1
        synchronize = count[0] <= 2
    if synchronize:
        barrier.wait()
    return snapshot
try:
    task_tools.stored_schedule_drafts = sync_first_reads
    with tempfile.TemporaryDirectory() as runtime:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, 'schedule', request, runtime=runtime) for request in requests]
            outputs = [future.result(timeout=15) for future in futures]
finally:
    task_tools.stored_schedule_drafts = original
(base/'outputs'/'schedule_concurrent.json').write_text(json.dumps(outputs,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
