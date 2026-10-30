"""LangGraph-backed tool-observation-decision loop."""
import json
import math
import os
from typing import Any, TypedDict
from langgraph.graph import StateGraph, START, END

TASKS = ('schedule',)

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
    immutable = {'read_material','check_rules'}
    return action in immutable and any(
        entry.get('action') == action and entry.get('args') == args
        and entry.get('observation',{}).get('ok')
        for entry in trace
    )






def _decide(state: State) -> dict:
    from .tasks import mock_decide, tool_specs
    from .prompts import TASK_GUIDANCE
    if state['model'] == 'mock':
        decision = mock_decide(state)
    else:
        from openai import OpenAI
        timeout = float(os.environ.get('OPENAI_TIMEOUT_SECONDS') or '30')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('OPENAI_TIMEOUT_SECONDS 必须是正的有限数值')
        system = ("你是素材排期代理。只输出 JSON 对象：要调用工具时为 "
                  '{"action":"工具名","args":{},"reason":"简要理由"}；结束时为 '
                  '{"action":"finish","result":{...},"reason":"简要理由"}。'
                  "根据工具返回调整动作，不得虚构事实、成功或已发布状态。只可调用列出的工具。"
                  "observations 已包含所有工具执行结果；每轮只选择一个新动作。"
                  "工具和参数未变化且刚刚成功时不要重复调用；保存成功且目标已满足时选择 finish。"
                  "已成功的工具和完全相同参数不可再次调用，除非后续观察改变了其依赖状态且确实需要刷新该状态；同一检索查询不要重复执行。"
                  "若上一步工具失败，阅读 error 并修正失败步骤的参数后重试；若错误要求重新查询则先查询。"
                  "缺信息、审核风险、时段冲突及确认边界必须明确说明。所有输出使用中文。"
                  + TASK_GUIDANCE['schedule'])
        payload = {'task': state['task'], 'request': state['request'], 'tools': tool_specs(state['task']),
                   'observations': model_observations(state['trace']), 'remaining_calls': state['max_calls'] - state['calls']}
        if state['trace']:
            payload['last_observation'] = state['trace'][-1]

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
    updates = {'trace':trace,'calls':state['calls']+1}
    if state.get('trace_file') and not state.get('trace_error'):
        try:
            append_trace(state['trace_file'],{'event':'tool','entry':entry})
        except (OSError,ValueError,TypeError) as e:
            updates['trace_error'] = f'轨迹写入失败: {type(e).__name__}: {e}'
    return updates


def route(state: State) -> str:
    if state['decision']['action'] == 'finish':
        return 'end'
    if state['calls'] >= state['max_calls']:
        return 'limit'
    return 'decide'


def limit(state: State) -> dict:
    result = {'status':'incomplete','reason':'达到工具调用上限', 'calls':state['calls'],
              'completed_steps':[x['action'] for x in state['trace'] if x['observation'].get('ok')],
              'step_results':[x for x in state['trace'] if x['observation'].get('ok')]}
    from .tasks import partial_schedule
    items = partial_schedule(state['request'],state['trace'])
    result.update({'items':items,'saved':bool(items) and all(x['saved'] for x in items),'published':False})
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
    names = {name:f'{prefix}_{name}' for name in ('decide','execute','limit')}
    g.add_node(names['decide'], bind(decide))
    g.add_node(names['execute'], bind(execute))
    g.add_node(names['limit'], bind(limit))
    g.add_edge(START, names['decide'])
    g.add_edge(names['decide'], names['execute'])

    def task_route(state):
        if state.get('task') != task:
            raise ValueError(f'{task} 图不能路由 task={state.get("task")}')
        destination = route(state)
        return END if destination == 'end' else names[destination]

    g.add_conditional_edges(names['execute'], task_route,
                            {END:END, names['decide']:names['decide'], names['limit']:names['limit']})
    g.add_edge(names['limit'], END)
    return g.compile()


def run(task: str, request: dict, model='mock', max_calls=20, runtime='runtime', trace_file=None) -> dict:
    if task not in TASKS:
        raise ValueError(f'task 必须是 {", ".join(TASKS)} 之一')
    if max_calls < 1:
        raise ValueError('max_calls 必须大于零')
    request = dict(request)
    initial = {'task':task,'request':request,'model':model,'max_calls':max_calls,
               'runtime':runtime,'calls':0,'trace':[]}
    try:
        if trace_file is not None:
            trace_file = os.fspath(trace_file)
            with open(trace_file,'x',encoding='utf-8') as stream:
                stream.write(json.dumps({'event':'start','task':task,'model':model},ensure_ascii=False)+'\n')
            initial['trace_file'] = trace_file
        if initial['calls'] >= max_calls:
            output = {'task':task,'model':model,'result':limit(initial)['result'],'trace':initial['trace'],
                      'final_decision':{'action':'limit','reason':'达到工具调用上限'}}
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
                             'pending_confirmation':'替代时段等待用户确认，尚未发布'}.get(outcome.get('status'),'请查看工具记录')
        if final.get('action')=='finish':
            source=state.get('decision_source','controller_error')
            ending={'action':'finish','reason':finish_reason,'decision_source':source}
            if source in ('real','mock'):
                model_reason=final.get('reason','')
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
