"""Opt-in, local SQLite progress for resuming content plans across CLI runs."""
import hashlib
import json
import math
import os
import sqlite3

from .tasks import plan_catalog


def signature(request):
    # Failure-injection flags may change to let the failed step succeed on resume.
    ignored = {'fail_tools','fail_once','fail_when','save_fail'}
    business = {k:v for k,v in request.items() if k not in ignored}
    if business.get('deadline') is not None:
        business.pop('deadline_hours',None)  # derived from the absolute deadline on each invocation
    payload = {'request':business, 'catalog':plan_catalog(request)}
    encoded = json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _connect(runtime):
    os.makedirs(runtime,exist_ok=True)
    conn = sqlite3.connect(os.path.join(runtime,'drafts.sqlite3'),timeout=10)
    conn.execute('''CREATE TABLE IF NOT EXISTS plan_checkpoints (
        run_id TEXT PRIMARY KEY, signature TEXT NOT NULL, started_wall REAL NOT NULL,
        version INTEGER NOT NULL, trace TEXT NOT NULL)''')
    return conn


def create(runtime, run_id, digest, started_wall):
    with _connect(runtime) as conn:
        try:
            conn.execute('INSERT INTO plan_checkpoints VALUES (?,?,?,?,?)',
                         (run_id,digest,started_wall,0,'[]'))
        except sqlite3.IntegrityError as exc:
            raise ValueError('运行 ID 已存在；请使用 --resume 或选择新的运行 ID') from exc


def load(runtime, run_id, digest):
    with _connect(runtime) as conn:
        row = conn.execute('SELECT signature,started_wall,version,trace FROM plan_checkpoints WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        raise ValueError('未找到该运行 ID 的续跑记录')
    saved_digest,started_wall,version,encoded = row
    if saved_digest != digest:
        raise ValueError('续跑请求或素材费用表与原运行不一致')
    trace = json.loads(encoded)
    if (not isinstance(started_wall,(int,float)) or not math.isfinite(started_wall)
        or not isinstance(version,int) or version < 0 or not isinstance(trace,list) or len(trace)!=version):
        raise ValueError('续跑记录格式无效')
    for number, entry in enumerate(trace,1):
        if (not isinstance(entry,dict) or entry.get('step')!=number or not isinstance(entry.get('action'),str)
            or not isinstance(entry.get('args'),dict) or not isinstance(entry.get('observation'),dict)):
            raise ValueError('续跑轨迹格式无效')
    return {'started_wall':started_wall,'version':version,'trace':trace}


def advance(runtime, run_id, digest, version, trace):
    if len(trace) != version+1:
        raise ValueError('续跑轨迹版本无效')
    encoded=json.dumps(trace,ensure_ascii=False,sort_keys=True,allow_nan=False)
    with _connect(runtime) as conn:
        cursor=conn.execute('UPDATE plan_checkpoints SET version=?,trace=? WHERE run_id=? AND signature=? AND version=?',
                            (version+1,encoded,run_id,digest,version))
        if cursor.rowcount != 1:
            raise ValueError('续跑记录已被其他执行更新，请重新加载')
    return version+1
