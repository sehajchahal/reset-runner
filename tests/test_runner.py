import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reset_runner as r
FAKE = str(ROOT / 'tests' / 'fake_cli.py')
SID = 'a0000000-0000-4000-8000-000000000001'

class Parsing(unittest.TestCase):
    def test_timestamp(self):
        self.assertEqual(r.timestamp(1800000000000), 1800000000)
        self.assertEqual(r.timestamp('2026-01-01T00:00:00Z'), 1767225600)
        for v in ('tomorrow', '2026-01-01T00:00:00', float('nan'), True, -2):
            self.assertIsNone(r.timestamp(v))

    def test_relative_reset(self):
        self.assertEqual(r.error_reset('Usage limit; try again in 2h 30m', 100), 9100)

    def test_iso_reset(self):
        self.assertEqual(r.error_reset('resets at 2026-01-01T00:00:00Z', 0), 1767225600)

    def test_claude_rejected(self):
        o = r.Observation()
        o.feed({'type':'rate_limit_event','rate_limit_info':{'status':'rejected','resetsAt':1800000000}}, 100)
        self.assertTrue(o.limited)
        self.assertEqual(o.resets, [1800000000])

    def test_warning_does_not_retry(self):
        o = r.Observation()
        o.feed({'type':'rate_limit_event','rate_limit_info':{'status':'allowed_warning','resetsAt':1800000000}}, 100)
        self.assertFalse(o.limited)

    def test_tool_and_assistant_text_cannot_trigger(self):
        o = r.Observation()
        for e in ({'type':'item.completed','item':{'aggregated_output':'Usage limit reached'}},
                  {'type':'assistant','message':{'content':'Rate limit exceeded'}}):
            o.feed(e, 100)
        self.assertFalse(o.limited)

    def test_auth_is_not_limit(self):
        o = r.Observation()
        o.feed({'type':'turn.failed','error':{'message':'Authentication failed'}}, 100)
        self.assertTrue(o.failed)
        self.assertFalse(o.limited)

    def test_codex_session(self):
        o = r.Observation()
        o.feed({'type':'thread.started','thread_id':SID}, 100)
        self.assertEqual(o.session, SID)

    def test_permission_denial(self):
        o = r.Observation()
        o.feed({'type':'result','subtype':'success','permission_denials':[{'tool':'Bash'}]}, 100)
        self.assertTrue(o.denied)

    def test_multiple_windows(self):
        rate = {'rateLimitsByLimitId':{'codex':{
            'primary':{'usedPercent':100,'resetsAt':500},
            'secondary':{'usedPercent':100,'resetsAt':1000}},
            'other':{'primary':{'usedPercent':100,'resetsAt':5000}}}}
        self.assertEqual(r.blocking_reset(rate, 100), 1000)
        self.assertIsNone(r.blocking_reset(rate, 1100))

    def test_unexhausted_weekly_does_not_delay(self):
        rate = {'rateLimits':{'primary':{'usedPercent':100,'resetsAt':500},
                            'secondary':{'usedPercent':99,'resetsAt':1000}}}
        self.assertEqual(r.blocking_reset(rate, 100), 500)

    def test_unknown_meter_does_not_use_other_bucket(self):
        self.assertIsNone(r.blocking_reset({'rateLimitsByLimitId':{'other':{}}}, 0))

    def test_backoff_is_bounded(self):
        o = r.Observation()
        self.assertEqual(r.retry_due(o, 1, 100)[0], 160)
        self.assertEqual(r.retry_due(o, 999, 100)[0], 1000)
        o.resets = [500, 800]
        self.assertEqual(r.retry_due(o, 1, 100)[0], 803)

