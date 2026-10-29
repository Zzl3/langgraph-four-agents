"""Synthetic business tools and an explicitly non-LLM offline decision policy."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
NEUTRAL_NARRATIONS = ('一起看看这项功能。', '你会怎么使用？', '来看看这个功能！', '你想怎么用？')
AUDIENCE_INTROS = {
    '新手': ('新手，一起看看这项功能。','新手，来看看这个功能！'),
    '创作者': ('创作者，一起看看这项功能。','创作者，来看看这个功能！'),
    '运营': ('运营同学，一起看看这项功能。','运营同学，来看看这个功能！'),
}

def load(name):
    return json.loads((DATA / f'{name}.json').read_text(encoding='utf-8'))

def save(kind, key, value, runtime, fail=False):
    if fail:
        return {'ok':False, 'error':'模拟保存失败', 'saved':False}
    os.makedirs(runtime, exist_ok=True)
    with sqlite3.connect(os.path.join(runtime, 'drafts.sqlite3')) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS drafts (kind TEXT, key TEXT, value TEXT, PRIMARY KEY(kind,key))')
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        cursor = conn.execute('INSERT OR IGNORE INTO drafts VALUES (?,?,?)', (kind, key, encoded))
        row = conn.execute('SELECT value FROM drafts WHERE kind=? AND key=?', (kind,key)).fetchone()
        if row[0] != encoded:
            return {'ok':False, 'error':'幂等键对应不同内容', 'saved':False}
        return {'ok':True,'saved':True,'duplicate':cursor.rowcount == 0,'key':key}

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

def absolute_deadline_hours(value):
    from datetime import datetime, timezone
    if not isinstance(value, str):
        raise TypeError('截止时间须为带时区的 ISO 8601 字符串')
    deadline = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError('截止时间必须包含时区')
    return (deadline - datetime.now(timezone.utc)).total_seconds() / 3600

def plan_deadline_field(req):
    return {'deadline':req['deadline']} if req.get('deadline') is not None else {'deadline_hours':req.get('deadline_hours')}

def plan_catalog(req):
    catalog = req.get('plan_data') if 'plan_data' in req else load('plan')
    if not isinstance(catalog,dict) or not isinstance(catalog.get('materials'),list) or not isinstance(catalog.get('options'),dict):
        raise ValueError('素材与方案费用表格式无效')
    seen = set()
    for material in catalog['materials']:
        if (not isinstance(material,dict) or not isinstance(material.get('id'),str) or not material['id'].strip()
            or not isinstance(material.get('description'),str) or not material['description'].strip()
            or not isinstance(material.get('keywords'),list) or not material['keywords']
            or any(not isinstance(k,str) or not k.strip() for k in material['keywords']) or material['id'] in seen):
            raise ValueError('素材需有唯一 ID、描述和非空关键词列表')
        seen.add(material['id'])
    if any(not isinstance(name,str) or not name.strip() for name in catalog['options']):
        raise ValueError('方案名称无效')
    return catalog

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

def finite_number(value):
    import math
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def decimal_result(value):
    """Convert finite decimal arithmetic back to a JSON-compatible number."""
    return int(value) if value == value.to_integral_value() else float(value)

def exact_sum(values):
    from decimal import Decimal
    return decimal_result(sum((Decimal(str(v)) for v in values), Decimal(0)))

def positive_gap(value, limit):
    from decimal import Decimal
    return decimal_result(max(Decimal(0), Decimal(str(value)) - Decimal(str(limit))))

def valid_plan_steps(steps):
    if not isinstance(steps,list) or not steps:
        return False
    seen = set()
    for step in steps:
        if (not isinstance(step,dict) or not {'name','cost','hours'} <= set(step)
            or set(step) - {'name','cost','hours','depends_on'}
            or not isinstance(step['name'],str) or not step['name'].strip() or step['name'] in seen
            or not finite_number(step['cost']) or step['cost'] < 0
            or not finite_number(step['hours']) or step['hours'] < 0):
            return False
        dependencies = step.get('depends_on',[])
        if (not isinstance(dependencies,list) or any(not isinstance(x,str) or x not in seen for x in dependencies)
            or len(dependencies)!=len(set(dependencies))):
            return False
        seen.add(step['name'])
    return True

def includes_fields(value, expected):
    if isinstance(expected, dict):
        return isinstance(value, dict) and all(k in value and includes_fields(value[k], v) for k,v in expected.items())
    return value == expected

def query_grams(query):
    import re
    query = str(query).lower()
    stop = {'公司','产品','制度','规定','规则','怎么','如何','什么','多少','少天',
            '多久','哪里','哪些','是否','可以','有关','问题','一个','一下','请问','期限'}
    tokens = re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', query)
    grams = set()
    for token in tokens:
        if token.isascii():
            if len(token) >= 2: grams.add(token)
        else:
            grams.update(token[i:i+2] for i in range(len(token)-1))
    grams -= stop
    return grams

def qa_has_subject(text):
    """Detect whether a short question supplies a topic; intentionally conservative."""
    import re
    if not isinstance(text, str):
        return False
    cleaned = re.sub(r'请问|我想知道|这个|那个|上述|它的|它|该|那|这|其|'
                     r'如何|怎么|多少|多久|几天|期限|周期|价格|语言|语种|地址|位置|'
                     r'在哪里|哪些|什么|是否|可以|支持|开启|关闭|安装|下载|吗|呢|的', '', text.lower())
    cleaned = re.sub(r'[^\w\u4e00-\u9fff]', '', cleaned)
    return len(cleaned) >= 2

def qa_query(req):
    question = req.get('question', '')
    if not isinstance(question,str):
        return ''
    question = question.strip()
    if qa_has_subject(question):
        return question
    history = req.get('history', [])
    if not isinstance(history, list):
        return question
    for message in reversed(history):
        if not isinstance(message,dict) or message.get('role','user') not in ('user','human'):
            continue
        content = message.get('content')
        if isinstance(content,str) and qa_has_subject(content):
            return f'{question} {content.strip()}'
    return question

def search_documents(query, docs):
    """Simple lexical retrieval plus a small explicit synonym map; not semantic search."""
    query = str(query).lower()
    synonyms = {'报销': ('报销','报账','费用','发票'),
                '年假': ('年假','休假','假期')}
    topics = [key for key, words in synonyms.items() if any(word in query for word in words)]
    grams = query_grams(query)
    matches = []
    for doc in docs:
        content = (str(doc.get('title', '')) + ' ' + str(doc.get('text', ''))).lower()
        if any(topic in content for topic in topics) or any(gram in content for gram in grams):
            matches.append({'id':doc['id'], 'date':doc['date'], 'title':doc['title']})
    return matches

def question_has_evidence(query, doc):
    """Conservative checks for common information needs, not semantic entailment."""
    text = str(doc.get('text', '')).lower()
    query = query.lower()
    if any(word in text for word in ('尚未明确','未明确','暂未确定','无法确定')):
        return False
    requirements = [
        (('语言','语种'), ('语言','语种','中文','英文','日文')),
        (('地址','在哪里','位置'), ('地址','位于','位置','地点')),
        (('价格','多少钱','费用多少'), ('价格','售价','费用','元')),
        (('多久','多少天','期限','周期'), ('期限','周期','天','小时','分钟','个月')),
        (('开启','打开'), ('开启','打开','启用')),
        (('关闭','关掉'), ('关闭','关掉','停用')),
        (('安装','下载'), ('安装','下载')),
    ]
    matched_intent = False
    for triggers, evidence in requirements:
        if any(t in query for t in triggers) and not any(e in text for e in evidence):
            return False
        if any(t in query for t in triggers):
            matched_intent = True
    matches = {gram for gram in query_grams(query) if gram in text}
    return len(matches) >= 2 or (matched_intent and bool(matches))

def documents_conflict(docs):
    """Detect a few explicit contradictions; absence of detection is not proof of agreement."""
    import re
    quantities = []
    polarities = []
    quantity_shapes = []
    polarity_shapes = []
    for doc in docs:
        text = str(doc.get('text',''))
        values = {}
        for number, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(天|日|小时|分钟|个月|月|年)', text):
            unit = {'日':'天','个月':'月'}.get(unit,unit)
            values.setdefault(unit,set()).add(float(number))
        quantities.append(values)
        shape = re.sub(r'(\d+(?:\.\d+)?)\s*(天|日|小时|分钟|个月|月|年)', lambda m:'#'+{'日':'天','个月':'月'}.get(m.group(2),m.group(2)), text)
        quantity_shapes.append(re.sub(r'[\s，。,.！!？?]', '', shape))
        negative = any(w in text for w in ('不允许','不可以','不支持','禁止'))
        positive = any(w in text for w in ('允许','可以','支持')) and not negative
        polarities.append('negative' if negative else 'positive' if positive else None)
        shape = text.replace('不允许','允许').replace('不可以','可以').replace('不支持','支持').replace('禁止','允许')
        polarity_shapes.append(re.sub(r'[\s，。,.！!？?]', '', shape))
    for i in range(len(docs)):
        for j in range(i+1,len(docs)):
            for unit in quantities[i].keys() & quantities[j].keys():
                if quantity_shapes[i] == quantity_shapes[j] and quantities[i][unit].isdisjoint(quantities[j][unit]): return True
            if polarity_shapes[i] == polarity_shapes[j] and {polarities[i],polarities[j]} == {'positive','negative'}: return True
    return False

def tool_specs(task):
    return {
      'schedule': {'read_material':'{id: 素材 ID}', 'check_rules':'{material: read_material 返回的完整对象}', 'query_slots':'{date: 素材要求的日期,material_id:该素材ID（用于幂等重查）}', 'save_draft':'{draft: {material_ids:[单个素材ID],date,requested_time,suggested_time,status:ready或pending_confirmation,confirmation:文字或null,published:false}}'},
      'plan': {'search_material':'{query:用户主题}', 'generate_script':'{material_id:检索结果ID,option:费用表中的方案名}', 'estimate_cost':'{option:费用表中的方案名}', 'save_plan':'{plan: {theme,material_id,option,budget,deadline或deadline_hours,cost,hours,steps,task_step_status,script}}，task_step_status 按 steps 顺序填写 {name,status:planned}；steps/cost/hours来自估价、material_id和script来自生成结果'},
      'qa': {'search_docs':'{query:完整检索问题}', 'read_doc':'{id:文档ID}'},
      'video': {'search_product':'{query:主题或改写查询}', 'estimate_duration':'{scenes:[{seconds:正数,visual:文字卡片、竖屏文字卡片或纯色背景,narration:素材原文或允许的中性旁白,source_id:素材ID或null}]}', 'check_brand':'{scenes:已估时的相同分镜,source_ids:[素材ID]}'},
    }[task]

def execute_tool(task, action, args, state):
    if action not in tool_specs(task):
        return {'ok':False,'error':'不存在或不属于此任务的工具'}
    req = state['request']
    if action in req.get('fail_tools', []):
        return {'ok':False,'error':f'模拟工具失败: {action}'}
    if action in req.get('fail_once', []) and not any(x['action'] == action for x in state['trace']):
        return {'ok':False,'error':f'模拟一次性工具失败: {action}'}
    if any(rule.get('action') == action and includes_fields(args, rule.get('args', {}))
           for rule in req.get('fail_when', []) if isinstance(rule, dict)):
        return {'ok':False,'error':f'模拟指定参数工具失败: {action}'}
    if task == 'schedule':
        if action == 'read_material':
            materials = req.get('materials', load('materials'))
            if not isinstance(materials, dict):
                return {'ok':False,'error':'素材必须为按 ID 索引的对象'}
            item = materials.get(args['id'])
            if item and (not isinstance(item,dict) or item.get('id') != args['id']):
                return {'ok':False,'error':'素材 ID 与元数据不一致'}
            return {'ok':bool(item), 'material':item, **({} if item else {'error':'素材不存在'})}
        if action == 'check_rules':
            m = args['material']
            reads = [x['observation'].get('material') for x in state['trace']
                     if x['action'] == 'read_material' and x['observation'].get('ok')]
            if m not in reads:
                return {'ok': False, 'error': '审核内容与已读取的素材不一致'}
            rules = load('schedule_rules')
            missing = [k for k in rules['required_fields'] if not m.get(k)]
            blocked_terms = set(rules['blocked_terms']) | set(req.get('blocked_terms', []))
            risks = [f'包含违规宣传词：{term}' for term in sorted(blocked_terms) if term and term in m.get('content','')]
            return {'ok':True,'missing':missing,'risks':risks,'status':'blocked' if risks else 'incomplete' if missing else 'approved'}
        if action == 'query_slots':
            if args.get('date') not in {x['date'] for x in schedule_requests(req)}:
                return {'ok':False,'error':'查询日期与用户要求不一致'}
            slots = req.get('slots_by_date', {}).get(args['date'], {})
            requested_id = args.get('material_id')
            matches = [x for x in schedule_requests(req) if x['id']==requested_id and x['date']==args['date']]
            if requested_id is not None and len(matches) != 1:
                return {'ok':False,'error':'查询素材与日期不匹配'}
            own_keys = {schedule_draft_key(x) for x in matches if valid_schedule_item(x)}
            local = [draft['suggested_time'] for key,draft in stored_schedule_drafts(state['runtime'])
                     if key not in own_keys and draft.get('date') == args['date']]
            occupied = set(slots.get('occupied', req.get('occupied', ['18:00']))) | set(local)
            return {'ok':True,'occupied':sorted(occupied),'local_draft_slots':sorted(set(local)),
                    'available':slots.get('available', req.get('available', ['19:00','20:00']))}
        if action == 'save_draft':
            draft = args['draft']
            error = draft_validation_error(draft)
            if error:
                return {'ok':False,'error':error}
            requests = schedule_requests(req)
            checks = [x for x in state['trace'] if x['action']=='check_rules' and x['observation'].get('status')=='approved']
            checked_ids = {x['args'].get('material',{}).get('id') for x in checks}
            draft_ids = draft.get('material_ids', [])
            request_item = next((x for x in requests if len(draft_ids) == 1 and x['id'] == draft_ids[0]), None)
            slot_result = next((x['observation'] for x in reversed(state['trace']) if x['action']=='query_slots'
                                and x['args'].get('date')==draft.get('date')
                                and x['args'].get('material_id') in (None, draft_ids[0] if len(draft_ids)==1 else None)),None)
            if not request_item or not valid_schedule_item(request_item) or draft_ids[0] not in checked_ids or not slot_result or not slot_result.get('ok'):
                return {'ok':False,'error':'缺少素材审核或时段查询证据'}
            if draft.get('date') != request_item['date'] or draft.get('requested_time') != request_item['time']:
                return {'ok':False,'error':'草稿与用户要求不一致'}
            if not valid_schedule_item({**request_item, 'time': draft.get('suggested_time')}):
                return {'ok':False,'error':'建议时段格式无效'}
            changed = draft.get('suggested_time') != request_item['time']
            if (changed and (draft.get('status') != 'pending_confirmation' or not draft.get('confirmation')
                             or draft.get('suggested_time') not in slot_result['available'])) or draft.get('suggested_time') in slot_result['occupied']:
                return {'ok':False,'error':'时段冲突或替代时段缺少确认状态'}
            previous = [x['args']['draft'] for x in state['trace'] if x['action']=='save_draft'
                        and x['observation'].get('ok') and x['args']['draft']['material_ids'] != draft_ids]
            if any(d['date'] == draft['date'] and d['suggested_time'] == draft.get('suggested_time') for d in previous):
                return {'ok':False,'error':'与本次其他素材的建议时段冲突'}
            if (changed and request_item['time'] not in slot_result['occupied']
                and not any(d['date']==draft['date'] and d['suggested_time']==request_item['time'] for d in previous)):
                return {'ok':False,'error':'原要求时段可用，不能虚称冲突并更改建议'}
            same_material = [x['args']['draft'] for x in state['trace'] if x['action']=='save_draft'
                             and x['observation'].get('ok') and x['args']['draft']['material_ids'] == draft_ids]
            if any(d != draft for d in same_material):
                return {'ok':False,'error':'本次请求中该素材已有不同草稿'}
            return save_schedule(schedule_draft_key(request_item), draft, state['runtime'], req.get('save_fail',False))
    if task == 'plan':
        if action == 'search_material':
            if args.get('query') != req.get('theme'):
                return {'ok':False,'error':'检索主题与用户主题不一致'}
            catalog = plan_catalog(req)
            query = args['query'].casefold()
            matches = [m for m in catalog['materials'] if any(keyword.casefold() in query for keyword in m['keywords'])]
            return {'ok':True,'materials':matches,'available_options':list(catalog['options'])}
        if action == 'generate_script':
            found = last(state['trace'], 'search_material')
            item = next((x for x in (found or {}).get('materials',[]) if x['id']==args['material_id']),None)
            if not found or not found.get('ok') or not item or args.get('option') not in found.get('available_options',[]):
                return {'ok':False,'error':'素材必须先经过检索'}
            return {'ok':True,'material_id':item['id'],'script':f"围绕{req.get('theme','')}介绍{item['description']}"}
        if action == 'estimate_cost':
            option = plan_catalog(req)['options'].get(args['option'])
            if not isinstance(option,dict) or not isinstance(option.get('steps'),list) or not option['steps']:
                return {'ok':False,'error':'方案不存在或没有步骤'}
            steps = option['steps']
            if not valid_plan_steps(steps):
                return {'ok':False,'error':'方案步骤的名称、依赖、费用或耗时无效'}
            cost, hours = exact_sum(x['cost'] for x in steps), exact_sum(x['hours'] for x in steps)
            if not finite_number(cost) or not finite_number(hours):
                return {'ok':False,'error':'总费用或耗时无效'}
            return {'ok':True,'option':args['option'],'cost':cost,'hours':hours,'steps':steps}
        if action == 'save_plan':
            import time
            if not isinstance(args.get('plan'), dict):
                return {'ok':False,'error':'参数必须是 {"plan":{...}}；不要把计划字段放在 args 顶层。计划字段必须逐项来自工具结果，不能添加 saved、duplicate 或未提供的 deadline。先检查所有方案是否同时满足预算和剩余时间，再保存。'}
            plan = args['plan']
            estimates = [x['observation'] for x in state['trace'] if x['action']=='estimate_cost' and x['observation'].get('ok')]
            estimate = next ( (x for x in estimates if x['option']==plan.get('option')), None)
            script = last(state['trace'], 'generate_script')
            remaining = (absolute_deadline_hours(req['deadline']) if req.get('deadline') is not None
                         else req.get('deadline_hours', -1) - max(0, time.monotonic()-state.get('started_monotonic',time.monotonic())) / 3600)
            deadline_field = plan_deadline_field(req)
            allowed_fields = {'theme','material_id','option','budget','cost','hours','steps','task_step_status','script'} | set(deadline_field)
            if not isinstance(plan,dict) or set(plan) != allowed_fields:
                return {'ok':False,'error':'方案字段不符合预期，不能包含未经验证的状态'}
            if (not isinstance(req.get('theme'),str) or not req['theme'].strip()
                or not finite_number(req.get('budget')) or not finite_number(remaining) or req['budget'] < 0 or remaining <= 0):
                return {'ok':False,'error':'主题、预算或截止时间无效'}
            if (not estimate or not script or not script.get('ok') or plan.get('cost') != estimate['cost']
                or plan.get('hours') != estimate['hours'] or plan.get('steps') != estimate['steps']
                or plan.get('task_step_status') != [{'name':x['name'],'status':'planned'} for x in estimate['steps']]
                or plan.get('script') != script['script'] or plan.get('material_id') != script['material_id'] or plan.get('theme') != req.get('theme')
                or plan.get('budget') != req.get('budget') or any(plan.get(k) != v for k,v in deadline_field.items())
                or estimate['cost'] > req.get('budget', -1) or estimate['hours'] > remaining):
                if estimate and estimate['cost'] > req.get('budget', -1):
                    return {'ok':False,'error':f"方案超预算：估算费用 {estimate['cost']}，预算 {req.get('budget')}。请重新估算费用表中的其他选项；全部超限时结束并说明缺口。"}
                if estimate and estimate['hours'] > remaining:
                    return {'ok':False,'error':f"方案超出剩余时间：估算 {estimate['hours']} 小时，可用 {remaining:.2f} 小时。请重新估算其他选项。"}
                if estimate and plan.get('task_step_status') != [{'name':x['name'],'status':'planned'} for x in estimate['steps']]:
                    return {'ok':False,'error':'task_step_status 必须是与估算 steps 同顺序的数组，例如 [{"name":"步骤名","status":"planned"}]；不能使用对象。'}
                return {'ok':False,'error':'计划内容必须逐字段匹配已验证的素材、脚本和费用估算结果。'}
            request_key = {'theme':req.get('theme'), 'budget':req.get('budget'), **deadline_field}
            if 'plan_data' in req:
                request_key['plan_data'] = req['plan_data']
            return save('plan', key_for(request_key), plan, state['runtime'], req.get('save_fail',False))
    if task == 'qa':
        docs = req.get('documents', load('docs'))
        if action == 'search_docs':
            return {'ok':True,'matches':search_documents(args['query'], docs)}
        if action == 'read_doc':
            matches = [d['id'] for x in state['trace'] if x['action']=='search_docs' and x['observation'].get('ok')
                       for d in x['observation']['matches']]
            if args['id'] not in matches:
                return {'ok':False,'error':'必须先检索到文档才能读取'}
            doc = next((d for d in docs if d['id']==args['id']), None)
            return {'ok':bool(doc),'document':doc, **({} if doc else {'error':'文档不存在'})}
    if task == 'video':
        if action == 'search_product':
            products = req.get('products', load('products'))
            q = args['query']
            matches = [p for p in products if any(w in q for w in p['keywords'])]
            return {'ok':True,'materials':matches}
        if action == 'estimate_duration':
            import math
            scenes = args['scenes']
            if not isinstance(scenes, list) or not scenes:
                return {'ok':False,'error':'缺少分镜'}
            durations = []
            for scene in scenes:
                declared = float(scene['seconds'])
                if not math.isfinite(declared) or declared <= 0:
                    return {'ok':False,'error':'分镜时长必须为正数'}
                # Conservative synthetic rate: at most 4 non-space characters per second.
                speech = math.ceil(len(''.join(scene.get('narration','').split())) / 4)
                durations.append(max(declared, speech))
            return {'ok':True,'seconds':sum(durations),'scene_seconds':durations,
                    'method':'每秒最多4个非空白字符；分镜时长取设定值与旁白估算值的较大者'}
        if action == 'check_brand':
            products = req.get('products', load('products')); ids = args['source_ids']
            rules = load('brand_rules')
            extra_rules = req.get('brand_rules', {})
            source = [p for p in products if p['id'] in ids]
            retrieved = {p['id'] for entry in state['trace'] if entry['action']=='search_product' and entry['observation'].get('ok')
                         for p in entry['observation']['materials']}
            texts = ' '.join(s.get('narration','') for s in args['scenes'])
            risks = []
            if not source: risks.append('缺少有效素材')
            if any(source_id not in retrieved for source_id in ids): risks.append('素材未经过检索')
            if set(ids) != {p['id'] for p in source}: risks.append('存在无效素材 ID')
            banned = set(rules['banned_phrases']) | set(extra_rules.get('banned_phrases', []))
            if any(w in texts for w in banned if w): risks.append('包含未经证实或禁止的宣传')
            allowed_visuals = set(rules['allowed_visuals'])
            if 'allowed_visuals' in extra_rules:
                allowed_visuals &= set(extra_rules['allowed_visuals'])
            facts = {p['id']:p['fact'] for p in source}
            fact_seen = False
            for scene in args['scenes']:
                fact = facts.get(scene.get('source_id'))
                if set(scene) - {'seconds','visual','narration','source_id'}:
                    risks.append('分镜含未经审核的额外文字字段')
                if scene.get('visual') not in allowed_visuals:
                    risks.append('画面描述未通过模板审核')
                if fact and scene.get('narration') == fact:
                    fact_seen = True
                elif scene.get('source_id') is None and scene.get('narration') in (*NEUTRAL_NARRATIONS, *(x for pair in AUDIENCE_INTROS.values() for x in pair)):
                    pass
                else:
                    risks.append('旁白未逐句匹配对应素材原文，需人工核验')
            if not fact_seen: risks.append('脚本缺少有来源的产品事实')
            corrections = []
            if not fact_seen and source:
                corrections.append('至少一个分镜必须逐字复制某条素材 fact 到 narration，并将该素材 ID 填入 source_id：'
                                   + '; '.join(f"{p['id']}: {p['fact']}" for p in source))
            if any('旁白未逐句匹配' in risk for risk in risks):
                corrections.append('每条事实旁白必须逐字等于其 source_id 对应的 fact；中性旁白只能使用允许的固定句式且 source_id=null。')
            return {'ok':True,'passed':not risks,'risks':risks,**({'corrections':corrections} if corrections else {})}
    return {'ok':False,'error':'工具未实现'}


def act(action, args=None, reason=''):
    return {'action':action,'args':args or {},'reason':reason}
def finish(result, reason='已根据工具结果结束'):
    return {'action':'finish','result':result,'reason':reason}
def last(trace, action):
    return next((x['observation'] for x in reversed(trace) if x['action']==action), None)
def fail_result(t):
    return {'status':'incomplete','reason':f"{t['action']} 失败: {t['observation'].get('error','未知错误')}", 'completed_steps':[x for x in []]}

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

def mock_decide(state):
    req, trace, task = state['request'], state['trace'], state['task']
    if trace and not trace[-1]['observation'].get('ok'):
        if task == 'schedule' and trace[-1]['action']=='save_draft' and trace[-1]['observation'].get('code')=='slot_conflict':
            draft = trace[-1]['args']['draft']
            return act('query_slots', {'date':draft['date'],'material_id':draft['material_ids'][0]}, '保存时发现并发占用，重新查询时段')
        if task == 'plan' and trace[-1]['action'] == 'estimate_cost':
            found = last(trace,'search_material') or {}
            attempted = {x['args'].get('option') for x in trace if x['action']=='estimate_cost'}
            alternative = next((o for o in found.get('available_options',[]) if o not in attempted),None)
            if alternative is not None:
                return act('estimate_cost', {'option':alternative}, '方案估价失败，尝试替代方案并保留已完成步骤')
        result = fail_result(trace[-1]); result['completed_steps'] = [x['action'] for x in trace if x['observation'].get('ok')]
        result['step_results'] = [x for x in trace if x['observation'].get('ok')]
        if task == 'schedule':
            result.update({'items':partial_schedule(req, trace), 'saved':False, 'published':False})
        if trace[-1]['action'] in ('save_draft','save_plan'): result['saved'] = False
        return finish(result, '工具失败，保留已成功步骤并停止')
    if task == 'schedule': return decide_schedule(req, trace)
    if task == 'plan': return decide_plan(req, trace)
    if task == 'qa': return decide_qa(req, trace)
    if task == 'video': return decide_video(req, trace)
    return finish({'status':'incomplete','reason':'未知任务'})

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

def decide_plan(req, trace):
    if not req.get('theme') or req.get('budget') is None or req.get('deadline_hours') is None:
        return finish({'status':'incomplete','reason':'缺少主题、预算或截止时间（deadline_hours）'})
    if (not finite_number(req['budget']) or not finite_number(req['deadline_hours'])
        or req['budget'] < 0 or req['deadline_hours'] <= 0):
        return finish({'status':'incomplete','reason':'预算或可用时间无效'})
    found = last(trace,'search_material')
    if found is None: return act('search_material',{'query':req['theme']},'检索活动素材')
    if not found['materials']: return finish({'status':'incomplete','reason':'没有素材'})
    options = found['available_options']
    if not options: return finish({'status':'incomplete','reason':'费用表没有可选方案'})
    script = last(trace,'generate_script')
    if script is None: return act('generate_script',{'material_id':found['materials'][0]['id'],'option':options[0]},'生成脚本并保留结果')
    attempts = [x for x in trace if x['action']=='estimate_cost']
    estimates = [x['observation'] for x in attempts if x['observation'].get('ok')]
    for option in options:
        latest = next((x for x in reversed(attempts) if x['args'].get('option')==option),None)
        if latest and not latest['observation'].get('ok'):
            continue
        estimate = latest['observation'] if latest else None
        if estimate is None: return act('estimate_cost',{'option':option},'估算初始方案' if not estimates else '前一方案超出限制，估算替代方案')
        if estimate['cost'] <= req['budget'] and estimate['hours'] <= req['deadline_hours']:
            plan = {'theme':req['theme'],'material_id':script['material_id'],'option':option,'budget':req['budget'], **plan_deadline_field(req),
                    'cost':estimate['cost'],'hours':estimate['hours'],'steps':estimate['steps'],
                    'task_step_status':[{'name':x['name'],'status':'planned'} for x in estimate['steps']],
                    'script':script['script']}
            saved = last(trace,'save_plan')
            if saved is None: return act('save_plan',{'plan':plan},'满足预算和期限，保存方案')
            return finish({'status':'completed','plan':plan,'saved':saved['saved'],'duplicate':saved['duplicate'],
                           'step_status':{'search_material':'completed','generate_script':'completed','estimate_cost':'completed','save_plan':'completed'},
                           'estimate_attempts':[{'option':x['args']['option'], 'status':'completed' if x['observation'].get('ok') else 'failed', 'result':x['observation']} for x in attempts]})
    return finish({'status':'incomplete','reason':'没有同时满足预算与时间的方案',
                   'budget_gap':min(positive_gap(x['cost'],req['budget']) for x in estimates),
                   'deadline_gap_hours':min(positive_gap(x['hours'],req['deadline_hours']) for x in estimates),
                   'option_gaps':[{'option':x['option'],'budget_gap':positive_gap(x['cost'],req['budget']),
                                   'deadline_gap_hours':positive_gap(x['hours'],req['deadline_hours'])} for x in estimates],
                   'estimates':estimates,'script':script['script'],'completed_steps':['search_material','generate_script','estimate_cost']})

def decide_qa(req, trace):
    question = req.get('question','').strip()
    if not question: return finish({'status':'incomplete','reason':'缺少问题'})
    query = qa_query(req)
    found = last(trace,'search_docs')
    if found is None: return act('search_docs',{'query':query},'结合问题及历史检索')
    if not found['matches']:
        return finish({'status':'insufficient','answer':'资料中未找到相关内容，请补充问题或资料。','citations':[],'gaps':['未检索到相关文档']})
    for item in found['matches']:
        if not any(x['action']=='read_doc' and x['args'].get('id')==item['id'] for x in trace):
            return act('read_doc',{'id':item['id']},'读取原文，核对证据与潜在冲突')
    docs = [x['observation']['document'] for x in trace if x['action']=='read_doc']
    citations = [{'id':d['id'],'date':d['date'],'excerpt':d['text']} for d in docs]
    if not all(question_has_evidence(query, d) for d in docs):
        return finish({'status':'insufficient','answer':'检索到的资料未覆盖问题所问的具体信息，请补充资料。',
                       'citations':citations,'gaps':['资料未覆盖具体问题']})
    texts = list(dict.fromkeys(d['text'] for d in docs))
    if len(texts)>1:
        if documents_conflict(docs):
            return finish({'status':'conflict','answer':'资料存在冲突，适用范围与优先级未明确，请确认适用制度。', 'citations':citations,'gaps':['适用范围或优先级']})
        return finish({'status':'insufficient','answer':'资料表述不一致，无法自动确定是否冲突，请补充适用范围。',
                       'citations':citations,'gaps':['资料间关系未明确']})
    return finish({'status':'completed','answer':texts[0], 'citations':citations,'gaps':[]})

def decide_video(req, trace):
    missing = [k for k in ('topic','audience','platform','duration','style') if not req.get(k)]
    if missing: return finish({'status':'incomplete','reason':'缺少关键信息','missing':missing})
    if not finite_number(req['duration']) or req['duration'] <= 0:
        return finish({'status':'incomplete','reason':'目标时长必须为正数'})
    searches = [x for x in trace if x['action']=='search_product']
    if not searches: return act('search_product',{'query':req['topic']},'根据主题检索素材')
    found = searches[-1]['observation']['materials']
    if not found:
        if len(searches)==1 and req.get('alternate_query'):
            return act('search_product',{'query':req['alternate_query']},'首次无结果，改写查询')
        return finish({'status':'insufficient','reason':'无可用素材，请补充产品事实','source_ids':[]})
    source = found[0]
    target = req['duration']
    estimates = [x for x in trace if x['action']=='estimate_duration']
    if not estimates:
        lively = '活泼' in req['style']
        audience_intro = next((pair[int(lively)] for key,pair in AUDIENCE_INTROS.items() if key in req['audience']),
                              NEUTRAL_NARRATIONS[2 if lively else 0])
        vertical = any(platform in req['platform'] for platform in ('短视频','抖音','快手','视频号'))
        fact_visual = '竖屏文字卡片' if vertical else '文字卡片'
        scenes = [
            {'seconds':5,'visual':'纯色背景','narration':audience_intro,'source_id':None},
            {'seconds':27,'visual':fact_visual,'narration':source['fact'],'source_id':source['id']},
            {'seconds':6,'visual':'纯色背景','narration':NEUTRAL_NARRATIONS[3 if lively else 1],'source_id':None},
        ]
        return act('estimate_duration',{'scenes':scenes},'估算首稿')
    scenes = estimates[-1]['args']['scenes']; seconds = estimates[-1]['observation']['seconds']
    if seconds > target:
        if len(estimates)>=2 and len(scenes)==1:
            return finish({'status':'incomplete','reason':'保留事实后仍无法满足时长','seconds':seconds})
        if len(estimates)>=2 or target < 8:
            fact_scene = next(scene for scene in estimates[0]['args']['scenes'] if scene['source_id']==source['id'])
            scenes = [dict(fact_scene,seconds=target)]
            reason = '删除非事实镜头，保留有来源的事实并重新估算'
        else:
            budget = max(target-2, target*0.8)
            initial = estimates[0]['args']['scenes']
            # Allocate the shorter edit according to the first draft's scene proportions.
            scenes = [dict(scene, seconds=max(0.1,round(budget*scene['seconds']/38))) for scene in initial]
            reason = '首稿超时，缩短各镜头并重新估算'
        return act('estimate_duration',{'scenes':scenes},reason)
    checked = last(trace,'check_brand')
    if checked is None: return act('check_brand',{'scenes':scenes,'source_ids':[source['id']]},'检查品牌规则和素材来源')
    if not checked['passed']: return finish({'status':'incomplete','reason':'品牌规则检查未通过','risks':checked['risks']})
    return finish({'status':'completed','title':req['topic'],'scenes':scenes,'seconds':seconds,
                   'estimated_scene_seconds':estimates[-1]['observation']['scene_seconds'],
                   'source_ids':[source['id']],'risks':checked['risks'],
                   'audience':req['audience'],'platform':req['platform'],'style':req['style']})


def validate_final(state, result):
    """Reject success claims which lack matching tool evidence, including stale checks."""
    if not isinstance(result, dict) or result.get('status') not in {
        'incomplete','insufficient','conflict','completed','ready','pending_confirmation'
    }:
        return {'status':'incomplete','reason':'结果格式或状态无效','code':'invalid_result'}
    task, trace = state['task'], state['trace']
    status = result['status']
    def rejected():
        rejected_result = {'status':'incomplete','reason':'最终结果与工具证据不一致，已拒绝未经验证的结论','code':'unverified_result'}
        if task == 'schedule':
            actual = partial_schedule(state['request'],trace)
            rejected_result.update({'items':actual,'saved':bool(actual) and all(x['saved'] for x in actual),'published':False})
        return rejected_result
    if result.get('published') is True:
        return rejected()
    if task == 'schedule':
        if 'published' in result and result['published'] is not False: return rejected()
        if status not in ('ready','pending_confirmation','incomplete'): return rejected()
        actual = partial_schedule(state['request'],trace)
        saved = bool(actual) and all(x['saved'] for x in actual)
        all_approved = bool(actual) and all(x['review_status']=='approved' for x in actual)
        pending = any(x['confirmation'] for x in actual if x['saved'])
        if status in ('ready','pending_confirmation'):
            if not saved or not all_approved or (status=='ready' and pending) or (status=='pending_confirmation' and not pending):
                return rejected()
        reason = safe_schedule_reason(result.get('reason'), '任务未完成，请查看逐项状态和工具记录' if status=='incomplete' else None)
        normalized = {'status':status,'items':actual,'saved':saved,'published':False}
        if reason: normalized['reason']=reason
        if len(actual)==1:
            normalized.update({k:actual[0].get(k) for k in ('suggested_time','confirmation','duplicate')})
        if 'completed_steps' in result:
            normalized['completed_steps']=[x['action'] for x in trace if x['observation'].get('ok')]
        if 'step_results' in result:
            normalized['step_results']=[x for x in trace if x['observation'].get('ok')]
        return normalized
    if task == 'plan':
        if status not in ('completed','incomplete'): return rejected()
        saves = [x for x in trace if x['action']=='save_plan' and x['observation'].get('ok')]
        if status=='completed' and (not saves or result.get('plan') != saves[-1]['args']['plan'] or result.get('saved') is not True):
            return rejected()
        if result.get('saved') is True and not saves: return rejected()
        actions = ('search_material','generate_script','estimate_cost','save_plan')
        step_status = {}
        for action in actions:
            entry = next((x for x in reversed(trace) if x['action']==action),None)
            step_status[action] = 'not_started' if entry is None else 'completed' if entry['observation'].get('ok') else 'failed'
        estimates = [x['observation'] for x in trace if x['action']=='estimate_cost' and x['observation'].get('ok')]
        attempts = [{'option':x['args'].get('option'),'status':'completed' if x['observation'].get('ok') else 'failed',
                     'result':x['observation']} for x in trace if x['action']=='estimate_cost']
        normalized = {'status':status,'saved':bool(saves),'step_status':step_status,
                      'completed_steps':[x['action'] for x in trace if x['observation'].get('ok')],
                      'step_results':[x for x in trace if x['observation'].get('ok')], 'estimate_attempts':attempts}
        if saves:
            normalized.update({'plan':saves[-1]['args']['plan'], 'duplicate':bool(saves[-1]['observation'].get('duplicate'))})
        if status=='incomplete':
            req = state['request']
            last_error = next((x for x in reversed(trace) if not x['observation'].get('ok')),None)
            search = next((x['observation'] for x in reversed(trace) if x['action']=='search_material' and x['observation'].get('ok')),None)
            if saves:
                reason = '方案已保存，但执行未完成；请查看工具记录'
            elif not req.get('theme') or req.get('budget') is None or req.get('deadline_hours') is None:
                reason = '缺少主题、预算或截止时间'
            elif last_error:
                reason = f"{last_error['action']} 失败: {last_error['observation'].get('error','未知错误')}"
            elif search and not search['materials']:
                reason = '没有素材'
            elif estimates:
                reason = '当前已评估方案无法完成，请查看费用和时限缺口'
            elif isinstance(result.get('reason'),str) and result['reason'].startswith('决策器失败:'):
                reason = result['reason']
            else:
                reason = '方案未完成，请查看工具记录'
            normalized['reason']=reason
            script = next((x['observation']['script'] for x in reversed(trace)
                           if x['action']=='generate_script' and x['observation'].get('ok')),None)
            if script is not None: normalized['script']=script
            if estimates:
                normalized['estimates']=estimates
                if finite_number(req.get('budget')) and finite_number(req.get('deadline_hours')):
                    normalized['option_gaps']=[{'option':x['option'],'budget_gap':positive_gap(x['cost'],req['budget']),
                                                'deadline_gap_hours':positive_gap(x['hours'],req['deadline_hours'])} for x in estimates]
                    normalized['budget_gap']=min(x['budget_gap'] for x in normalized['option_gaps'])
                    normalized['deadline_gap_hours']=min(x['deadline_gap_hours'] for x in normalized['option_gaps'])
        return normalized
    if task == 'qa' and status not in ('completed','conflict','incomplete','insufficient'):
        return rejected()
    if task == 'qa' and status in ('incomplete','insufficient'):
        if status == 'insufficient':
            docs = [x['observation']['document'] for x in trace if x['action']=='read_doc' and x['observation'].get('ok')]
            texts = {d['text'] for d in docs}
            question = state['request'].get('question', '')
            gap = ('资料内容不同，其适用范围或相互关系未确定' if len(texts)>1
                   else f'缺少能回答“{question}”的完整资料')
            return {'status':'insufficient','answer':'现有资料不足以回答该问题，请补充资料或明确适用范围。',
                    'citations':[{'id':d['id'],'date':d['date'],'excerpt':d['text']} for d in docs], 'gaps':[gap]}
        last_error = trace[-1] if trace and not trace[-1]['observation'].get('ok') else None
        if not state['request'].get('question'):
            reason = '缺少问题'
        elif last_error:
            reason = f"{last_error['action']} 失败: {last_error['observation'].get('error','未知错误')}"
        elif isinstance(result.get('reason'),str) and result['reason'].startswith('决策器失败:'):
            reason = result['reason']
        else:
            reason = '问答未完成，请查看工具记录'
        return {'status':'incomplete','reason':reason,
                'completed_steps':[x['action'] for x in trace if x['observation'].get('ok')],
                'step_results':[x for x in trace if x['observation'].get('ok')]}
    if task == 'qa' and status in ('completed','conflict'):
        docs = [x['observation']['document'] for x in trace if x['action']=='read_doc' and x['observation'].get('ok')]
        searches = [x for x in trace if x['action']=='search_docs' and x['observation'].get('ok')]
        for index, entry in enumerate(trace):
            if entry['action']=='search_docs' and not entry['observation'].get('ok'):
                if not any(x['action']=='search_docs' and x['args']==entry['args'] and x['observation'].get('ok')
                           for x in trace[index+1:]): return rejected()
        found_ids = {d['id'] for x in searches for d in x['observation']['matches']}
        read_ids = {d['id'] for d in docs}
        if not searches or found_ids != read_ids: return rejected()
        req = state['request']
        query = qa_query(req)
        if not any(x['args'].get('query') == query for x in searches): return rejected()
        if not all(question_has_evidence(query, d) for d in docs): return rejected()
        citations = result.get('citations')
        expected = [{'id':d['id'],'date':d['date'],'excerpt':d['text']} for d in docs]
        if not docs or not isinstance(citations,list) or citations != expected: return rejected()
        unique = set(d['text'] for d in docs)
        if status == 'completed' and (len(unique) != 1 or result.get('answer') not in unique): return rejected()
        if status == 'conflict' and (len(unique) < 2 or not documents_conflict(docs)): return rejected()
        if status == 'conflict':
            return {'status':'conflict','answer':'资料存在不同表述，适用范围与优先级未明确，请确认适用制度。',
                    'citations':expected,'gaps':['适用范围或优先级']}
        return {'status':'completed','answer':next(iter(unique)),'citations':expected,'gaps':[]}
    if task == 'video' and status not in ('completed','incomplete','insufficient'):
        return rejected()
    if task == 'video' and status=='completed':
        estimates = [x for x in trace if x['action']=='estimate_duration' and x['observation'].get('ok')]
        checks = [x for x in trace if x['action']=='check_brand' and x['observation'].get('passed')]
        scenes = result.get('scenes')
        if (not finite_number(state['request'].get('duration')) or state['request']['duration'] <= 0
            or not all(isinstance(state['request'].get(k),str) and state['request'][k].strip() for k in ('topic','audience','platform','style'))
            or not estimates or not checks or scenes != estimates[-1]['args']['scenes']
            or scenes != checks[-1]['args']['scenes'] or result.get('source_ids') != checks[-1]['args']['source_ids']
            or result.get('seconds') != estimates[-1]['observation']['seconds']
            or result.get('estimated_scene_seconds') != estimates[-1]['observation']['scene_seconds']
            or result.get('title') != state['request'].get('topic')
            or result.get('risks') != checks[-1]['observation']['risks']
            or any(result.get(k) != state['request'].get(k) for k in ('audience','platform','style'))
            or result['seconds'] > state['request'].get('duration',0)):
            return rejected()
        return {'status':'completed','title':state['request']['topic'],
                'audience':state['request']['audience'],'platform':state['request']['platform'],
                'style':state['request']['style'],'scenes':scenes,
                'seconds':estimates[-1]['observation']['seconds'],
                'estimated_scene_seconds':estimates[-1]['observation']['scene_seconds'],
                'source_ids':checks[-1]['args']['source_ids'],'risks':checks[-1]['observation']['risks']}
    if task == 'video':
        req = state['request']
        searches = [x for x in trace if x['action']=='search_product' and x['observation'].get('ok')]
        estimates = [x for x in trace if x['action']=='estimate_duration' and x['observation'].get('ok')]
        checks = [x for x in trace if x['action']=='check_brand' and x['observation'].get('ok')]
        missing = [k for k in ('topic','audience','platform','duration','style') if not req.get(k)]
        last_error = trace[-1] if trace and not trace[-1]['observation'].get('ok') else None
        if missing:
            reason = '缺少关键信息'
        elif not finite_number(req.get('duration')) or req['duration']<=0:
            reason = '目标时长必须为正数'
        elif last_error:
            reason = f"{last_error['action']} 失败: {last_error['observation'].get('error','未知错误')}"
        elif checks and not checks[-1]['observation'].get('passed'):
            reason = '品牌规则检查未通过'
        elif estimates and estimates[-1]['observation']['seconds'] > req['duration']:
            reason = '脚本超过目标时长，无法验证合规版本'
        elif searches and not searches[-1]['observation']['materials']:
            reason = '无可用素材，请补充产品事实'
        elif isinstance(result.get('reason'),str) and result['reason'].startswith('决策器失败:'):
            reason = result['reason']
        else:
            reason = '脚本未完成，请查看工具记录'
        normalized = {'status':status,'reason':reason}
        if missing: normalized['missing']=missing
        if checks: normalized['risks']=checks[-1]['observation']['risks']
        if estimates: normalized['seconds']=estimates[-1]['observation']['seconds']
        retrieved = {p['id'] for x in searches for p in x['observation']['materials']}
        if estimates:
            normalized['source_ids']=list(dict.fromkeys(s.get('source_id') for s in estimates[-1]['args']['scenes']
                                                       if s.get('source_id') in retrieved))
        else:
            normalized['source_ids']=[]
        return normalized
    return result
