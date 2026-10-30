import hashlib


import json


import os


import sqlite3


from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'


def load(name):
    return json.loads((DATA / f'{name}.json').read_text(encoding='utf-8'))


def key_for(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def schedule_draft_key(item):
    return key_for({'material_id':item['id'], 'date':item['date'], 'time':item['time']})


def decode_schedule_draft(key, value):
    draft = json.loads(value)
    if not valid_draft_shape(draft):
        raise ValueError('本地排期草稿格式无效')
    ids = draft.get('material_ids')
    if not isinstance(ids,list) or len(ids)!=1:
        raise ValueError('本地排期草稿素材无效')
    item = {'id':ids[0], 'date':draft.get('date'), 'time':draft.get('requested_time')}
    suggested = {**item, 'time':draft.get('suggested_time')}
    if (not valid_schedule_item(item) or not valid_schedule_item(suggested)
        or schedule_draft_key(item) != key or draft.get('published') is not False
        or draft.get('status') not in ('ready','pending_confirmation')):
        raise ValueError('本地排期草稿数据不完整或幂等键不匹配')
    return draft


def verify_distinct_draft_slots(entries):
    used = set()
    for _, draft in entries:
        slot = (draft['date'],draft['suggested_time'])
        if slot in used:
            raise ValueError('本地已有草稿时段冲突，需先处理已有数据')
        used.add(slot)


def stored_schedule_drafts(runtime):
    """Read local draft reservations; the external schedule remains a separate input."""
    path = os.path.join(runtime, 'drafts.sqlite3')
    if not os.path.exists(path):
        return []
    with sqlite3.connect(path, timeout=10) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='drafts'").fetchone():
            return []
        entries = [(key, decode_schedule_draft(key,value)) for key,value in conn.execute(
            "SELECT key,value FROM drafts WHERE kind='schedule'")]
        verify_distinct_draft_slots(entries)
        return entries


def save_schedule(key, draft, runtime, fail=False):
    """Check and persist a draft under one SQLite write lock, including idempotent retries."""
    if fail:
        return {'ok':False,'error':'模拟保存失败','saved':False}
    os.makedirs(runtime, exist_ok=True)
    encoded = json.dumps(draft, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(os.path.join(runtime,'drafts.sqlite3'), timeout=10) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS drafts (kind TEXT, key TEXT, value TEXT, PRIMARY KEY(kind,key))')
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("SELECT key,value FROM drafts WHERE kind='schedule'").fetchall()
        decoded = [(old_key, old_value, decode_schedule_draft(old_key,old_value)) for old_key,old_value in rows]
        verify_distinct_draft_slots([(old_key,old) for old_key,_,old in decoded])
        for old_key, old_value, old in decoded:
            if old_key == key:
                if old != draft:
                    return {'ok':False,'error':'幂等键对应不同内容','saved':False,'code':'idempotency_conflict'}
                return {'ok':True,'saved':True,'duplicate':True,'key':key}
        for old_key, old_value, old in decoded:
            if old.get('date') == draft['date'] and old.get('suggested_time') == draft['suggested_time']:
                return {'ok':False,'error':'该时段已被其他本地草稿占用，请重新查询',
                        'saved':False,'code':'slot_conflict'}
        conn.execute('INSERT INTO drafts VALUES (?,?,?)', ('schedule',key,encoded))
        return {'ok':True,'saved':True,'duplicate':False,'key':key}


def schedule_requests(req):
    if 'items' in req:
        items = req['items']
        if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
            return []
        return [{'id': x.get('id'), 'date': x.get('date'), 'time': x.get('time')}
                for x in items]
    ids = req.get('material_ids') or ([req['material_id']] if req.get('material_id') else [])
    if not isinstance(ids, list):
        return []
    return [{'id': mid, 'date': req.get('date'), 'time': req.get('time')} for mid in ids]


def valid_schedule_item(item):
    from datetime import date, time
    import re
    try:
        if not isinstance(item['id'], str) or not item['id'].strip(): return False
        if not isinstance(item['date'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',item['date']): return False
        if not isinstance(item['time'], str) or not re.fullmatch(r'\d{2}:\d{2}',item['time']): return False
        date.fromisoformat(item['date'])
        time.fromisoformat(item['time'])
        return True
    except (ValueError, TypeError, KeyError):
        return False


def pending_confirmation(requested, suggested):
    return f'原要求 {requested} 不可用，建议 {suggested}，等待用户确认'


def safe_schedule_reason(reason, fallback):
    disallowed = ('已发布','已经发布','发布成功','已上线','已确认','已经确认','已同意','同意修改')
    if not isinstance(reason,str) or any(text in reason for text in disallowed):
        return fallback
    return reason


def valid_draft_shape(draft):
    return draft_validation_error(draft) is None


def draft_validation_error(draft):
    fields = {'material_ids','date','requested_time','suggested_time','status','confirmation','published'}
    if not isinstance(draft,dict) or set(draft) != fields:
        return '草稿必须包含且仅包含 material_ids、date、requested_time、suggested_time、status、confirmation、published'
    if draft['published'] is not False:
        return '草稿 published 必须是 JSON false，不得标记已发布'
    ids = draft['material_ids']
    if not isinstance(ids,list) or len(ids)!=1:
        return '草稿 material_ids 必须只包含一个素材 ID'
    request = {'id':ids[0], 'date':draft['date'], 'time':draft['requested_time']}
    suggested = {**request, 'time':draft['suggested_time']}
    if not valid_schedule_item(request) or not valid_schedule_item(suggested):
        return '草稿素材 ID、日期或时段无效，日期须为 YYYY-MM-DD，时段须为 HH:MM'
    if draft['suggested_time'] == draft['requested_time']:
        if draft['status']!='ready' or draft['confirmation'] is not None:
            return '原时段未改变：status 必须为 ready，confirmation 必须为 JSON null，不需等待时段变更确认'
        return None
    if draft['status']!='pending_confirmation' or draft['confirmation']!=pending_confirmation(draft['requested_time'],draft['suggested_time']):
        return '替代时段须为 pending_confirmation，确认文案必须是：'+pending_confirmation(draft['requested_time'],draft['suggested_time'])
    return None


def includes_fields(value, expected):
    if isinstance(expected, dict):
        return isinstance(value, dict) and all(k in value and includes_fields(value[k], v) for k,v in expected.items())
    return value == expected


def act(action, args=None, reason=''):
    return {'action':action,'args':args or {},'reason':reason}


def finish(result, reason='已根据工具结果结束'):
    return {'action':'finish','result':result,'reason':reason}


def last(trace, action):
    return next((x['observation'] for x in reversed(trace) if x['action']==action), None)


def fail_result(t):
    return {'status':'incomplete','reason':f"{t['action']} 失败: {t['observation'].get('error','未知错误')}"}


def partial_schedule(req, trace):
    """Summarize actual per-item work when a tool stops the workflow."""
    items = []
    for item in schedule_requests(req):
        mid = item['id']
        read = next((x for x in reversed(trace) if x['action']=='read_material' and x['args'].get('id')==mid), None)
        check = next((x for x in reversed(trace) if x['action']=='check_rules' and x['args'].get('material',{}).get('id')==mid), None)
        saves = [x for x in trace if x['action']=='save_draft' and x['args'].get('draft',{}).get('material_ids')==[mid]]
        entry = {'id':mid,'date':item['date'],'requested_time':item['time'],
                 'review_status':check['observation']['status'] if check and check['observation'].get('ok')
                                 else 'failed' if read and not read['observation'].get('ok') or check and not check['observation'].get('ok')
                                 else 'not_checked',
                 'risk_reasons':check['observation'].get('risks',[]) if check else [],
                 'missing':check['observation'].get('missing',[]) if check else [],
                 'saved':False,'suggested_time':None,'confirmation':None}
        if saves:
            successful = next((x for x in reversed(saves) if x['observation'].get('ok')),None)
            attempt = successful or saves[-1]
            draft = attempt['args']['draft']
            entry.update({'suggested_time':draft.get('suggested_time'), 'confirmation':draft.get('confirmation'),
                          'saved':bool(attempt['observation'].get('saved',False))})
            if successful: entry['duplicate'] = attempt['observation'].get('duplicate',False)
            if not saves[-1]['observation'].get('ok'): entry['save_error'] = saves[-1]['observation'].get('error')
        if read and not read['observation'].get('ok'): entry['error'] = read['observation'].get('error')
        if check and not check['observation'].get('ok'): entry['error'] = check['observation'].get('error')
        if entry['review_status']=='approved' and not entry['saved'] and not saves:
            slot = next((x['observation'] for x in reversed(trace) if x['action']=='query_slots'
                         and x['args'].get('date')==item['date'] and x['args'].get('material_id')==mid
                         and x['observation'].get('ok')),None)
            if slot:
                used = set(slot['occupied']) | {x['args']['draft']['suggested_time'] for x in trace
                       if x['action']=='save_draft' and x['observation'].get('ok') and x['args']['draft']['date']==item['date']}
                if item['time'] in used and not any(t not in used for t in slot['available']):
                    entry['reason']='无可用替代时段'
        items.append(entry)
    return items


def decide_schedule(req, trace):
    requests = schedule_requests(req)
    if not requests or any(not valid_schedule_item(x) for x in requests):
        return finish({'status':'incomplete','reason':'缺少素材 ID 或日期、时段格式无效（需 YYYY-MM-DD、HH:MM）'})
    ids = [x['id'] for x in requests]
    if len(ids) != len(set(ids)):
        return finish({'status':'incomplete','reason':'素材 ID 重复，请去重后提交'})
    results = []
    for mid in ids:
        read = next((x['observation'] for x in trace if x['action']=='read_material' and x['args'].get('id')==mid),None)
        if read is None: return act('read_material',{'id':mid},'读取该素材')
        material = read['material']
        checked = next((x['observation'] for x in trace if x['action']=='check_rules' and x['args'].get('material',{}).get('id')==mid),None)
        if checked is None: return act('check_rules',{'material':material},'依据已读取素材检查缺失和风险')
        item_req = next(x for x in requests if x['id']==mid)
        results.append({'id':mid,'date':item_req['date'],'requested_time':item_req['time'],
                        'review_status':checked['status'],'risk_reasons':checked['risks'],
                        'missing':checked['missing'],'saved':False,'suggested_time':None,'confirmation':None})
    approved = [x for x in results if x['review_status']=='approved']
    if not approved:
        return finish({'status':'incomplete','items':results,'reason':'素材审核未通过或资料不完整','saved':False})
    for item in approved:
        mid = item['id']
        item_req = next(x for x in requests if x['id']==mid)
        date = item_req['date']
        slots = next((x['observation'] for x in reversed(trace) if x['action']=='query_slots'
                      and x['args'].get('date')==date and x['args'].get('material_id')==mid),None)
        if slots is None: return act('query_slots',{'date':date,'material_id':mid},'查询该素材所在日期的排期冲突')
        used = set(slots['occupied']) | {x['args']['draft']['suggested_time'] for x in trace
                if x['action']=='save_draft' and x['observation'].get('ok') and x['args']['draft']['date']==date}
        saved_entry = next((x for x in trace if x['action']=='save_draft' and x['observation'].get('ok')
                            and x['args'].get('draft',{}).get('material_ids')==[mid]),None)
        if saved_entry:
            draft = saved_entry['args']['draft']
            item.update({'suggested_time':draft['suggested_time'],'confirmation':draft['confirmation'],
                         'saved':saved_entry['observation']['saved'],'duplicate':saved_entry['observation']['duplicate']})
            continue
        requested = item_req['time']
        selected = requested if requested not in used else next((t for t in slots['available'] if t not in used),None)
        if selected is None:
            item['reason'] = '无可用替代时段'
            continue
        confirmation = None if selected == requested else pending_confirmation(requested,selected)
        draft = {'material_ids':[mid],'date':date,'requested_time':requested,'suggested_time':selected,
                 'status':'pending_confirmation' if confirmation else 'ready','confirmation':confirmation,'published':False}
        return act('save_draft',{'draft':draft},'保存未发布草稿；替代时段需要确认')
    all_saved = all(x['saved'] for x in results)
    pending = any(x['confirmation'] for x in results)
    result = {'status': 'incomplete' if not all_saved else 'pending_confirmation' if pending else 'ready',
              'items':results,'saved':all_saved,'published':False}
    if len(results)==1:
        result.update({k:results[0].get(k) for k in ('suggested_time','confirmation','duplicate')})
    if not all_saved: result['reason']='部分素材审核未通过、资料不足或无可用时段'
    return finish(result)

def tool_specs(task):
    if task != 'schedule':
        raise ValueError('本项目仅实现素材排期任务')
    return {
        'read_material':'{id: 素材 ID}',
        'check_rules':'{material: read_material 返回的完整对象}',
        'query_slots':'{date: 用户要求的日期,material_id: 素材 ID（用于幂等重查）}',
        'save_draft':'{draft: {material_ids:[单个素材ID],date,requested_time,suggested_time,status:ready或pending_confirmation,confirmation:文字或null,published:false}}',
    }


def execute_tool(task, action, args, state):
    if task != 'schedule' or action not in tool_specs('schedule'):
        return {'ok':False,'error':'不存在或不属于此项目的工具'}
    req=state['request']
    if action in req.get('fail_tools',[]):
        return {'ok':False,'error':f'模拟工具失败: {action}'}
    if action in req.get('fail_once',[]) and not any(x['action']==action for x in state['trace']):
        return {'ok':False,'error':f'模拟一次性工具失败: {action}'}
    if any(rule.get('action')==action and includes_fields(args,rule.get('args',{}))
           for rule in req.get('fail_when',[]) if isinstance(rule,dict)):
        return {'ok':False,'error':f'模拟指定参数工具失败: {action}'}
    if action=='read_material':
        materials=req.get('materials',load('materials'))
        if not isinstance(materials,dict): return {'ok':False,'error':'素材必须为按 ID 索引的对象'}
        item=materials.get(args['id'])
        if item and (not isinstance(item,dict) or item.get('id')!=args['id']):
            return {'ok':False,'error':'素材 ID 与元数据不一致'}
        return {'ok':bool(item),'material':item,**({} if item else {'error':'素材不存在'})}
    if action=='check_rules':
        material=args['material']
        reads=[x['observation'].get('material') for x in state['trace']
               if x['action']=='read_material' and x['observation'].get('ok')]
        if material not in reads: return {'ok':False,'error':'审核内容与已读取的素材不一致'}
        rules=load('schedule_rules')
        missing=[key for key in rules['required_fields'] if not material.get(key)]
        blocked=set(rules['blocked_terms'])|set(req.get('blocked_terms',[]))
        content=material.get('content','')
        risks=[f'包含违规宣传词：{term}' for term in sorted(blocked) if term and term in content]
        return {'ok':True,'missing':missing,'risks':risks,
                'status':'blocked' if risks else 'incomplete' if missing else 'approved'}
    if action=='query_slots':
        if args.get('date') not in {x['date'] for x in schedule_requests(req)}:
            return {'ok':False,'error':'查询日期与用户要求不一致'}
        slots=req.get('slots_by_date',{}).get(args['date'],{})
        requested_id=args.get('material_id')
        matches=[x for x in schedule_requests(req) if x['id']==requested_id and x['date']==args['date']]
        if requested_id is not None and len(matches)!=1:
            return {'ok':False,'error':'查询素材与日期不匹配'}
        own_keys={schedule_draft_key(x) for x in matches if valid_schedule_item(x)}
        local=[draft['suggested_time'] for key,draft in stored_schedule_drafts(state['runtime'])
               if key not in own_keys and draft.get('date')==args['date']]
        occupied=set(slots.get('occupied',req.get('occupied',['18:00'])))|set(local)
        return {'ok':True,'occupied':sorted(occupied),'local_draft_slots':sorted(set(local)),
                'available':slots.get('available',req.get('available',['19:00','20:00']))}
    draft=args['draft']
    error=draft_validation_error(draft)
    if error: return {'ok':False,'error':error}
    requests=schedule_requests(req)
    checked=[x for x in state['trace'] if x['action']=='check_rules' and x['observation'].get('status')=='approved']
    checked_ids={x['args'].get('material',{}).get('id') for x in checked}
    ids=draft.get('material_ids',[])
    request_item=next((x for x in requests if len(ids)==1 and x['id']==ids[0]),None)
    slot_result=next((x['observation'] for x in reversed(state['trace'])
                      if x['action']=='query_slots' and x['args'].get('date')==draft.get('date')
                      and x['args'].get('material_id') in (None,ids[0] if len(ids)==1 else None)),None)
    if (not request_item or not valid_schedule_item(request_item) or ids[0] not in checked_ids
        or not slot_result or not slot_result.get('ok')):
        return {'ok':False,'error':'缺少素材审核或时段查询证据'}
    if draft.get('date')!=request_item['date'] or draft.get('requested_time')!=request_item['time']:
        return {'ok':False,'error':'草稿与用户要求不一致'}
    if not valid_schedule_item({**request_item,'time':draft.get('suggested_time')}):
        return {'ok':False,'error':'建议时段格式无效'}
    changed=draft.get('suggested_time')!=request_item['time']
    if ((changed and (draft.get('status')!='pending_confirmation' or not draft.get('confirmation')
                      or draft.get('suggested_time') not in slot_result['available']))
        or draft.get('suggested_time') in slot_result['occupied']):
        return {'ok':False,'error':'时段冲突或替代时段缺少确认状态'}
    previous=[x['args']['draft'] for x in state['trace'] if x['action']=='save_draft'
              and x['observation'].get('ok') and x['args']['draft']['material_ids']!=ids]
    if any(d['date']==draft['date'] and d['suggested_time']==draft.get('suggested_time') for d in previous):
        return {'ok':False,'error':'与本次其他素材的建议时段冲突'}
    if (changed and request_item['time'] not in slot_result['occupied']
        and not any(d['date']==draft['date'] and d['suggested_time']==request_item['time'] for d in previous)):
        return {'ok':False,'error':'原要求时段可用，不能虚称冲突并更改建议'}
    same=[x['args']['draft'] for x in state['trace'] if x['action']=='save_draft'
          and x['observation'].get('ok') and x['args']['draft']['material_ids']==ids]
    if any(item!=draft for item in same): return {'ok':False,'error':'本次请求中该素材已有不同草稿'}
    return save_schedule(schedule_draft_key(request_item),draft,state['runtime'],req.get('save_fail',False))






def mock_decide(state):
    req,trace=state['request'],state['trace']
    if trace and not trace[-1]['observation'].get('ok'):
        last_entry=trace[-1]
        if last_entry['action']=='save_draft' and last_entry['observation'].get('code')=='slot_conflict':
            draft=last_entry['args']['draft']
            return act('query_slots',{'date':draft['date'],'material_id':draft['material_ids'][0]},
                       '保存时发现并发占用，重新查询时段')
        result=fail_result(last_entry)
        result['completed_steps']=[x['action'] for x in trace if x['observation'].get('ok')]
        result['step_results']=[x for x in trace if x['observation'].get('ok')]
        result.update({'items':partial_schedule(req,trace),'saved':False,'published':False})
        if last_entry['action']=='save_draft': result['saved']=False
        return finish(result,'工具失败，保留已成功步骤并停止')
    return decide_schedule(req,trace)


def validate_final(state,result):
    if not isinstance(result,dict) or result.get('status') not in {'incomplete','ready','pending_confirmation'}:
        return {'status':'incomplete','reason':'结果格式或状态无效','code':'invalid_result'}
    trace=state['trace']; status=result['status']
    if result.get('published') is True or ('published' in result and result['published'] is not False):
        status='invalid'
    actual=partial_schedule(state['request'],trace)
    saved=bool(actual) and all(item['saved'] for item in actual)
    approved=bool(actual) and all(item['review_status']=='approved' for item in actual)
    pending=any(item['confirmation'] for item in actual if item['saved'])
    if status in ('ready','pending_confirmation') and (
        not saved or not approved or (status=='ready' and pending) or (status=='pending_confirmation' and not pending)):
        status='invalid'
    if status=='invalid':
        rejected={'status':'incomplete','reason':'最终结果与工具证据不一致，已拒绝未经验证的结论','code':'unverified_result',
                  'items':actual,'saved':saved,'published':False}
        return rejected
    reason=safe_schedule_reason(result.get('reason'),
        '任务未完成，请查看逐项状态和工具记录' if status=='incomplete' else None)
    normalized={'status':status,'items':actual,'saved':saved,'published':False}
    if reason: normalized['reason']=reason
    if len(actual)==1:
        normalized.update({key:actual[0].get(key) for key in ('suggested_time','confirmation','duplicate')})
    if 'completed_steps' in result: normalized['completed_steps']=[x['action'] for x in trace if x['observation'].get('ok')]
    if 'step_results' in result: normalized['step_results']=[x for x in trace if x['observation'].get('ok')]
    return normalized
