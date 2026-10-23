"""Restricted remote evidence commands must stay bounded and read-only."""
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from deployment.capture import serve_context as context


EXPECTED_NAMES = {
    'docker-compose_catalogue_1', 'docker-compose_catalogue_2',
    'docker-compose_catalogue_3', 'docker-compose_catalogue_4',
    'docker-compose_front-end_1', 'docker-compose_front-end_2',
    'docker-compose_front-end_3', 'docker-compose_front-end_4',
    'docker-compose_catalogue-db_1',
}


def test_context_scope_and_commands_are_fixed_and_read_only(monkeypatch):
    calls = []

    def bounded(command, **kwargs):
        calls.append((command, kwargs))
        return {'text':'ok\n', 'truncated':False, 'timed_out':False, 'exit_code':0}

    monkeypatch.setattr(context, 'bounded', bounded)
    result = context.collect(1000, 1100)
    assert len(context.NAMES) == 9
    assert set(context.NAMES) == EXPECTED_NAMES
    assert {r['container'] for r in result['containers']} == EXPECTED_NAMES
    assert len(calls) == 18
    for command, limits in calls:
        assert command[0] == '/usr/bin/docker'
        assert command[-1] in EXPECTED_NAMES
        assert 0 < limits['timeout'] <= 8
        if command[1] == 'inspect':
            assert command[2] == '--format'
            assert '.Config.Env' not in command[3]
            assert '.Config.Cmd' not in command[3]
            assert limits['limit'] == 65536
        else:
            assert command[1:] == [
                'logs', '--timestamps', '--since', '1970-01-01T00:16:40+00:00',
                '--until', '1970-01-01T00:18:20+00:00', '--tail', '2000', command[-1],
            ]
            assert limits['limit'] == context.MAX_OUTPUT
    assert 'front-end' in result['scope']
    assert 'application logs may omit requests' in result['scope']
    assert result['context_time_limit_seconds'] < 100


def test_context_budget_preserves_finished_logs_and_marks_unread_containers(monkeypatch):
    clock = [0.0]
    calls = []

    def bounded(command, **kwargs):
        calls.append(command)
        clock[0] += 8
        return {'text':'preserved\n', 'truncated':False, 'timed_out':False, 'exit_code':0}

    monkeypatch.setattr(context.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(context, 'CONTEXT_TIMEOUT', 16)
    monkeypatch.setattr(context, 'bounded', bounded)
    result = context.collect(1000, 1100)
    assert len(calls) == 2
    assert result['containers'][0]['logs']['text'] == 'preserved\n'
    assert len(result['containers']) == 9
    for record in result['containers'][1:]:
        for kind in ('metadata', 'logs'):
            assert record[kind]['timed_out'] is True
            assert record[kind]['exit_code'] is None
            assert record[kind]['reason'] == 'context_time_budget_exhausted'


def test_context_log_truncation_is_exposed(monkeypatch):
    def bounded(command, **kwargs):
        return {'text':'line\n' * 2000, 'truncated':True, 'timed_out':False, 'exit_code':0}

    monkeypatch.setattr(context, 'bounded', bounded)
    result = context.collect(1000, 1100)
    assert all(r['log_line_limit_may_apply'] for r in result['containers'])


@pytest.mark.parametrize('payload', [
    {'start':1000, 'end':1100, 'container':'unrelated'},
    {'start':1000, 'end':1100, 'command':'docker exec'},
    {'start':1000, 'end':1700},
    {'start':1000, 'end':1000},
    {'start':1000, 'end':float('inf')},
    {'start':True, 'end':1100},
])
def test_context_request_cannot_expand_scope(payload):
    with pytest.raises(ValueError):
        context.validate_request(payload, now=1100)


def test_context_accepts_recent_bounded_interval_only():
    assert context.validate_request({'start':1000, 'end':1100}, now=1100) == (1000, 1100)
    with pytest.raises(ValueError):
        context.validate_request({'start':1000, 'end':1100}, now=5000)


def test_context_cli_rejects_arguments_without_running_docker():
    module = Path(context.__file__)
    result = subprocess.run([sys.executable, str(module), '--container', 'arbitrary'],
                            input=json.dumps({'start':1000, 'end':1100}).encode(),
                            capture_output=True, timeout=3)
    assert result.returncode == 64
    assert result.stdout == b''


def test_context_wrapper_uses_short_shared_wait_within_ssh_timeout():
    wrapper = Path(context.__file__).with_name('hycad-context-catalogue').read_text()
    command = next(line for line in wrapper.splitlines() if line.startswith('exec '))
    assert shlex.split(command) == [
        'exec', '/usr/bin/flock', '--wait', '5', '--no-fork',
        '/run/lock/hycad-context.lock', '/usr/bin/python3',
        '/usr/local/libexec/hycad-serve-context.py',
    ]
    assert '"$#" -eq 0' in wrapper
    assert context.CONTEXT_TIMEOUT + 5 < 100
