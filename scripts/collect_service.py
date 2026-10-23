#!/usr/bin/env python3
"""Continuously collect a fixed remote capture through a restricted SSH key.

Requires Python 3.10+ and OpenSSH. The remote authorized_keys entry must force
the root-owned capture wrapper; this client requests only ``hycad-capture``.
The supervisor runs continuously, reconnects after loss, and never reads a
password. A system service manager is responsible for restarting after reboot.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

if __package__:
    from .collect_ssh import EventLines, FLUSH_SECONDS, RotatingCapture
else:
    from collect_ssh import EventLines, FLUSH_SECONDS, RotatingCapture

LOG = logging.getLogger("hycad.collector")
MAX_RETRY_SECONDS = 60.0
STABLE_SESSION_SECONDS = 60.0


def _ssh_config_path(path: Path) -> str:
    value = str(path)
    if any(ord(char) < 32 for char in value):
        raise ValueError("SSH file paths must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_service_command(host: str, identity: Path, known_hosts: Path, port: int = 22) -> list[str]:
    """Construct key-only SSH argv with strict host verification and one command."""
    if (not isinstance(host, str) or not host or host.startswith("-") or len(host) > 256
            or any(char.isspace() or ord(char) < 32 for char in host)):
        raise ValueError("host must be an SSH host or user@host without options or whitespace")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    options = [
        "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
        "IdentityFile=" + _ssh_config_path(identity),
        "PreferredAuthentications=publickey", "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no", "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=" + _ssh_config_path(known_hosts), "GlobalKnownHostsFile=/dev/null",
        "ConnectTimeout=10", "ConnectionAttempts=1", "ServerAliveInterval=10",
        "ServerAliveCountMax=3", "ControlMaster=no", "ControlPath=none",
        "ClearAllForwardings=yes", "LogLevel=ERROR",
    ]
    command = ["ssh", "-F", "/dev/null", "-T", "-n", "-p", str(port)]
    for option in options:
        command.extend(["-o", option])
    return [*command, host, "hycad-capture"]


class CollectorLock:
    """A held OS lock prevents duplicate writers; stale PID text is harmless."""

    def __init__(self, path: Path, file_mode: int = 0o640):
        self.path, self.file_mode, self.fd = Path(path), file_mode, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), self.file_mode)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Collector lock must be a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another collector holds the output lock") from exc
            os.fchmod(fd, self.file_mode)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def __exit__(self, *args):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class HealthState:
    """Atomic, bounded metadata only: no key material, credentials or raw events."""

    def __init__(self, path: Path | None = None, file_mode: int = 0o640):
        self.path = Path(path) if path else None
        self.file_mode = file_mode
        self.data = {"state": "starting", "pid": os.getpid(), "attempts": 0, "restarts": 0,
                     "bytes_received": 0, "last_data_at": None}
        self._last_write = 0.0

    def update(self, force: bool = False, **fields) -> None:
        self.data.update(fields)
        self.data["updated_at"] = time.time()
        if not self.path or (not force and time.monotonic() - self._last_write < 1):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            os.fchmod(fd, self.file_mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
                handle.flush()
            os.replace(temporary, self.path)
            self._last_write = time.monotonic()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@dataclass
class SessionResult:
    exit_code: int
    reason: str
    bytes_received: int
    elapsed: float
    last_data_at: float | None = None


def _terminate_owned_group(process: subprocess.Popen) -> None:
    """Signal only the process group created by this supervisor's Popen call."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)
    # A child could outlive an exited group leader. It belongs to our dedicated
    # session as well; do not leave it holding stdout or a capture connection.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_session(command: list[str], writer: RotatingCapture, stop: threading.Event,
                idle_seconds: float, health: HealthState) -> SessionResult:
    """Run one owned transport, stream bounded records, and mark discontinuity."""
    started = time.monotonic()
    last_byte = started
    last_data_at = None
    received = 0
    base_received = health.data.get("bytes_received", 0)
    reason = "remote_exit"
    process = None
    selector = selectors.DefaultSelector()
    lines = EventLines(writer)
    writer.write(b"!bridge_source_start\n")
    writer.flush()
    health.update(force=True, state="connecting", idle_seconds=0, next_retry_at=None)
    try:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=None, bufsize=0, start_new_session=True)
        except OSError:
            reason = "spawn_error"
        if process is not None:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            last_flush = started
            eof = False
            while not stop.is_set() and not eof:
                remaining = idle_seconds - (time.monotonic() - last_byte)
                if remaining <= 0:
                    reason = "input_idle"
                    break
                for key, _ in selector.select(min(FLUSH_SECONDS, remaining)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        reason, eof = "pipe_error", True
                        break
                    if not chunk:
                        eof = True
                        break
                    first_data = received == 0
                    received += len(chunk)
                    last_byte, last_data_at = time.monotonic(), time.time()
                    lines.feed(chunk)
                    health.update(force=first_data, state="streaming", last_data_at=last_data_at,
                                  bytes_received=base_received + received, idle_seconds=0)
                if time.monotonic() - last_flush >= FLUSH_SECONDS:
                    writer.flush()
                    last_flush = time.monotonic()
                health.update(idle_seconds=max(0.0, time.monotonic() - last_byte))
            if stop.is_set():
                reason = "stopped"
            elif eof:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        selector.close()
        if process is not None:
            _terminate_owned_group(process)
            process.stdout.close()
        lines.finish()  # Partial records cannot cross SSH session boundaries.
    return SessionResult(process.returncode if process is not None else 255, reason, received,
                         time.monotonic() - started, last_data_at)


def run_service(command: list[str], writer: RotatingCapture, stop: threading.Event,
                retry_seconds: float = 2, idle_seconds: float = 90,
                health: HealthState | None = None, session_runner=None) -> None:
    """Reconnect indefinitely with interruptible exponential backoff up to 60s."""
    if not math.isfinite(retry_seconds) or not 0 < retry_seconds <= MAX_RETRY_SECONDS:
        raise ValueError("retry-seconds must be positive and at most 60")
    if not math.isfinite(idle_seconds) or idle_seconds <= 0:
        raise ValueError("idle-seconds must be positive")
    health = health if health is not None else HealthState()
    runner = session_runner or run_session
    delay = retry_seconds
    attempts = 0
    total_received = 0
    failed = False
    try:
        while not stop.is_set():
            attempts += 1
            health.update(force=True, state="connecting", attempts=attempts, restarts=attempts - 1,
                          bytes_received=total_received, next_retry_at=None)
            session = runner(command, writer, stop, idle_seconds, health)
            total_received += session.bytes_received
            if session.last_data_at is not None:
                health.update(last_data_at=session.last_data_at)
            health.update(bytes_received=total_received, last_exit_code=session.exit_code,
                          last_disconnect_reason=session.reason)
            if stop.is_set():
                break
            if session.elapsed >= STABLE_SESSION_SECONDS and session.bytes_received > 0:
                delay = retry_seconds
            health.update(force=True, state="retrying", retry_seconds=delay, next_retry_at=time.time() + delay)
            LOG.warning("Capture disconnected: reason=%s status=%s bytes=%s; reconnecting in %.1fs",
                        session.reason, session.exit_code, session.bytes_received, delay)
            if stop.wait(delay):
                break
            delay = min(MAX_RETRY_SECONDS, delay * 2)
    except BaseException:
        failed = True
        health.update(force=True, state="failed", next_retry_at=None)
        raise
    finally:
        if not failed:
            health.update(force=True, state="stopped", next_retry_at=None)


def _file_mode(value: str) -> int:
    try:
        mode = int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("file-mode must be 0600 or 0640") from exc
    if mode not in (0o600, 0o640):
        raise argparse.ArgumentTypeError("file-mode must be 0600 or 0640")
    return mode


def _validate_auth_files(identity: Path, known_hosts: Path) -> None:
    if not identity.is_file() or not known_hosts.is_file():
        raise ValueError("identity and known-hosts must be existing files")
    if stat.S_IMODE(identity.stat().st_mode) & 0o077:
        raise ValueError("identity must not be accessible by group or others")
    if stat.S_IMODE(known_hosts.stat().st_mode) & 0o022:
        raise ValueError("known-hosts must not be writable by group or others")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--output", type=Path, default=Path("data/live/events.jsonl"))
    parser.add_argument("--max-mib", type=int, default=128, help="Per-file limit, 1..512 MiB; keeps one rotated file.")
    parser.add_argument("--retry-seconds", type=float, default=2, help="Initial retry delay; doubles up to 60 seconds.")
    parser.add_argument("--idle-seconds", type=float, default=90, help="Reconnect if no event bytes arrive for this long.")
    parser.add_argument("--file-mode", type=_file_mode, default=0o640, help="0600 or 0640; group comes from the service/directory.")
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--health-file", type=Path, help="Optional atomic status JSON containing metadata only.")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_mib <= 512:
            raise ValueError("max-mib must be between 1 and 512")
        if not math.isfinite(args.retry_seconds) or not 0 < args.retry_seconds <= MAX_RETRY_SECONDS:
            raise ValueError("retry-seconds must be positive and at most 60")
        if not math.isfinite(args.idle_seconds) or args.idle_seconds <= 0:
            raise ValueError("idle-seconds must be positive")
        _validate_auth_files(args.identity, args.known_hosts)
        command = build_service_command(args.host, args.identity, args.known_hosts, args.port)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    lock_path = args.lock_file or args.output.with_name(args.output.name + ".collector.lock")
    health = HealthState(args.health_file, args.file_mode)
    try:
        with CollectorLock(lock_path, args.file_mode):
            with RotatingCapture(args.output, args.max_mib * 1024 * 1024, args.file_mode) as writer:
                LOG.info("Starting continuous capture: host=%s output=%s", args.host, args.output)
                run_service(command, writer, stop, args.retry_seconds, args.idle_seconds, health)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        LOG.error("Collector stopped: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
