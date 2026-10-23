import os
from pathlib import Path
import shlex
import stat
import sys
import threading

import pytest

from scripts.collect_ssh import (EventLines, RotatingCapture, SYSDIG_FIELDS,
                                 build_capture_command, capture)


def test_capture_command_uses_exact_fields_filter_and_bounded_timeout():
    command = build_capture_command("marine@example.test", Path("work/with space.sock"),
                                    "docker-compose_catalogue_", sudo=True, duration=120)
    assert command[command.index("-S") + 1] == "work/with space.sock"
    assert command[-2] == "marine@example.test"
    remote = shlex.split(command[-1])
    assert remote[:6] == ["exec", "sudo", "-S", "-p", "", "--"]
    assert remote[6:10] == ["timeout", "--signal=INT", "--kill-after=5s", "120s"]
    assert remote[-1] == "container.name startswith docker-compose_catalogue_"
    assert remote[-2] == "*" + " ".join("%" + name for name in SYSDIG_FIELDS)
    assert len(SYSDIG_FIELDS) == 12
    assert "BatchMode=yes" in command


@pytest.mark.parametrize("kwargs", [{"prefix": "x or container.name!=x"}, {"prefix": "x'; touch /tmp/x"},
                                    {"host": "-oProxyCommand=bad"}, {"duration": 0}, {"duration": 3601}])
def test_invalid_filter_or_unbounded_session_is_rejected(kwargs):
    options = dict(host="marine@example.test", control=Path("work/inner.sock"), prefix="catalogue_", duration=3600)
    options.update(kwargs)
    with pytest.raises(ValueError):
        build_capture_command(**options)


def test_rotation_is_atomic_bounded_and_private(tmp_path):
    path = tmp_path / "events.jsonl"
    with RotatingCapture(path, 12) as writer:
        writer.write(b"first11\n")
        writer.flush()
        first_inode = path.stat().st_ino
        writer.write(b"second2\n")
        writer.flush()
        assert path.stat().st_ino != first_inode
        assert path.with_name("events.jsonl.1").read_bytes() == b"first11\n"
        writer.write(b"third33\n")
        writer.flush()
    assert path.read_bytes() == b"third33\n"
    assert path.with_name("events.jsonl.1").read_bytes() == b"second2\n"
    assert len(list(tmp_path.iterdir())) == 2
    for candidate in tmp_path.iterdir():
        assert candidate.stat().st_size <= 12
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600


def test_existing_oversize_file_is_not_silently_destroyed(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"important old evidence")
    with pytest.raises(ValueError):
        RotatingCapture(path, 8)
    assert path.read_bytes() == b"important old evidence"


def test_split_pipe_records_and_shutdown_marker(tmp_path):
    path = tmp_path / "events.jsonl"
    with RotatingCapture(path, 1024) as writer:
        lines = EventLines(writer)
        lines.feed(b'{"a":')
        lines.feed(b'1}\n{"unfinished":')
        lines.finish()
    assert path.read_bytes() == b'{"a":1}\n!bridge_truncated_event\n!bridge_capture_stopped\n'


def test_capture_transport_streams_without_ssh_and_closes_owned_process(tmp_path):
    path = tmp_path / "events.jsonl"
    # Exercise the pipe/flush/lifecycle path with a local child, never a server.
    command = [sys.executable, "-c", "import sys; sys.stdout.write('{\"event\":1}\\n'); sys.stdout.flush()"]
    with RotatingCapture(path, 1024) as writer:
        code = capture(command, writer, threading.Event(), sudo=False, duration=1)
    assert code == 0
    assert path.read_bytes() == b'{"event":1}\n!bridge_capture_stopped\n'
