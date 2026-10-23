import ast
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time

import pytest

from scripts.collect_service import (CollectorLock, HealthState, SessionResult, build_service_command,
                                     run_service, run_session)
from scripts.collect_ssh import RotatingCapture


def test_service_requests_one_fixed_command_with_verified_key_only_ssh():
    command = build_service_command("hycad@192.0.2.10", Path("/etc/hycad/identity"),
                                    Path("/etc/hycad/known hosts"), 2222)
    assert command[-2:] == ["hycad@192.0.2.10", "hycad-capture"]
    assert 'IdentityFile="/etc/hycad/identity"' in command
    assert command[command.index("-p") + 1] == "2222"
    for option in ("BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none", "StrictHostKeyChecking=yes",
                   "PasswordAuthentication=no", "ConnectTimeout=10", "ServerAliveCountMax=3", "ControlPath=none"):
        assert option in command
    assert 'UserKnownHostsFile="/etc/hycad/known hosts"' in command
    assert not any("sudo" in arg for arg in command)


@pytest.mark.parametrize("host,port", [("-oProxyCommand=bad", 22), ("host with space", 22), ("host", 0)])
def test_service_rejects_option_injection_and_bad_port(host, port):
    with pytest.raises(ValueError):
        build_service_command(host, Path("key"), Path("known_hosts"), port)


def test_lock_prevents_duplicate_writer_and_releases_after_exit(tmp_path):
    path = tmp_path / "collector.lock"
    with CollectorLock(path):
        assert int(path.read_text()) == os.getpid()
        with pytest.raises(RuntimeError, match="Another collector"):
            with CollectorLock(path):
                pytest.fail("Duplicate collector acquired the lock")
    with CollectorLock(path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_group_readable_mode_survives_rotation_with_bounded_storage(tmp_path):
    path = tmp_path / "events.jsonl"
    with RotatingCapture(path, 64, file_mode=0o640) as writer:
        for index in range(20):
            writer.write((f'{{"index":{index}}}\n').encode())
        writer.flush()
    assert len(list(tmp_path.iterdir())) == 2
    for file in tmp_path.iterdir():
        assert file.stat().st_size <= 64
        assert stat.S_IMODE(file.stat().st_mode) == 0o640


def test_actual_child_exit_reconnects_and_partial_records_never_join_sessions(tmp_path):
    path = tmp_path / "events.jsonl"
    stop = threading.Event()
    health = HealthState(tmp_path / "health.json")
    attempts = []
    command = [sys.executable, "-c", "import sys;sys.stdout.write('{\"partial\":');sys.stdout.flush()"]

    def session_runner(*args):
        result = run_session(*args)
        attempts.append(result)
        if len(attempts) == 3:
            stop.set()
        return result

    with RotatingCapture(path, 4096, 0o640) as writer:
        run_service(command, writer, stop, 0.01, 2, health, session_runner)
    contents = path.read_bytes()
    assert len(attempts) == 3
    assert contents.count(b"!bridge_source_start\n") == 3
    assert contents.count(b"!bridge_truncated_event\n") == 3
    assert contents.count(b"!bridge_capture_stopped\n") == 3
    assert b'{"partial":' not in contents
    status = json.loads((tmp_path / "health.json").read_text())
    assert status["state"] == "stopped"
    assert status["attempts"] == 3
    assert status["restarts"] == 2


def test_idle_connection_is_terminated_and_marked_for_reconnect(tmp_path):
    path = tmp_path / "events.jsonl"
    command = [sys.executable, "-c", "import time;print('{\"event\":1}',flush=True);time.sleep(60)"]
    with RotatingCapture(path, 4096, 0o640) as writer:
        result = run_session(command, writer, threading.Event(), 0.1, HealthState())
    assert result.reason == "input_idle"
    assert result.elapsed < 3
    assert b'{"event":1}\n' in path.read_bytes()
    assert path.read_bytes().endswith(b"!bridge_capture_stopped\n")


def test_stop_interrupts_live_child_and_supervisor_without_restarting(tmp_path):
    path = tmp_path / "events.jsonl"
    stop = threading.Event()
    health = HealthState()
    command = [sys.executable, "-c", "import time;print('{\"event\":1}',flush=True);time.sleep(60)"]
    errors = []

    def worker():
        try:
            with RotatingCapture(path, 4096, 0o640) as writer:
                run_service(command, writer, stop, 30, 90, health)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 3
    while health.data["state"] != "streaming" and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=4)
    assert not thread.is_alive()
    assert not errors
    assert health.data["state"] == "stopped"
    assert health.data["attempts"] == 1


def test_exponential_backoff_is_bounded_and_stable_session_resets_it(tmp_path):
    class NoWaitEvent(threading.Event):
        def __init__(self):
            super().__init__()
            self.delays = []

        def wait(self, timeout=None):
            self.delays.append(timeout)
            return self.is_set()

    stop = NoWaitEvent()
    attempts = [0]

    def runner(*args):
        attempts[0] += 1
        if attempts[0] == 9:
            stop.set()
        # A stable data-producing connection resets an exhausted retry budget.
        return SessionResult(255, "remote_exit", 1 if attempts[0] == 8 else 0,
                             61 if attempts[0] == 8 else 0.1)

    with RotatingCapture(tmp_path / "events", 4096) as writer:
        run_service([], writer, stop, retry_seconds=2, session_runner=runner)
    assert stop.delays == [2, 4, 8, 16, 32, 60, 60, 2]


def test_stop_interrupts_long_backoff_wait(tmp_path):
    stop = threading.Event()
    health = HealthState()
    errors = []

    def runner(*args):
        return SessionResult(255, "remote_exit", 0, 0)

    def worker():
        try:
            with RotatingCapture(tmp_path / "events", 4096) as writer:
                run_service([], writer, stop, 60, health=health, session_runner=runner)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 3
    while health.data["state"] != "retrying" and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert not errors


def test_oversized_raw_event_is_dropped_marked_and_does_not_break_next_record(tmp_path):
    path = tmp_path / "events"
    command = [sys.executable, "-c", "import sys;sys.stdout.write('x'*1100000+'\\n{\"good\":1}\\n');sys.stdout.flush()"]
    with RotatingCapture(path, 2 * 1024 * 1024, 0o640) as writer:
        result = run_session(command, writer, threading.Event(), 2, HealthState())
    data = path.read_bytes()
    assert result.exit_code == 0
    assert b"!bridge_oversized_event\n" in data
    assert b'{"good":1}\n' in data
    assert b"x" * 100 not in data
    assert len(data) < 1024


def test_new_service_and_shared_helpers_parse_as_python310():
    root = Path(__file__).resolve().parents[1]
    for name in ("collect_service.py", "collect_ssh.py"):
        ast.parse((root / "scripts" / name).read_text(), feature_version=(3, 10))
