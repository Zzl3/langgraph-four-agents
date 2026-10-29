"""LangGraph-backed tool-observation-decision loop."""
import json
import math
import os
import time
from typing import Any, TypedDict
from langgraph.graph import StateGraph, START, END

TASKS = ('schedule','plan','qa','video')

class State(TypedDict, total=False):
    request: dict
    task: str
    trace: list
    result: dict
    decision: dict
    calls: int
    max_calls: int
    model: str
    runtime: str
    started_monotonic: float
    run_id: str
    checkpoint_signature: str
    checkpoint_version: int
    resume_pending: bool
    checkpoint_error: str
    trace_file: str
    trace_error: str
    decision_source: str

def append_trace(path, event):
    with open(path,'a',encoding='utf-8') as stream:
        stream.write(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n')

def compact_observations(trace):
    """Compress adjacent identical observations in model context, retaining full public trace."""
    compacted=[]
    for entry in trace:
        signature = (entry.get('action'),entry.get('args'),entry.get('observation'))
        if compacted and signature == (compacted[-1].get('action'),compacted[-1].get('args'),compacted[-1].get('observation')):
            count = compacted[-1].get('consecutive_repetitions',1)+1
            compacted[-1] = {**entry,'consecutive_repetitions':count}
        else:
            compacted.append(dict(entry))
    return compacted


def model_observations(trace):
    """Deduplicate repeated identical results across the trace sent to a small-context model."""
    compacted = compact_observations(trace)
    unique = []
    positions = {}
    for entry in compacted:
        signature = json.dumps({key:entry.get(key) for key in ('action','args','observation')},
                               ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if signature not in positions:
            positions[signature] = len(unique)
            unique.append(entry)
            continue
        index = positions[signature]
        previous = unique[index]
        unique[index] = {**entry,'repetitions':previous.get('repetitions',1)+entry.get('consecutive_repetitions',1)}
    return unique


def repeated_successful_call(action, args, trace):
    """Prevent the model from burning the call budget repeating immutable reads/checks."""
    immutable = {'search_material','search_docs','read_doc','search_product','check_rules',
                 'estimate_duration','check_brand'}
    return action in immutable and any(
        entry.get('action') == action and entry.get('args') == args
        and entry.get('observation',{}).get('ok')
        for entry in trace
    )


def video_brand_revision(trace):
    """Build revision guidance from the latest failed check and later duration estimates."""
    checks = [(index,entry) for index,entry in enumerate(trace)
              if entry.get('action')=='check_brand' and entry.get('observation',{}).get('ok')]
    if not checks or checks[-1][1]['observation'].get('passed'):
        return None
    failed_index, failed = checks[-1]
    later_estimates = [entry for entry in trace[failed_index+1:]
                       if entry.get('action')=='estimate_duration'
                       and entry.get('observation',{}).get('ok')]
    searches = [entry['observation'] for entry in trace
                if entry.get('action')=='search_product' and entry.get('observation',{}).get('ok')]
    products = {}
    for search in searches:
        for item in search.get('materials',[]):
            products[(item.get('id'),item.get('fact'))] = item
    source_ids = set(failed.get('args',{}).get('source_ids',[]))
    facts = [{'source_id':item['id'],'fact':item['fact']} for item in products.values()
             if item.get('id') in source_ids and isinstance(item.get('fact'),str)]
    estimate_pending = bool(later_estimates)
    return {
        'required':True,
        'failed_scene_set':failed.get('args',{}).get('scenes'),
        'risks':failed['observation'].get('risks',[]),
        'corrections':failed['observation'].get('corrections',[]),
        'exact_product_facts':facts,
        'next_step':'check_brand' if estimate_pending else 'revise_and_estimate',
        'scenes_to_check':later_estimates[-1]['args'].get('scenes') if estimate_pending else None,
        'rule':('调用 check_brand 检查 scenes_to_check 中刚刚估时的同一版本。'
                if estimate_pending else
                '必须实际改写分镜；产品事实旁白逐字使用 fact 并填对应 source_id，中性旁白 source_id=null。'
                '先重新估时修订后的分镜，再检查同一版本。不得重复提交失败的分镜。'),
    }


def qa_evidence_context(request, trace, remaining_calls=None):
    """Expose citation-ready read evidence and a conservative lexical status hint."""
    from .tasks import documents_conflict, qa_search_state, question_has_evidence
    search_state=qa_search_state(request,trace)
    query=search_state['query']
    failures=search_state['unresolved_search_failures']+search_state['unresolved_read_failures']
    if failures and (any(item['attempts']>=2 for item in failures)
                     or (remaining_calls is not None and remaining_calls<=1)):
        return {'stage':'tool_failure_terminal','can_finish_incomplete':True,
                'failures':failures,
                'rule':'检索或读取持续失败，或剩余调用不足以恢复并完成；请以 incomplete 结束，说明失败及已完成步骤，不得声称已有答案。'}
    if search_state['unresolved_search_failures']:
        actions=[{'action':'search_docs','args':item['args'],'reason':'重试失败的同一检索，成功观察后才能结束'}
                 for item in search_state['unresolved_search_failures']]
        return {'stage':'retry_failed_search','next_required_actions':actions,
                'required_query':query}
    if search_state['unresolved_read_failures']:
        actions=[{'action':'read_doc','args':item['args'],'reason':'重试失败的原文读取'}
                 for item in search_state['unresolved_read_failures']]
        return {'stage':'retry_failed_read','next_required_actions':actions,
                'required_query':query}
    if not search_state['baseline_succeeded']:
        return {'stage':'baseline_search_required','required_query':query,
                'next_required_actions':[{'action':'search_docs','args':{'query':query},
                                          'reason':'先完成与当前问题一致的基准检索'}]}
    if search_state['required_reads']:
        actions=[{'action':'read_doc','args':{'id':doc_id},'reason':'读取检索命中的原文后才能判断答案'}
                 for doc_id in search_state['required_reads']]
        return {'stage':'read_required_documents','required_document_ids':search_state['required_reads'],
                'next_required_actions':actions}
    ordered_ids = search_state['matched_ids']
    documents = []
    read_ids = set()
    for entry in trace:
        if entry.get('action')=='read_doc' and entry.get('observation',{}).get('ok'):
            document = entry['observation'].get('document')
            if isinstance(document,dict):
                documents.append(document)
                read_ids.add(document.get('id'))
    citations = [{'id':doc['id'],'date':doc.get('date'),'excerpt':doc.get('text','')}
                 for doc in documents]
    if not ordered_ids:
        status, reason = 'insufficient', 'no_matching_documents'
    elif read_ids != set(ordered_ids):
        status, reason = 'incomplete', 'read_documents_do_not_match_search_results'
    else:
        relevant = all(question_has_evidence(query,doc) for doc in documents)
        texts = list(dict.fromkeys(doc.get('text') for doc in documents))
        if not relevant:
            status, reason = 'insufficient', 'question_not_supported'
        elif len(texts)==1:
            status, reason = 'completed', 'single_supported_answer'
        elif documents_conflict(documents):
            status, reason = 'conflict', 'verified_numeric_or_polarity_conflict'
        else:
            status, reason = 'insufficient', 'relationship_between_sources_unclear'
    return {'stage':'decide_from_read_evidence','suggested_status':status,'assessment':reason,
            'citations':citations,
            'rule':'仅将此状态作为基于现有词汇规则的提示。由你决定是否结束；结束时引用应从 citations 原样复制，不能补造、改写或遗漏支撑结论的文档。'}


def _decide(state: State) -> dict:
    from .tasks import documents_conflict, finite_number, mock_decide, qa_search_state, remaining_plan_hours, tool_specs
    from .prompts import TASK_GUIDANCE
    if state['model'] == 'mock':
        if state.get('resume_pending') and state['trace'] and not state['trace'][-1]['observation'].get('ok'):
            previous = state['trace'][-1]
            decision = {'action':previous['action'],'args':previous['args'],'reason':'恢复后重试此前失败的步骤'}
        else:
            decision = mock_decide(state)
    else:
        from openai import OpenAI
        timeout = float(os.environ.get('OPENAI_TIMEOUT_SECONDS') or '30')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('OPENAI_TIMEOUT_SECONDS 必须是正的有限数值')
        system = ("你是任务执行代理。只输出 JSON 对象：要调用工具时为 "
                  '{"action":"工具名","args":{},"reason":"简要理由"}；结束时为 '
                  '{"action":"finish","result":{...},"reason":"简要理由"}。'
                  "根据工具返回调整动作，不得虚构事实、成功或已发布状态。只可调用列出的工具。"
                  "observations 已包含所有工具执行结果；每轮只选择一个新动作。"
                  "工具和参数未变化且刚刚成功时不要重复调用；保存成功且目标已满足时选择 finish。"
                  "已成功的工具和完全相同参数不可再次调用，除非后续观察改变了其依赖状态且确实需要刷新该状态；同一检索查询不要重复执行。"
                  "若上一步工具失败，阅读 error 并修正失败步骤的参数后重试；若错误要求重新查询则先查询。"
                  "缺信息、失败、冲突、预算及确认边界必须明确说明。所有输出使用中文。"
                  + TASK_GUIDANCE[state['task']])
        if state['task'] == 'plan':
            system += ' plan_state 给出各方案真实估价、脚本状态与预算/剩余时间判定；保存前逐项核对。'
        if state['task'] == 'video':
            system += ' 若有 brand_revision，严格按 next_step 阶段执行：修订阶段改写并重新估时；检查阶段对 scenes_to_check 原样进行品牌检查。'
        if state['task'] == 'qa':
            system += ' qa_state.stage 为 baseline_search_required、retry_failed_search、retry_failed_read 或 read_required_documents 时先执行 qa_state.next_required_actions；进入 decide_from_read_evidence 后才可给答案、冲突或资料不足；若 qa_state.can_finish_incomplete=true，允许说明工具失败并以 incomplete 提前结束。引用从 qa_state.citations 原样复制。'
        payload = {'task': state['task'], 'request': state['request'], 'tools': tool_specs(state['task']),
                   'observations': model_observations(state['trace']), 'remaining_calls': state['max_calls'] - state['calls']}
        if state['trace']:
            payload['last_observation'] = state['trace'][-1]
        if state['task'] == 'qa':
            qa_search=qa_search_state(state['request'],state['trace'])
            payload['required_query'] = qa_search['query']
            payload['required_document_reads'] = qa_search['required_reads']
            payload['qa_state'] = qa_evidence_context(state['request'],state['trace'],
                                                       state['max_calls']-state['calls'])
            payload['next_required_actions'] = payload['qa_state'].get('next_required_actions',[])
            read_docs = [entry['observation']['document'] for entry in state['trace']
                         if entry['action']=='read_doc' and entry['observation'].get('ok')]
            payload['evidence_status'] = {
                'all_matches_read': not payload['required_document_reads'],
                'possible_conflict': documents_conflict(read_docs) if read_docs else False,
                'read_document_ids': [doc['id'] for doc in read_docs],
            }
        if state['task'] == 'plan':
            payload['remaining_deadline_hours'] = remaining_plan_hours(state['request'],state)
            material_search = next((x['observation'] for x in reversed(state['trace'])
                                    if x['action']=='search_material' and x['observation'].get('ok')), {})
            estimates = {x['args'].get('option'):x['observation'] for x in state['trace']
                         if x['action']=='estimate_cost'}
            generated_scripts = [x['observation'] for x in state['trace']
                                 if x['action']=='generate_script' and x['observation'].get('ok')]
            payload['plan_state'] = {
                'budget': state['request'].get('budget'),
                'remaining_deadline_hours': payload['remaining_deadline_hours'],
                'script_generated':bool(generated_scripts),
                'script_material_id':generated_scripts[-1].get('material_id') if generated_scripts else None,
                'options': [{'name':name,
                             'estimate':estimates.get(name),
                             'within_limits':bool(estimates.get(name,{}).get('ok')
                                 and finite_number(state['request'].get('budget'))
                                 and finite_number(payload['remaining_deadline_hours'])
                                 and estimates[name].get('cost',float('inf'))<=state['request']['budget']
                                 and estimates[name].get('hours',float('inf'))<=payload['remaining_deadline_hours'])}
                            for name in material_search.get('available_options',[])],
                'rule':'脚本由主题和匹配素材生成，与方案选项无关；先取得匹配素材的脚本，再评估候选方案。只有存在该素材脚本、该选项估价且同时满足预算和剩余时限，才可保存。全部选项评估后仍无可行项则结束并报告缺口。',
            }
        if state['task'] == 'video':
            revision = video_brand_revision(state['trace'])
            if revision:
                payload['brand_revision'] = revision
        if state.get('resume_pending'):
            payload['resume_context'] = '已恢复此前轨迹，请基于失败或成功结果选择重试、调整或结束。'
        client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ.get('OPENAI_BASE_URL') or None,
                        timeout=timeout, max_retries=0)
        try:
            response = client.chat.completions.create(model=os.environ['OPENAI_MODEL'],
                        messages=[{'role':'system','content':system},{'role':'user','content':json.dumps(payload, ensure_ascii=False)}],
                        response_format={'type':'json_object'}, temperature=0)
        finally:
            client.close()
        decision = json.loads(response.choices[0].message.content)
    if not isinstance(decision, dict) or not isinstance(decision.get('action'), str):
        raise ValueError('模型返回的决策格式无效')
    return {'decision': decision,'decision_source':state['model']}


def decide(state: State) -> dict:
    try:
        return _decide(state)
    except Exception as e:
        return {'decision': {'action': 'finish', 'reason': '决策器失败',
                             'result': {'status': 'incomplete',
                                        'reason': f'决策器失败: {type(e).__name__}: {e}',
                                        'completed_steps': [x['action'] for x in state['trace'] if x['observation'].get('ok')]}},
                'decision_source':'controller_error'}


def execute(state: State) -> dict:
    from .tasks import execute_tool
    d = state['decision']
    if d['action'] == 'finish':
        from .tasks import validate_final
        proposed = d.get('result', {'status':'incomplete','reason':'模型未返回结果'})
        try:
            return {'result': validate_final(state, proposed)}
        except (TypeError, KeyError, ValueError, AttributeError, IndexError):
            return {'result': {'status':'incomplete','reason':'最终结果格式无效，无法核验','code':'invalid_result'}}
    try:
        args = d.get('args', {})
        if repeated_successful_call(d['action'],args,state['trace']):
            obs = {'ok':False,'code':'repeated_successful_call',
                   'error':'相同参数的读取或检查已成功执行，结果已在 observations 中；不要重复调用。请根据已有结果执行下一项尚未完成的动作，或在证据齐全时结束。'}
        else:
            obs = execute_tool(state['task'], d['action'], args, state)
    except Exception as e:
        obs = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    reason = d.get('reason','')
    if state['task']=='schedule':
        from .tasks import safe_schedule_reason
        reason = safe_schedule_reason(reason, '请根据审核、时段和草稿结果判断')
    entry = {'step': state['calls'] + 1, 'action': d['action'], 'args': d.get('args', {}),
             'reason': reason, 'observation': obs,
             'decision_source':state.get('decision_source',state['model'])}
    trace = state['trace'] + [entry]
    updates = {'trace':trace,'calls':state['calls']+1,'resume_pending':False}
    if state.get('trace_file') and not state.get('trace_error'):
        try:
            append_trace(state['trace_file'],{'event':'tool','entry':entry})
        except (OSError,ValueError,TypeError) as e:
            updates['trace_error'] = f'轨迹写入失败: {type(e).__name__}: {e}'
    if state.get('run_id'):
        try:
            from .checkpoints import advance
            updates['checkpoint_version'] = advance(state['runtime'],state['run_id'],state['checkpoint_signature'],
                                                    state['checkpoint_version'],trace)
        except Exception as e:
            updates['checkpoint_error'] = f'进度保存失败: {type(e).__name__}: {e}'
    return updates


def route(state: State) -> str:
    if state.get('checkpoint_error'):
        return 'checkpoint_failure'
    if state['decision']['action'] == 'finish':
        return 'end'
    if state['calls'] >= state['max_calls']:
        return 'limit'
    return 'decide'


def limit(state: State) -> dict:
    result = {'status':'incomplete','reason':'达到工具调用上限', 'calls':state['calls'],
              'completed_steps':[x['action'] for x in state['trace'] if x['observation'].get('ok')],
              'step_results':[x for x in state['trace'] if x['observation'].get('ok')]}
    if state['task'] == 'schedule':
        from .tasks import partial_schedule
        items = partial_schedule(state['request'],state['trace'])
        result.update({'items':items,'saved':bool(items) and all(x['saved'] for x in items),'published':False})
    return {'result':result}

def checkpoint_failure(state: State) -> dict:
    result = {'status':'incomplete','reason':state['checkpoint_error'],
              'completed_steps':[x['action'] for x in state['trace'] if x['observation'].get('ok')],
              'step_results':[x for x in state['trace'] if x['observation'].get('ok')]}
    saved = next((x for x in reversed(state['trace']) if x['action']=='save_plan' and x['observation'].get('ok')),None)
    if saved:
        result.update({'saved':True,'plan':saved['args']['plan']})
    return {'result':result}


def build_graph(task: str):
    """Compile one task-bound LangGraph agent from the shared execution skeleton."""
    if task not in TASKS:
        raise ValueError(f'未知任务图: {task}')

    def bind(node):
        def invoke(state):
            if state.get('task') != task:
                raise ValueError(f'{task} 图不能执行 task={state.get("task")}')
            return node(state)
        return invoke

    prefix = task
    g = StateGraph(State)
    names = {name:f'{prefix}_{name}' for name in ('decide','execute','limit','checkpoint_failure')}
    g.add_node(names['decide'], bind(decide))
    g.add_node(names['execute'], bind(execute))
    g.add_node(names['limit'], bind(limit))
    g.add_node(names['checkpoint_failure'], bind(checkpoint_failure))
    g.add_edge(START, names['decide'])
    g.add_edge(names['decide'], names['execute'])

    def task_route(state):
        if state.get('task') != task:
            raise ValueError(f'{task} 图不能路由 task={state.get("task")}')
        destination = route(state)
        return END if destination == 'end' else names[destination]

    g.add_conditional_edges(names['execute'], task_route,
                            {END:END, names['decide']:names['decide'], names['limit']:names['limit'],
                             names['checkpoint_failure']:names['checkpoint_failure']})
    g.add_edge(names['limit'], END)
    g.add_edge(names['checkpoint_failure'], END)
    return g.compile()


def run(task: str, request: dict, model='mock', max_calls=20, runtime='runtime', run_id=None, resume=False, trace_file=None) -> dict:
    if task not in TASKS:
        raise ValueError(f'task 必须是 {", ".join(TASKS)} 之一')
    if max_calls < 1:
        raise ValueError('max_calls 必须大于零')
    request = dict(request)
    if task == 'plan' and request.get('deadline') is not None:
        from .tasks import absolute_deadline_hours
        try:
            request['deadline_hours'] = absolute_deadline_hours(request['deadline'])
        except (TypeError, ValueError) as e:
            return {'task':task,'model':model,'result':{'status':'incomplete','reason':f'截止时间无效: {e}'},
                    'trace':[],'final_decision':{'action':'finish','reason':'输入校验失败'}}
    initial = {'task':task,'request':request,'model':model,'max_calls':max_calls,
               'runtime':runtime,'calls':0,'trace':[], 'started_monotonic':time.monotonic()}
    try:
        if trace_file is not None:
            trace_file = os.fspath(trace_file)
            with open(trace_file,'x',encoding='utf-8') as stream:
                stream.write(json.dumps({'event':'start','task':task,'model':model,'run_id':run_id,'resumed':resume},ensure_ascii=False)+'\n')
            initial['trace_file'] = trace_file
        if resume and not run_id:
            raise ValueError('续跑需要指定运行 ID')
        if run_id is not None:
            if task != 'plan' or not isinstance(run_id,str) or not run_id.strip() or len(run_id)>128:
                raise ValueError('运行 ID 仅用于内容规划，长度须为 1 至 128')
            from .checkpoints import create, load, signature
            digest = signature(request)
            if resume:
                checkpoint = load(runtime,run_id,digest)
                elapsed = time.time()-checkpoint['started_wall']
                if elapsed < 0:
                    raise ValueError('系统时间早于原运行开始时间，无法安全计算截止时间')
                initial.update({'trace':checkpoint['trace'],'calls':checkpoint['version'],
                                'started_monotonic':time.monotonic()-elapsed,'resume_pending':True,
                                'checkpoint_version':checkpoint['version']})
            else:
                create(runtime,run_id,digest,time.time())
                initial['checkpoint_version'] = 0
            initial.update({'run_id':run_id,'checkpoint_signature':digest})
        if initial['calls'] >= max_calls:
            output = {'task':task,'model':model,'result':limit(initial)['result'],'trace':initial['trace'],
                      'final_decision':{'action':'limit','reason':'达到工具调用上限'},'run_id':run_id,'resumed':resume}
            if trace_file:
                try: append_trace(trace_file,{'event':'finish','result':output['result'],'final_decision':output['final_decision']})
                except (OSError,ValueError,TypeError) as e: output['trace_error']=f'轨迹写入失败: {type(e).__name__}: {e}'
            return output
        state = build_graph(task).invoke(initial,
                                    config={'recursion_limit': max_calls * 3 + 5})
        final = state.get('decision', {})
        rejected = state.get('result',{}).get('code') in ('invalid_result','unverified_result')
        outcome = state.get('result',{})
        if rejected:
            finish_reason = outcome['reason']
        elif outcome.get('status')=='incomplete':
            finish_reason = outcome.get('reason','任务未完成，请查看工具记录')
        else:
            finish_reason = {'ready':'审核和草稿保存完成，尚未发布',
                             'pending_confirmation':'替代时段等待用户确认，尚未发布',
                             'conflict':'资料存在冲突，需要确认适用范围',
                             'insufficient':'资料不足，需要补充',
                             'completed':'依据工具结果完成当前任务'}.get(outcome.get('status'),'请查看工具记录')
        if state.get('checkpoint_error'):
            ending={'action':'checkpoint_error','reason':state['checkpoint_error']}
        elif final.get('action')=='finish':
            source=state.get('decision_source','controller_error')
            ending={'action':'finish','reason':finish_reason,'decision_source':source}
            if source in ('real','mock'):
                model_reason=final.get('reason','')
                if task=='schedule':
                    from .tasks import safe_schedule_reason
                    model_reason=safe_schedule_reason(model_reason,finish_reason)
                if isinstance(model_reason,str) and model_reason:
                    ending['model_reason']=model_reason[:300]
                if isinstance(final.get('result'),dict):
                    ending['proposal']=final['result']
            elif isinstance(final.get('result'),dict):
                ending['controller_result']=final['result']
        else:
            ending={'action':'limit','reason':'达到工具调用上限'}
        result = {'task':task,'model':model,'result':state.get('result', {}),'trace':state['trace'],'final_decision':ending}
        if run_id is not None:
            result.update({'run_id':run_id,'resumed':resume})
        if state.get('trace_error'):
            result['trace_error']=state['trace_error']
        elif trace_file:
            try: append_trace(trace_file,{'event':'finish','result':result['result'],'final_decision':ending})
            except (OSError,ValueError,TypeError) as e: result['trace_error']=f'轨迹写入失败: {type(e).__name__}: {e}'
        return result
    except Exception as e:
        output = {'task':task,'model':model,'result':{'status':'incomplete','reason':f'决策或运行失败: {type(e).__name__}: {e}'},'trace':[]}
        if initial.get('trace_file'):
            try: append_trace(initial['trace_file'],{'event':'finish','result':output['result']})
            except (OSError,ValueError,TypeError) as trace_error: output['trace_error']=f'轨迹写入失败: {type(trace_error).__name__}: {trace_error}'
        return output