class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = r.Store(self.path / 'data')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def add(self, provider='claude', prompt='do work'):
        return self.store.add(provider, FAKE, str(self.path), prompt)

    def test_cancellation_wins_late_completion(self):
        id = self.add()
        self.store.update(id, state='cancelled')
        self.store.update(id, _unless_cancelled=True, state='done')
        self.assertEqual(self.store.job(id)['state'], 'cancelled')
        r.execute(self.store, self.store.job(id), threading.Event())
        self.assertEqual(self.store.job(id)['attempts'], 0)

    def test_duplicate_session_rejected(self):
        self.store.add('claude', FAKE, str(self.path), 'x', SID)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add('claude', FAKE, str(self.path), 'x', SID)
        self.store.db.rollback()

    def test_lock_excludes_second_worker(self):
        lock = r.DaemonLock(self.path / 'lock')
        try:
            with self.assertRaises(RuntimeError):
                r.DaemonLock(self.path / 'lock')
        finally:
            lock.close()
        r.DaemonLock(self.path / 'lock').close()

    @unittest.skipIf(sys.platform == 'win32', 'POSIX executable fixture; Windows runtime not available')
    def test_claude_limit_then_exact_resume(self):
        id = self.add()
        r.execute(self.store, self.store.job(id), threading.Event())
        j = self.store.job(id)
        self.assertEqual(j['state'], 'waiting')
        self.assertEqual(j['session'], SID)
        self.assertGreater(j['due'], time.time())
        r.execute(self.store, j, threading.Event())
        self.assertEqual(self.store.job(id)['state'], 'done')
        calls = [json.loads(x) for x in (self.path/'fake_attempts.jsonl').read_text().splitlines()]
        self.assertIn(SID, calls[1]['args'])
        self.assertEqual(calls[1]['prompt'], r.CONTINUE)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX executable fixture')
    def test_codex_appserver_and_resume(self):
        id = self.add('codex')
        r.execute(self.store, self.store.job(id), threading.Event())
        j = self.store.job(id)
        self.assertEqual(j['state'], 'waiting')
        self.assertIn('Provider reset', j['message'])
        r.execute(self.store, j, threading.Event())
        self.assertEqual(self.store.job(id)['state'], 'done')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX executable fixture')
    def test_non_limit_failure_stops(self):
        id = self.add(prompt='auth')
        r.execute(self.store, self.store.job(id), threading.Event())
        self.assertEqual(self.store.job(id)['state'], 'attention')
        self.assertEqual(self.store.job(id)['attempts'], 1)

    def test_command_pins_id_and_uses_stdin(self):
        id = self.add('codex', '$(touch bad)')
        self.store.update(id, session=SID)
        cmd, prompt = r.command(self.store.job(id))
        self.assertIn(SID, cmd)
        self.assertNotIn('--last', cmd)
        self.assertEqual(cmd[-1], '-')
        self.assertEqual(prompt, r.CONTINUE)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX executable fixture')
    def test_daemon_due_time_and_restart(self):
        id = self.add()
        cmd = [sys.executable, str(ROOT/'reset_runner.py'), '--data', str(self.store.path), 'daemon']
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            self.wait_state(id, 'waiting')
            due = self.store.job(id)['due']
            self.store.meta('stop', '1')
            p.wait(timeout=5)
            p.stderr.close()
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.wait_state(id, 'done', timeout=10)
            calls = [json.loads(x) for x in (self.path/'fake_attempts.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertGreaterEqual(calls[1]['at'], due)
            self.assertLess(calls[1]['at'] - due, 2)
        finally:
            self.store.meta('stop', '1')
            p.wait(timeout=5)
            p.stderr.close()

    def wait_state(self, id, state, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.store.job(id)['state'] == state:
                return
            time.sleep(.05)
        self.fail('Expected '+state+', got '+str(self.store.job(id)))

    @unittest.skipIf(sys.platform == 'win32', 'POSIX executable fixture')
    def test_cancel_running(self):
        id = self.add(prompt='slow')
        p = subprocess.Popen([sys.executable,str(ROOT/'reset_runner.py'),'--data',str(self.store.path),'daemon'])
        try:
            self.wait_state(id, 'running')
            self.store.update(id, state='cancelled')
            time.sleep(1.5)
            self.assertEqual(self.store.job(id)['state'], 'cancelled')
        finally:
            self.store.meta('stop', '1')
            p.wait(timeout=5)

    def test_crash_recovery_no_automatic_replay(self):
        id = self.add()
        self.store.update(id, state='running')
        p = subprocess.Popen([sys.executable,str(ROOT/'reset_runner.py'),'--data',str(self.store.path),'daemon'])
        try:
            self.wait_state(id, 'attention')
            self.assertFalse((self.path/'fake_attempts.jsonl').exists())
        finally:
            self.store.meta('stop', '1')
            p.wait(timeout=5)

if __name__ == '__main__':
    unittest.main()
