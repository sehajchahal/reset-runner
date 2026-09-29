#!/usr/bin/env python3
"""Reset Runner: durable, local continuation for Codex and Claude Code. Python 3.10+."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parent
CONTINUE = ('Continue the interrupted task from the saved conversation. Inspect current state '
            'before repeating any action; preserve completed work. Follow the original scope '
            'and permissions. If the task is already complete, report completion and stop.')
LIMIT = re.compile(r'usage[_ ]limit|rate[_ -]?limit|you.ve hit your limit|limit reached|too many requests', re.I)


def timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            n = float(value)
            n = n / 1000 if n > 1e12 else n
        elif isinstance(value, str):
            d = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if d.tzinfo is None:
                return None
            n = d.timestamp()
        else:
            return None
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, OverflowError):
        return None


def error_reset(text, now):
    m = re.search(r'(?:resets? at|try again (?:at|after))\s+(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d))', text, re.I)
    if m:
        return timestamp(m[1])
    m = re.search(r'(?:try again|resets?) in\s+((?:\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\s*)+)', text, re.I)
    if m:
        seconds = sum(float(n) * (3600 if u.lower().startswith('h') else 60 if u.lower().startswith('m') else 1)
                      for n, u in re.findall(r'(\d+(?:\.\d+)?)\s*([a-z]+)', m[1], re.I))
        return now + seconds
    return None


class Observation:
    def __init__(self):
        self.session = None
        self.limited = False
        self.resets = []
        self.failed = False
        self.completed = False
        self.message = ''
        self.denied = False

    def error(self, value, now):
        text = value if isinstance(value, str) else json.dumps(value)
        self.failed = True
        self.message = text[:2000]
        if LIMIT.search(text):
            self.limited = True
            r = error_reset(text, now)
            if r:
                self.resets.append(r)
            if isinstance(value, dict):
                for key in ('resetsAt', 'resets_at', 'reset_at'):
                    r = timestamp(value.get(key))
                    if r:
                        self.resets.append(r)

    def feed(self, event, now):
        if not isinstance(event, dict):
            return
        kind = event.get('type')
        # Never interpret assistant/tool prose as control instructions or limit errors.
        if kind == 'thread.started':
            self.session = event.get('thread_id')
        if kind == 'system' and event.get('subtype') == 'init':
            self.session = event.get('session_id')
        if kind == 'rate_limit_event':
            info = event.get('rate_limit_info') or {}
            if info.get('status') == 'rejected':
                self.limited = True
                r = timestamp(info.get('resetsAt', info.get('resets_at')))
                if r:
                    self.resets.append(r)
        if kind in ('error', 'turn.failed'):
            self.error(event.get('error', event.get('message', event)), now)
        if kind == 'assistant' and event.get('error'):
            self.error(event['error'], now)
        if kind == 'turn.completed':
            self.completed = True
        if kind == 'result':
            self.session = event.get('session_id') or self.session
            self.denied = bool(event.get('permission_denials'))
            if event.get('is_error') or event.get('subtype') not in (None, 'success'):
                self.error(event.get('errors') or event.get('result') or event, now)
            else:
                self.completed = True


def blocking_reset(result, now, bucket='codex'):
    """Only the selected meter matters. Wait for *all* exhausted windows."""
    buckets = result.get('rateLimitsByLimitId')
    rate = buckets.get(bucket) if isinstance(buckets, dict) else result.get('rateLimits')
    if not isinstance(rate, dict):
        return None
    resets = []
    for key in ('primary', 'secondary'):
        w = rate.get(key)
        if isinstance(w, dict) and isinstance(w.get('usedPercent'), (int, float)) and w['usedPercent'] >= 100:
            r = timestamp(w.get('resetsAt'))
            if r and r > now:
                resets.append(r)
    return max(resets) if resets else None


def spawn_options():
    return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}


def terminate(p):
    if p.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            p.kill()
        p.wait()


def codex_limits(executable, cwd, timeout=15):
    """Official app-server protocol; no token extraction or private HTTP endpoints."""
    p = subprocess.Popen([executable, 'app-server'], cwd=cwd, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         encoding='utf-8', errors='replace', **spawn_options())
    q = queue.Queue()
    def reader():
        for line in p.stdout:
            try:
                q.put(json.loads(line))
            except ValueError:
                pass
        q.put(None)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    def send(payload):
        p.stdin.write(json.dumps(payload) + '\n')
        p.stdin.flush()
    def response(id):
        while True:
            obj = q.get(timeout=max(.01, deadline - time.monotonic()))
            if obj is None:
                raise RuntimeError('Codex app-server closed before replying')
            if obj.get('id') == id:
                if 'error' in obj:
                    raise RuntimeError(str(obj['error']))
                return obj.get('result', {})
    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'reset_runner', 'version': VERSION}}})
        response(1)
        send({'method': 'initialized', 'params': {}})
        send({'id': 2, 'method': 'account/rateLimits/read'})
        return response(2)
    except queue.Empty:
        raise RuntimeError('Codex usage query timed out') from None
    finally:
        terminate(p)
        p.stdin.close()
        t.join(timeout=2)
        p.stdout.close()


class Store:
    def __init__(self, directory):
        self.path = Path(directory).resolve()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path / 'queue.sqlite3', timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, executable TEXT NOT NULL,
          cwd TEXT NOT NULL, prompt TEXT NOT NULL, session TEXT, model TEXT,
          state TEXT NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 100, message TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS active_session ON jobs(provider, session)
          WHERE session IS NOT NULL AND state IN ('queued','waiting','running');
        ''')
        self.db.commit()
        try:
            os.chmod(self.path / 'queue.sqlite3', 0o600)
        except OSError:
            pass

    def meta(self, key, value=None):
        if value is not None:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))
            self.db.commit()
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def update(self, id, _unless_cancelled=False, **fields):
        fields['updated'] = time.time()
        suffix = " AND state!='cancelled'" if _unless_cancelled else ''
        changed = self.db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?' + suffix, [*fields.values(), id]).rowcount
        self.db.commit()
        return changed

    def job(self, id):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (id,)).fetchone()
        if row is None:
            raise ValueError('Unknown job ID: ' + id)
        return dict(row)

    def add(self, provider, executable, cwd, prompt, session=None, model=None, due=None, max_attempts=100):
        id = uuid.uuid4().hex[:12]
        now = time.time()
        self.db.execute('''INSERT INTO jobs
          (id,provider,executable,cwd,prompt,session,model,state,due,max_attempts,created,updated)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
          (id, provider, executable, cwd, prompt, session, model, 'queued', due or now, max_attempts, now, now))
        self.db.commit()
        return id


class DaemonLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')
        self.file.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not self.file.read(1):
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError('Daemon already running') from None
        else:
            import fcntl
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError('Daemon already running') from None

    def close(self):
        self.file.close()


def command(job):
    prompt = CONTINUE if job['session'] else job['prompt']
    if job['provider'] == 'codex':
        cmd = [job['executable'], 'exec', '-c', 'sandbox_mode="workspace-write"',
               '-c', 'approval_policy="never"', '--json', '--skip-git-repo-check']
        if job['model']:
            cmd += ['--model', job['model']]
        if job['session']:
            cmd += ['resume', job['session']]
        cmd += ['-']  # prompt over stdin, not shell or process arguments
    else:
        cmd = [job['executable'], '-p', '--output-format', 'stream-json', '--verbose']
        if job['model']:
            cmd += ['--model', job['model']]
        if job['session']:
            cmd += ['--resume', job['session']]
    return cmd, prompt


def retry_due(obs, attempts, now):
    future = [r for r in obs.resets if r > now]
    if future:
        return max(future) + 3, 'Provider reset time + 3 seconds'
    # An unknown reset is not a guessed promise: a bounded, infrequent probe.
    delay = min(900, 60 * 2 ** min(max(attempts - 1, 0), 4))
    return now + delay, 'Reset time unavailable; bounded retry in %s seconds' % delay


def execute(store, job, stopping):
    id = job['id']
    if store.job(id)['state'] == 'cancelled':
        return
    if not store.update(id, _unless_cancelled=True, state='running', attempts=job['attempts'] + 1, message='Running'):
        return
    obs = Observation()
    cmd, prompt = command(job)
    logs = store.path / 'logs'
    logs.mkdir(exist_ok=True, mode=0o700)
    p = None
    try:
        with open(logs / (id + '.jsonl'), 'a', encoding='utf-8') as log:
            p = subprocess.Popen(cmd, cwd=job['cwd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                                 **spawn_options())
            q = queue.Queue(maxsize=256)
            def read(stream, name):
                for line in stream:
                    q.put((name, line))
                q.put((name, None))
            readers = [threading.Thread(target=read, args=(s, n), daemon=True)
                       for s, n in ((p.stdout, 'stdout'), (p.stderr, 'stderr'))]
            for t in readers:
                t.start()
            p.stdin.write(prompt)
            p.stdin.close()
            ended = 0
            stderr_tail = ''
            last_heartbeat = 0
            interrupted = False
            while ended < 2:
                if time.monotonic() - last_heartbeat >= 1:
                    store.meta('heartbeat', time.time())
                    last_heartbeat = time.monotonic()
                    current = store.job(id)
                    if stopping.is_set() or store.meta('stop') == '1' or current['state'] == 'cancelled':
                        terminate(p)
                        interrupted = True
                try:
                    name, line = q.get(timeout=.25)
                except queue.Empty:
                    continue
                if line is None:
                    ended += 1
                    continue
                log.write(json.dumps({'time': time.time(), 'stream': name, 'line': line.rstrip()}) + '\n')
                log.flush()
                if name == 'stderr':
                    stderr_tail = (stderr_tail + line)[-16000:]
                else:
                    try:
                        obs.feed(json.loads(line), time.time())
                    except ValueError:
                        pass
                    if obs.session and obs.session != job['session']:
                        # Pin immediately so a later resume never selects another conversation.
                        uuid.UUID(obs.session)
                        store.update(id, _unless_cancelled=True, session=obs.session)
                        job['session'] = obs.session
            rc = p.wait()
            for t in readers:
                t.join(timeout=1)
            if interrupted:
                if store.job(id)['state'] != 'cancelled':
                    store.update(id, _unless_cancelled=True, state='attention', message='Stopped during execution; inspect session then retry explicitly')
                return
            if rc and not obs.failed and not obs.limited:
                obs.error(stderr_tail or 'CLI exited with code %s' % rc, time.time())
            if obs.completed and not obs.failed and rc == 0:
                store.update(id, _unless_cancelled=True, state='attention' if obs.denied else 'done',
                             message='Permission denied; review session' if obs.denied else 'Provider completed the turn')
                return
            if obs.limited:
                if not job['session']:
                    store.update(id, _unless_cancelled=True, state='attention', message='Limit reached without a session ID; cannot safely resume')
                    return
                if job['attempts'] + 1 >= job['max_attempts']:
                    store.update(id, _unless_cancelled=True, state='attention', message='Retry cap reached; inspect and retry explicitly')
                    return
                if job['provider'] == 'codex':
                    try:
                        reset = blocking_reset(codex_limits(job['executable'], job['cwd']), time.time())
                        if reset:
                            obs.resets.append(reset)
                    except (OSError, RuntimeError):
                        pass
                due, reason = retry_due(obs, job['attempts'] + 1, time.time())
                if store.job(id)['state'] != 'cancelled':
                    store.update(id, _unless_cancelled=True, state='waiting', due=due, message=reason)
            else:
                store.update(id, _unless_cancelled=True, state='attention', message=obs.message or 'No successful completion event; inspect log')
    except Exception as exc:
        if store.job(id)['state'] != 'cancelled':
            store.update(id, _unless_cancelled=True, state='attention', message=str(exc)[:2000])
    finally:
        if p:
            terminate(p)
            for s in (p.stdin, p.stdout, p.stderr):
                if s and not s.closed:
                    s.close()


def daemon(directory):
    store = Store(directory)
    lock = DaemonLock(store.path / 'daemon.lock')
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    try:
        store.meta('stop', '0')
        store.meta('pid', os.getpid())
        # Unknown crash state must never silently replay potentially completed side effects.
        store.db.execute("UPDATE jobs SET state='attention', message='Worker interrupted; inspect session before retry' WHERE state='running'")
        store.db.commit()
        while not stopping.is_set() and store.meta('stop') != '1':
            store.meta('heartbeat', time.time())
            row = store.db.execute("SELECT * FROM jobs WHERE state IN ('queued','waiting') AND due<=? ORDER BY due,created LIMIT 1", (time.time(),)).fetchone()
            if row:
                execute(store, dict(row), stopping)
            else:
                stopping.wait(.5)
    finally:
        store.meta('heartbeat', '0')
        store.meta('pid', '0')
        lock.close()
        store.db.close()


def alive(store):
    # Heartbeat is diagnostic; the OS lock remains the actual ownership guard.
    return time.time() - float(store.meta('heartbeat') or 0) < 30


def start(store):
    if alive(store):
        return 'Daemon already running'
    with open(store.path / 'daemon.log', 'a', encoding='utf-8') as log:
        flags = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--data', str(store.path), 'daemon'],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(ROOT), **flags)
    for _ in range(50):
        if alive(store):
            return 'Daemon started'
        time.sleep(.1)
    raise RuntimeError('Daemon did not start; inspect ' + str(store.path / 'daemon.log'))


def resolve_executable(provider):
    path = shutil.which(provider)
    if not path and provider == 'codex' and sys.platform == 'darwin':
        candidate = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
        if candidate.is_file():
            path = str(candidate)
    if not path:
        raise ValueError(provider + ' CLI is not installed or not on PATH; see README')
    if Path(path).suffix.lower() in ('.cmd', '.bat'):
        raise ValueError('Use a native CLI executable or WSL, not a Windows .cmd/.bat shim')
    return str(Path(path).absolute())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default=str(ROOT / '.reset-runner'), help='Local state directory')
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('start', 'stop', 'daemon', 'status', 'doctor'):
        sub.add_parser(name)
    for name in ('run', 'attach'):
        p = sub.add_parser(name)
        p.add_argument('provider', choices=['codex', 'claude'])
        p.add_argument('--cwd', required=True, help='Trusted project directory')
        p.add_argument('--model')
        p.add_argument('--max-attempts', type=int, default=100)
        p.add_argument('--at', help='Do not start before this ISO 8601 time with UTC offset')
        if name == 'run':
            p.add_argument('prompt')
        else:
            p.add_argument('session', help='Exact saved session UUID (stop its other runner first)')
    for name in ('cancel', 'retry', 'logs'):
        sub.add_parser(name).add_argument('id')
    p = sub.add_parser('limits')
    p.add_argument('--cwd', default=str(Path.cwd()))
    args = parser.parse_args()
    try:
        if args.action == 'daemon':
            daemon(args.data)
            return
        store = Store(args.data)
        if args.action == 'doctor':
            print('Reset Runner', VERSION, '| Python', sys.version.split()[0])
            for provider in ('codex', 'claude'):
                try:
                    exe = resolve_executable(provider)
                    r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=10)
                    print(provider + ':', r.stdout.strip() or r.stderr.strip(), '\n ', exe)
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    print(provider + ':', exc)
            print('Data:', store.path, '\nWorker:', 'running' if alive(store) else 'stopped')
        elif args.action == 'start':
            print(start(store))
        elif args.action == 'stop':
            store.meta('stop', '1')
            print('Stop requested. Queued and waiting jobs remain saved.')
        elif args.action in ('run', 'attach'):
            cwd = str(Path(args.cwd).resolve())
            if not Path(cwd).is_dir():
                raise ValueError('Project directory does not exist')
            if args.max_attempts < 1:
                raise ValueError('max-attempts must be positive')
            due = timestamp(args.at) if args.at else None
            if args.at and due is None:
                raise ValueError('--at requires an ISO timestamp with timezone, e.g. 2026-09-29T09:00:00-04:00')
            session = str(uuid.UUID(args.session)) if args.action == 'attach' else None
            id = store.add(args.provider, resolve_executable(args.provider), cwd,
                           args.prompt if args.action == 'run' else CONTINUE,
                           session, args.model, due, args.max_attempts)
            print('Job:', id)
            print(start(store))
        elif args.action == 'status':
            print('Worker:', 'running' if alive(store) else 'stopped')
            rows = store.db.execute('SELECT * FROM jobs ORDER BY created DESC').fetchall()
            if not rows:
                print('No jobs. Use run or attach to register a task.')
            for row in rows:
                due = dt.datetime.fromtimestamp(row['due']).astimezone().isoformat(timespec='seconds')
                print(f"{row['id']}  {row['provider']:6}  {row['state']:9}  attempts={row['attempts']}  due={due}")
                print(' ', row['message'] or 'Queued', '| session:', row['session'] or 'not yet assigned')
        elif args.action == 'cancel':
            job = store.job(args.id)
            if job['state'] in ('queued', 'waiting', 'running'):
                store.update(args.id, state='cancelled', message='Cancelled by user')
            print('Job:', store.job(args.id)['state'])
        elif args.action == 'retry':
            job = store.job(args.id)
            if job['state'] != 'attention':
                raise ValueError('Only attention jobs can be retried; cancel/done jobs are final')
            store.update(args.id, state='queued', due=time.time(), attempts=0, message='Explicit retry')
            print(start(store))
        elif args.action == 'logs':
            store.job(args.id)
            path = store.path / 'logs' / (args.id + '.jsonl')
            if path.exists():
                with path.open(encoding='utf-8') as f:
                    from collections import deque
                    print(''.join(deque(f, maxlen=80)), end='')
            else:
                print('No output yet')
        elif args.action == 'limits':
            print(json.dumps(codex_limits(resolve_executable('codex'), args.cwd), indent=2))
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print('Error:', exc, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
