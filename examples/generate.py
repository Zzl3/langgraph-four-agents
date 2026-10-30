"""Regenerate the selected scheduling task's offline input/output examples."""
import json
import tempfile
from pathlib import Path

from src.engine import run

CASES = {
    'schedule_normal': {'material_id':'M01','date':'2026-11-01','time':'20:00'},
    'schedule_conflict': {'material_id':'M01','date':'2026-11-01','time':'18:00'},
    'schedule_missing': {'material_id':'M02','date':'2026-11-01','time':'18:00'},
    'schedule_risk': {'material_id':'M03','date':'2026-11-01','time':'18:00'},
    'schedule_failure': {'material_id':'M01','date':'2026-11-01','time':'18:00','save_fail':True},
    'schedule_multiple': {'material_ids':['M01','M04','M03'],'date':'2026-11-01','time':'18:00',
                          'available':['19:00','20:00']},
}


def generate():
    for name, request in CASES.items():
        Path('examples/inputs').mkdir(parents=True, exist_ok=True)
        Path('examples/outputs').mkdir(parents=True, exist_ok=True)
        Path(f'examples/inputs/{name}.json').write_text(
            json.dumps(request,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        with tempfile.TemporaryDirectory() as runtime:
            output=run('schedule',request,runtime=runtime)
        Path(f'examples/outputs/{name}.json').write_text(
            json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    request=CASES['schedule_conflict']
    with tempfile.TemporaryDirectory() as runtime:
        first=run('schedule',request,runtime=runtime)
        second=run('schedule',request,runtime=runtime)
    Path('examples/outputs/schedule_conflict_repeat.json').write_text(
        json.dumps({'first':first,'repeat':second},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    generate()
