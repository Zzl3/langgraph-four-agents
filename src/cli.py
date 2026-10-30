import argparse
import json
from dotenv import load_dotenv
from .engine import run

def main():
    load_dotenv(override=False)
    p = argparse.ArgumentParser(description='素材审核与发布排期 Agent')
    p.add_argument('--input',required=True,help='JSON 文件路径')
    p.add_argument('--model',choices=['mock','real'],default='mock')
    p.add_argument('--max-calls',type=int,default=20)
    p.add_argument('--runtime',default='runtime')
    p.add_argument('--trace-file',help='增量写入 JSONL 轨迹到新的文件（不覆盖已有文件）')
    args = p.parse_args()
    with open(args.input,encoding='utf-8') as f: request=json.load(f)
    print(json.dumps(run('schedule',request,args.model,args.max_calls,args.runtime,trace_file=args.trace_file),ensure_ascii=False,indent=2))

if __name__ == '__main__': main()
