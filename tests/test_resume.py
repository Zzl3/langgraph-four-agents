import json
import sqlite3
import subprocess
import sys
import time

from src.engine import run


def test_resume_from_failed_script_across_cli_processes(tmp_path):
    initial={'theme':'字幕活动','budget':10,'deadline_hours':4,'fail_tools':['generate_script']}
    retry={'theme':'字幕活动','budget':10,'deadline_hours':4}
    first_path=tmp_path/'first.json'; first_path.write_text(json.dumps(initial),encoding='utf-8')
    retry_path=tmp_path/'retry.json'; retry_path.write_text(json.dumps(retry),encoding='utf-8')
    base=[sys.executable,'-m','src.cli','plan','--runtime',str(tmp_path),'--run-id','campaign-1']
    first=json.loads(subprocess.run(base+['--input',str(first_path)],check=True,capture_output=True,text=True).stdout)
    assert first['result']['status']=='incomplete'
    assert [x['action'] for x in first['trace']]==['search_material','generate_script']
    assert first['trace'][-1]['observation']['ok'] is False
    resumed=json.loads(subprocess.run(base+['--input',str(retry_path),'--resume'],check=True,capture_output=True,text=True).stdout)
    assert resumed['result']['status']=='completed' and resumed['resumed'] is True
    assert sum(x['action']=='search_material' for x in resumed['trace'])==1
    attempts=[x for x in resumed['trace'] if x['action']=='generate_script']
    assert [x['observation']['ok'] for x in attempts]==[False,True]
    assert resumed['result']['plan']['cost']==8


def test_resume_respects_total_call_limit_and_input_identity(tmp_path):
    initial={'theme':'字幕','budget':10,'deadline_hours':4,'fail_tools':['generate_script']}
    first=run('plan',initial,max_calls=2,runtime=str(tmp_path),run_id='job')
    assert first['result']['status']=='incomplete' and len(first['trace'])==2
    valid={'theme':'字幕','budget':10,'deadline_hours':4}
    limited=run('plan',valid,max_calls=2,runtime=str(tmp_path),run_id='job',resume=True)
    assert limited['result']['status']=='incomplete' and limited['trace']==first['trace']
    mismatch=run('plan',dict(valid,budget=9),runtime=str(tmp_path),run_id='job',resume=True)
    assert mismatch['result']['status']=='incomplete' and '不一致' in mismatch['result']['reason']
    done=run('plan',valid,max_calls=20,runtime=str(tmp_path),run_id='job',resume=True)
    assert done['result']['status']=='completed' and sum(x['action']=='search_material' for x in done['trace'])==1


def test_resume_does_not_reset_relative_deadline(tmp_path):
    req={'theme':'字幕','budget':10,'deadline_hours':4,'fail_tools':['generate_script']}
    first=run('plan',req,runtime=str(tmp_path),run_id='deadline')
    assert first['result']['status']=='incomplete'
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        conn.execute('update plan_checkpoints set started_wall=? where run_id=?',(time.time()-5*3600,'deadline'))
    retry={'theme':'字幕','budget':10,'deadline_hours':4}
    result=run('plan',retry,runtime=str(tmp_path),run_id='deadline',resume=True)
    assert result['result']['status']=='incomplete'
    assert not any(x['action']=='save_plan' and x['observation'].get('ok') for x in result['trace'])


def test_checkpoint_failure_after_save_can_resume_idempotently(tmp_path, monkeypatch):
    import src.checkpoints
    original=src.checkpoints.advance
    def fail_after_save(runtime,run_id,digest,version,trace):
        if trace[-1]['action']=='save_plan':
            raise OSError('simulated checkpoint outage')
        return original(runtime,run_id,digest,version,trace)
    monkeypatch.setattr(src.checkpoints,'advance',fail_after_save)
    req={'theme':'字幕','budget':10,'deadline_hours':4}
    first=run('plan',req,runtime=str(tmp_path),run_id='checkpoint-failure')
    assert first['result']['status']=='incomplete' and first['result']['saved'] is True
    assert first['final_decision']['action']=='checkpoint_error'
    monkeypatch.setattr(src.checkpoints,'advance',original)
    resumed=run('plan',req,runtime=str(tmp_path),run_id='checkpoint-failure',resume=True)
    assert resumed['result']['status']=='completed' and resumed['result']['duplicate']
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        assert conn.execute("select count(*) from drafts where kind='plan'").fetchone()[0]==1


def test_resume_retries_failed_estimate_without_rerunning_script(tmp_path):
    req={'theme':'字幕','budget':10,'deadline_hours':4,'fail_tools':['estimate_cost']}
    first=run('plan',req,runtime=str(tmp_path),run_id='estimate')
    assert first['result']['status']=='incomplete'
    assert [x['args']['option'] for x in first['trace'] if x['action']=='estimate_cost']==['standard','economy']
    retry={'theme':'字幕','budget':10,'deadline_hours':4}
    result=run('plan',retry,runtime=str(tmp_path),run_id='estimate',resume=True)
    assert result['result']['status']=='completed' and result['result']['plan']['cost']==8
    assert sum(x['action']=='generate_script' for x in result['trace'])==1
    assert [(x['option'],x['status']) for x in result['result']['estimate_attempts']]==[
        ('standard','failed'),('economy','failed'),('economy','completed')]


def test_concurrent_resume_does_not_overwrite_checkpoint(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import src.checkpoints
    req={'theme':'字幕','budget':10,'deadline_hours':4,'fail_tools':['generate_script']}
    assert run('plan',req,runtime=str(tmp_path),run_id='race')['result']['status']=='incomplete'
    original=src.checkpoints.advance
    barrier=Barrier(2,timeout=10)
    def synchronized(runtime,run_id,digest,version,trace):
        if version==2:
            barrier.wait()
        return original(runtime,run_id,digest,version,trace)
    monkeypatch.setattr(src.checkpoints,'advance',synchronized)
    retry={'theme':'字幕','budget':10,'deadline_hours':4}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,'plan',retry,runtime=str(tmp_path),run_id='race',resume=True) for _ in range(2)]
        results=[f.result(timeout=15) for f in futures]
    assert sorted(x['result']['status'] for x in results)==['completed','incomplete']
    failed=next(x for x in results if x['result']['status']=='incomplete')
    assert failed['final_decision']['action']=='checkpoint_error' and '其他执行更新' in failed['result']['reason']
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        assert conn.execute("select count(*) from drafts where kind='plan'").fetchone()[0]==1


def test_resume_rejects_changed_material_or_cost_table(tmp_path):
    import copy
    catalog={'materials':[{'id':'A','description':'字幕功能','keywords':['字幕']}],
             'options':{'one':{'steps':[{'name':'剪辑','cost':4,'hours':1}]}}}
    req={'theme':'字幕','budget':10,'deadline_hours':4,'plan_data':catalog,'fail_tools':['generate_script']}
    assert run('plan',req,runtime=str(tmp_path),run_id='catalog')['result']['status']=='incomplete'
    changed=copy.deepcopy(req)
    changed.pop('fail_tools')
    changed['plan_data']['options']['one']['steps'][0]['cost']=5
    result=run('plan',changed,runtime=str(tmp_path),run_id='catalog',resume=True)
    assert result['result']['status']=='incomplete' and '不一致' in result['result']['reason']
    with sqlite3.connect(tmp_path/'drafts.sqlite3') as conn:
        assert conn.execute('select version from plan_checkpoints where run_id=?',('catalog',)).fetchone()[0]==2
