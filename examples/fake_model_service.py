"""Local OpenAI-compatible simulator for adapter demonstrations; not an LLM."""
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.tasks import mock_decide


@contextmanager
def local_model_service(fail_after_observations=None, delay_seconds=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            if self.path != '/v1/chat/completions':
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            payload = json.loads(body['messages'][-1]['content'])
            observations = payload['observations']
            if fail_after_observations is not None and len(observations) >= fail_after_observations:
                status = 400
                response = {'error':{'message':'simulated provider failure','type':'invalid_request_error'}}
                decision = None
            else:
                status = 200
                if payload.get('resume_context') and observations and not observations[-1]['observation'].get('ok'):
                    previous=observations[-1]
                    decision={'action':previous['action'],'args':previous['args'],'reason':'模拟服务重试失败步骤'}
                else:
                    decision = mock_decide({'task':payload['task'], 'request':payload['request'], 'trace':observations})
                response = {'id':'chatcmpl-local','object':'chat.completion','created':0,'model':'local-simulator',
                            'choices':[{'index':0,'finish_reason':'stop',
                                        'message':{'role':'assistant','content':json.dumps(decision,ensure_ascii=False)}}]}
            self.server.received.append({'body':body,'payload':payload,'decision':decision})
            if delay_seconds:
                time.sleep(delay_seconds)
            encoded = json.dumps(response,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
