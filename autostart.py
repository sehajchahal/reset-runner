#!/usr/bin/env python3
"""Explicit, per-user login startup registration. Run after putting this folder in its final location."""
import argparse
import base64
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
LABEL = 'local.reset-runner'

def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'remove'])
    args = parser.parse_args()
    python = str(Path(sys.executable).absolute())
    script = str(ROOT / 'reset_runner.py')
    state = ROOT / '.reset-runner'
    if sys.platform == 'darwin':
        target = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
        domain = 'gui/' + str(os.getuid())
        subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
        if args.action == 'remove':
            target.unlink(missing_ok=True)
        else:
            state.mkdir(exist_ok=True, mode=0o700)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('wb') as f:
                plistlib.dump({'Label': LABEL, 'ProgramArguments': [python, script, 'daemon'],
                    'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
                    'WorkingDirectory': str(ROOT),
                    'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')},
                    'StandardOutPath': str(state/'startup.log'),
                    'StandardErrorPath': str(state/'startup.log')}, f)
            subprocess.run([python, script, 'stop'], check=True)
            # Wait for the worker to release its lock before launching the login worker.
            sys.path.insert(0, str(ROOT))
            from reset_runner import DaemonLock
            import time
            for _ in range(60):
                try:
                    lock = DaemonLock(state/'daemon.lock')
                    lock.close()
                    break
                except RuntimeError:
                    time.sleep(.5)
            else:
                raise RuntimeError('Worker did not stop; retry installation after checking status')
            subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
    elif os.name == 'nt':
        if args.action == 'remove':
            code = "Unregister-ScheduledTask -TaskName 'Reset Runner' -Confirm:$false -ErrorAction SilentlyContinue"
        else:
            # subprocess.list2cmdline applies Windows argument quoting; PowerShell string is separately escaped.
            subprocess.run([python, script, 'stop'], check=True)
            import time
            sys.path.insert(0, str(ROOT))
            from reset_runner import DaemonLock, Store
            Store(state).db.close()
            for _ in range(60):
                try:
                    lock = DaemonLock(state/'daemon.lock')
                    lock.close()
                    break
                except RuntimeError:
                    time.sleep(.5)
            else:
                raise RuntimeError('Worker did not stop; retry installation after checking status')
            argv = subprocess.list2cmdline([script, 'daemon'])
            code = ("$ErrorActionPreference='Stop'; "
                    "$a=New-ScheduledTaskAction -Execute " + ps_quote(python) + " -Argument " + ps_quote(argv) +
                    " -WorkingDirectory " + ps_quote(ROOT) + "; "
                    "$t=New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name); "
                    "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                    "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
                    "Register-ScheduledTask -TaskName 'Reset Runner' -Action $a -Trigger $t -Settings $s "
                    "-RunLevel Limited -Force | Out-Null; Start-ScheduledTask -TaskName 'Reset Runner'")
        encoded = base64.b64encode(code.encode('utf-16-le')).decode('ascii')
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded], check=True)
    else:
        raise RuntimeError('Autostart registration supports macOS and Windows; use your service manager on Linux/WSL')
    print('Login startup ' + ('installed.' if args.action == 'install' else 'removed.'))
    if args.action == 'remove':
        subprocess.run([python, script, 'stop'], check=True)

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('Error:', exc, file=sys.stderr)
        sys.exit(1)
