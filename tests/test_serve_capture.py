import ast
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest

from deployment.capture.serve_capture import (
    CAPTURE_COMMAND, CAPTURE_FILTER, CAPTURE_NAMES, SYSDIG_FIELDS,
)
from scripts.collect_ssh import SYSDIG_FIELDS as CLIENT_FIELDS

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "deployment/capture/serve_capture.py"


def start_supervisor(child_code, extra_setup="", grace=0.2):
    command = [sys.executable, "-c", child_code]
    program = ("import importlib.util;"
               f"spec=importlib.util.spec_from_file_location('capture_supervisor',{str(MODULE)!r});"
               "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);"
               + extra_setup
               + f"raise SystemExit(module.serve_capture(command={command!r},termination_grace={grace},poll_interval=0.02))")
    return subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)


def wait_file(path, supervisor):
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        if supervisor.poll() is not None:
            pytest.fail("Supervisor exited before capture started: " + supervisor.stderr.read().decode())
        time.sleep(0.01)
    assert path.exists(), "Capture child did not start"
    return path.read_text()


def cleanup(supervisor, child_pid=None):
    if child_pid:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if supervisor.poll() is None:
        supervisor.kill()
        supervisor.wait(timeout=3)
    for pipe in (supervisor.stdout, supervisor.stderr):
        if pipe and not pipe.closed:
            pipe.close()


def assert_child_reaped(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("ignore_term", [False, True])
def test_closing_reader_stops_and_reaps_capture_even_without_one_output_byte(tmp_path, ignore_term):
    pidfile = tmp_path / "capture.pid"
    child_code = ("import os,time,signal;from pathlib import Path;"
                  + ("signal.signal(signal.SIGTERM,signal.SIG_IGN);" if ignore_term else "")
                  + f"Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)")
    supervisor = start_supervisor(child_code)
    child_pid = None
    try:
        child_pid = int(wait_file(pidfile, supervisor))
        # The wrapper emits no heartbeat or metadata into an idle event stream.
        assert select.select([supervisor.stdout], [], [], 0.05)[0] == []
        supervisor.stdout.close()
        assert supervisor.wait(timeout=3) == 0
        assert_child_reaped(child_pid)
    finally:
        cleanup(supervisor, child_pid)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_wrapper_signals_stop_owned_capture_without_strays(tmp_path, signum):
    pidfile = tmp_path / "capture.pid"
    child_code = f"import os,time;from pathlib import Path;Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)"
    supervisor = start_supervisor(child_code)
    child_pid = None
    try:
        child_pid = int(wait_file(pidfile, supervisor))
        supervisor.send_signal(signum)
        assert supervisor.wait(timeout=3) == 128 + signum
        assert_child_reaped(child_pid)
    finally:
        cleanup(supervisor, child_pid)


def test_flock_descriptor_is_not_inherited_by_capture(tmp_path):
    resultfile = tmp_path / "descriptors.json"
    lockfile = tmp_path / "capture.lock"
    extra = ("import os,fcntl;"
             f"lock=os.open({str(lockfile)!r},os.O_CREAT|os.O_RDWR,0o600);"
             "fcntl.flock(lock,fcntl.LOCK_EX);os.dup2(lock,128);os.set_inheritable(128,True);")
    child_code = ("import os,json;from pathlib import Path\n"
                  "try:\n os.fstat(128);inherited=True\n"
                  "except OSError:\n inherited=False\n"
                  f"Path({str(resultfile)!r}).write_text(json.dumps({{'lock_inherited':inherited}}))\n")
    supervisor = start_supervisor(child_code, extra_setup=extra)
    try:
        assert supervisor.wait(timeout=3) == 0
        assert json.loads(resultfile.read_text())["lock_inherited"] is False
    finally:
        cleanup(supervisor)


def test_child_stream_and_exit_status_pass_through_without_wrapper_bytes():
    supervisor = start_supervisor("import sys;sys.stdout.write('{\"evt.type\":\"read\"}\\n');sys.stdout.flush();sys.exit(7)")
    try:
        stdout, stderr = supervisor.communicate(timeout=3)
        assert supervisor.returncode == 7
        assert stdout == b'{"evt.type":"read"}\n'
        assert stderr == b""
    finally:
        cleanup(supervisor)


def test_production_command_is_fixed_and_cli_arguments_are_rejected():
    assert tuple(f for f in SYSDIG_FIELDS if f in CLIENT_FIELDS) == tuple(CLIENT_FIELDS)
    assert SYSDIG_FIELDS[-1] == "proc.args"
    assert {"fd.rip", "fd.rport", "proc.pid", "evt.dir", "evt.rawres"} <= set(SYSDIG_FIELDS)
    assert CAPTURE_COMMAND == ("/usr/bin/sysdig", "-j", "-p", "*" + " ".join("%" + f for f in SYSDIG_FIELDS),
                               CAPTURE_FILTER)
    result = subprocess.run([sys.executable, str(MODULE), "--prefix", "other"], capture_output=True, timeout=3)
    assert result.returncode == 64
    assert result.stdout == b""
    assert b"accepts no arguments" in result.stderr
    ast.parse(MODULE.read_text(), feature_version=(3, 10))


def test_capture_scope_is_exactly_catalogue_and_frontend_four_replicas():
    expected = {
        "docker-compose_catalogue_1", "docker-compose_catalogue_2",
        "docker-compose_catalogue_3", "docker-compose_catalogue_4",
        "docker-compose_front-end_1", "docker-compose_front-end_2",
        "docker-compose_front-end_3", "docker-compose_front-end_4",
    }
    assert len(CAPTURE_NAMES) == 8
    assert set(CAPTURE_NAMES) == expected
    # Check the emitted Sysdig filter contract, not just the Python name list.
    assert CAPTURE_FILTER.startswith("(") and CAPTURE_FILTER.endswith(")")
    predicates = CAPTURE_FILTER[1:-1].split(" or ")
    assert len(predicates) == 8
    assert set(predicates) == {"container.name=" + name for name in expected}
    for excluded in ("docker-compose_catalogue-db_1", "docker-compose_catalogue_10",
                     "docker-compose_front-end_5", "docker-compose_front-end_1_helper"):
        assert "container.name=" + excluded not in predicates
