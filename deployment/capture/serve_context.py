#!/usr/bin/env python3
"""Restricted read-only Sock Shop context. JSON stdin accepts only start/end.

Installed root-owned; forced SSH command and sudo wrapper take no arguments.
Never executes shell text, reads container environments, or accepts a container
name from the caller. Output and Docker call times are bounded.
"""
import datetime
import json
import math
import selectors
import signal
import subprocess
import sys
import time

NAMES = tuple(
    f'docker-compose_{service}_{i}'
    for service in ('catalogue', 'front-end') for i in range(1, 5)
) + ('docker-compose_catalogue-db_1',)
MAX_OUTPUT = 2 * 1024 * 1024
# Leave time for SSH cleanup before the evidence service's 100-second timeout.
# Exhaustion produces a partial response, so already collected logs survive.
CONTEXT_TIMEOUT = 85


def validate_request(request, now=None):
    if not isinstance(request, dict) or set(request) != {'start', 'end'}:
        raise ValueError('Expected start and end only')
    start, end = request['start'], request['end']
    now = time.time() if now is None else now
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)):
        raise ValueError('Invalid time')
    if not 0 < end - start <= 600 or start < now - 3600 or end > now + 5:
        raise ValueError('Only a bounded recent interval is allowed')
    return start, end


def bounded(command, limit=MAX_OUTPUT, timeout=8):
    p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    data = bytearray(); truncated = False; timed_out = False
    selector = selectors.DefaultSelector(); selector.register(p.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() >= deadline:
                timed_out = True; break
            if not selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                continue
            chunk = p.stdout.read1(min(65536, limit + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
            if len(data) > limit:
                truncated = True; break
    finally:
        selector.close()
        if p.poll() is None and not truncated and not timed_out:
            try: p.wait(timeout=1)
            except subprocess.TimeoutExpired: timed_out=True
        if p.poll() is None:
            p.kill()
        p.wait(timeout=3); p.stdout.close()
    return {'text':bytes(data[:limit]).decode('utf-8', errors='replace'),
            'truncated':truncated,'timed_out':timed_out,'exit_code':p.returncode}


def collect(start, end):
    def iso(t): return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()
    records = []
    deadline = time.monotonic() + CONTEXT_TIMEOUT

    def within_budget(command, limit=MAX_OUTPUT):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'text':'', 'truncated':False, 'timed_out':True,
                    'exit_code':None, 'reason':'context_time_budget_exhausted'}
        return bounded(command, limit=limit, timeout=min(8, remaining))

    # Select fields only. In particular .Config.Env and command arguments are not read.
    fmt = '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},"image_id":{{json .Image}},"state":{{json .State.Status}},"started_at":{{json .State.StartedAt}},"restart_count":{{json .RestartCount}},"networks":{{json .NetworkSettings.Networks}},"log_driver":{{json .HostConfig.LogConfig.Type}}}'
    for name in NAMES:
        metadata = within_budget(['/usr/bin/docker','inspect','--format',fmt,name],limit=65536)
        logs = within_budget(['/usr/bin/docker','logs','--timestamps','--since',iso(start),'--until',iso(end),'--tail','2000',name])
        records.append({'container':name,'metadata':metadata,'logs':logs,
                        'log_tail_limit':2000,'log_line_limit_may_apply':len(logs['text'].splitlines()) >= 2000})
    return {'start':start,'end':end,'captured_at':time.time(),'containers':records,
            'scope':'catalogue and front-end replicas plus catalogue-db stdout/stderr; application logs may omit requests',
            'context_time_limit_seconds':CONTEXT_TIMEOUT}


def main():
    if len(sys.argv) != 1: return 64
    try:
        signal.alarm(10)
        raw = sys.stdin.buffer.readline(4097)
        signal.alarm(0)
        if len(raw) > 4096: raise ValueError('Request too large')
        start,end=validate_request(json.loads(raw))
        print(json.dumps(collect(start,end),ensure_ascii=False))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc),file=sys.stderr); return 1

if __name__ == '__main__': raise SystemExit(main())
