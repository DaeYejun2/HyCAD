#!/usr/bin/env python3
"""Supervise fixed Sysdig capture and stop it when the SSH stdout reader closes.

Install this file root-owned as /usr/local/libexec/hycad-serve-capture.py.
Production accepts no command-line arguments. No heartbeat bytes are written to
stdout: only the Sysdig child's unmodified event stream reaches the collector.
"""
from __future__ import annotations

import math
import os
import select
import signal
import subprocess
import sys
import threading

SYSDIG_FIELDS = (
    "evt.rawtime", "container.name", "evt.type", "evt.category",
    "fd.directory", "fd.filename", "fd.type", "proc.name",
    "proc.cmdline", "proc.cwd", "proc.exe",
    # Investigation-only fields; excluded from the model feature mapping.
    "evt.num", "evt.dir", "evt.rawres", "proc.pid", "proc.ppid", "container.id",
    "fd.sip", "fd.sport", "fd.cip", "fd.cport", "fd.lip", "fd.lport",
    "fd.rip", "fd.rport", "fd.l4proto",
    # Sysdig 0.41.4 emits an empty JSON key if proc.args is not last.
    "proc.args",
)
# One stream serves two independent detectors. Exact names deliberately exclude
# catalogue-db, extra replicas and similarly prefixed services. Changing the
# observed deployment requires editing this root-owned allowlist, not SSH input.
CAPTURE_NAMES = tuple(
    f"docker-compose_{service}_{replica}"
    for service in ("catalogue", "front-end")
    for replica in range(1, 5)
)
CAPTURE_FILTER = "(" + " or ".join(
    "container.name=" + name for name in CAPTURE_NAMES
) + ")"
CAPTURE_COMMAND = (
    "/usr/bin/sysdig", "-j", "-p", "*" + " ".join("%" + field for field in SYSDIG_FIELDS),
    CAPTURE_FILTER,
)


def _stop_owned_capture(process: subprocess.Popen, grace_seconds: float) -> None:
    """Stop only the new session created for this supervisor's capture child."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    # Do not leave descendants behind if the process-group leader exited first.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def serve_capture(command=None, termination_grace: float = 5.0, poll_interval: float = 0.2) -> int:
    """Run one capture; command injection exists only for local Python tests.

    The installed CLI always passes no parameters and uses CAPTURE_COMMAND.
    Watching the pipe's write end detects reader loss even if Sysdig is idle.
    """
    if (not math.isfinite(termination_grace) or termination_grace < 0
            or not math.isfinite(poll_interval) or poll_interval <= 0):
        raise ValueError("Termination grace must be nonnegative and poll interval positive")
    capture_command = list(CAPTURE_COMMAND if command is None else command)
    if not capture_command:
        raise ValueError("Capture command is empty")
    output_fd = sys.stdout.fileno()
    poller = select.poll()
    closed_flags = select.POLLERR | select.POLLHUP | select.POLLNVAL
    poller.register(output_fd, closed_flags)
    stop = threading.Event()
    received_signal = [None]
    handled_signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    old_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}

    def request_stop(signum, _frame):
        received_signal[0] = signum
        stop.set()

    process = None
    pipe_closed = False
    try:
        for signum in handled_signals:
            signal.signal(signum, request_stop)
        # The initial nonblocking poll avoids launching a capture for a client
        # that has already disconnected while sudo/flock started this wrapper.
        if any(flags & closed_flags for _, flags in poller.poll(0)):
            return 0
        process = subprocess.Popen(capture_command, stdin=subprocess.DEVNULL,
                                   stdout=output_fd, stderr=None, close_fds=True,
                                   start_new_session=True, restore_signals=True)
        while not stop.is_set():
            code = process.poll()
            if code is not None:
                return code if code >= 0 else 128 - code
            if any(flags & closed_flags for _, flags in poller.poll(max(1, math.ceil(poll_interval * 1000)))):
                pipe_closed = True
                break
        if pipe_closed:
            return 0
        return 128 + int(received_signal[0] or signal.SIGTERM)
    finally:
        try:
            if process is not None:
                _stop_owned_capture(process, termination_grace)
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
            poller.unregister(output_fd)


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("This capture command accepts no arguments.", file=sys.stderr)
        return 64
    try:
        return serve_capture()
    except OSError as exc:
        print(f"Capture supervisor failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
