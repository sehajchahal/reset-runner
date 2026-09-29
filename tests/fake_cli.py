#!/usr/bin/env python3
"""Deterministic provider simulator. Never contacts a real AI service."""
import json
import os
from pathlib import Path
import sys
import time

sid = 'a0000000-0000-4000-8000-000000000001'
def emit(obj):
    print(json.dumps(obj), flush=True)
if 'app-server' in sys.argv:
    for line in sys.stdin:
        req = json.loads(line)
        if req.get('method') == 'initialize':
            emit({'id': req['id'], 'result': {}})
        if req.get('method') == 'account/rateLimits/read':
            emit({'id': req['id'], 'result': {'rateLimits': {'primary': {'usedPercent':100,'resetsAt':time.time()+1}}}})
    sys.exit()
prompt = sys.stdin.read()
state = Path('fake_attempts.jsonl')
with state.open('a') as f:
    f.write(json.dumps({'args':sys.argv[1:], 'at':time.time(), 'prompt':prompt})+'\n')
count = len(state.read_text().splitlines())
claude = '-p' in sys.argv
emit({'type':'system','subtype':'init','session_id':sid} if claude else {'type':'thread.started','thread_id':sid})
if prompt == 'slow':
    time.sleep(60)
if prompt == 'auth':
    emit({'type':'result','is_error':True,'result':'Authentication failed','session_id':sid} if claude else {'type':'turn.failed','error':{'message':'Authentication failed'}})
    sys.exit(1)
if count == 1:
    if claude:
        emit({'type':'rate_limit_event','rate_limit_info':{'status':'rejected','resetsAt':time.time()+1}})
        emit({'type':'result','is_error':True,'result':'Usage limit reached','session_id':sid})
    else:
        emit({'type':'turn.failed','error':{'message':'Usage limit reached'}})
    sys.exit(1)
emit({'type':'result','subtype':'success','is_error':False,'session_id':sid} if claude else {'type':'turn.completed'})
