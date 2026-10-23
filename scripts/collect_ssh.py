#!/usr/bin/env python3
"""Passively stream remote Sysdig events through an existing SSH control socket.

No packages or files are created on the remote host. Each capture is bounded to
one hour. sudo credentials are read interactively and sent only over SSH stdin.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time

SYSDIG_FIELDS = (
    "evt.rawtime", "container.name", "evt.type", "evt.category",
    "fd.directory", "fd.filename", "fd.type", "proc.name",
    "proc.cmdline", "proc.cwd", "proc.exe", "proc.args",
)
MAX_DURATION = 3600
MAX_LINE_BYTES = 1024 * 1024
FLUSH_SECONDS = 0.2


def build_capture_command(host: str, control: Path, prefix: str,
                          sudo: bool = False, duration: int = MAX_DURATION) -> list[str]:
    """Return inspectable SSH argv. No credentials ever enter this command."""
    if (not isinstance(host, str) or not host or host.startswith("-") or len(host) > 256
            or any(char.isspace() or ord(char) < 32 for char in host)):
        raise ValueError("host must be an SSH host or user@host without options or whitespace")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", prefix):
        raise ValueError("prefix must be 1..128 Docker name characters")
    if type(duration) is not int or not 1 <= duration <= MAX_DURATION:
        raise ValueError("duration must be between 1 and 3600 seconds")
    remote = ["timeout", "--signal=INT", "--kill-after=5s", f"{duration}s", "sysdig", "-j", "-p",
              "*" + " ".join("%" + name for name in SYSDIG_FIELDS),
              f"container.name startswith {prefix}"]
    if sudo:
        remote = ["sudo", "-S", "-p", "", "--", *remote]
    # exec removes the intermediate shell. timeout is a remote lifetime backstop
    # even if the network disappears before SSH can close the capture channel.
    remote_command = "exec " + shlex.join(remote)
    return ["ssh", "-T", "-S", str(control), "-o", "BatchMode=yes", "-o", "ControlMaster=no",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", host, remote_command]


class RotatingCapture:
    """Write complete records to output + output.1, each bounded by max_bytes."""

    def __init__(self, path: Path, max_bytes: int, file_mode: int = 0o600):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if file_mode not in (0o600, 0o640):
            raise ValueError("file_mode must be 0600 or 0640")
        self.path = Path(path)
        self.backup = self.path.with_name(self.path.name + ".1")
        self.max_bytes = max_bytes
        self.file_mode = file_mode
        self.handle = None
        self.size = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for candidate in (self.path, self.backup):
            if candidate.exists() and candidate.stat().st_size > max_bytes:
                raise ValueError(f"Existing capture exceeds configured limit: {candidate}")
        self._open()

    def _open(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, self.file_mode)
        try:
            os.fchmod(fd, self.file_mode)
            self.size = os.fstat(fd).st_size
            self.handle = os.fdopen(fd, "ab", buffering=65536)
        except BaseException:
            os.close(fd)
            raise

    def write(self, record: bytes) -> None:
        if not isinstance(record, bytes) or len(record) > self.max_bytes:
            raise ValueError("One record exceeds the capture size limit")
        if self.size + len(record) > self.max_bytes:
            self.handle.flush()
            self.handle.close()
            self.handle = None
            os.replace(self.path, self.backup)
            self._open()
        self.handle.write(record)
        self.size += len(record)

    def flush(self) -> None:
        if self.handle:
            self.handle.flush()

    def close(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class EventLines:
    """Retain partial pipe reads and mark oversized/truncated input explicitly."""

    def __init__(self, writer: RotatingCapture):
        self.writer = writer
        self.pending = b""
        self.discard = False
        self.limit = min(MAX_LINE_BYTES, writer.max_bytes)

    def feed(self, chunk: bytes) -> None:
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if self.discard or len(line) + 1 > self.limit:
                self.writer.write(b"!bridge_oversized_event\n")
                self.discard = False
            elif line.strip():
                self.writer.write(line + b"\n")
        if len(self.pending) >= self.limit:
            self.pending = b""
            self.discard = True

    def finish(self) -> None:
        if self.pending or self.discard:
            self.writer.write(b"!bridge_truncated_event\n")
        # This intentional non-event makes the local monitor invalidate the open
        # window immediately instead of scoring a prematurely ended capture.
        self.writer.write(b"!bridge_capture_stopped\n")
        self.writer.flush()
        self.pending = b""
        self.discard = False


def capture(command: list[str], writer: RotatingCapture, stop: threading.Event,
            sudo: bool, duration: int) -> int:
    secret = None
    process = None
    selector = selectors.DefaultSelector()
    lines = EventLines(writer)
    try:
        if sudo:
            if not sys.stdin.isatty():
                raise ValueError("--sudo requires an interactive terminal so password input cannot echo")
            previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                for signum in previous_handlers:
                    signal.signal(signum, signal.default_int_handler)
                secret = bytearray(getpass.getpass("Remote sudo password: ").encode("utf-8"))
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=None, bufsize=0)
        try:
            if secret is not None:
                process.stdin.write(secret + b"\n")
                process.stdin.flush()
        finally:
            process.stdin.close()
            if secret is not None:
                secret[:] = b"\0" * len(secret)
                secret = None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + duration + 15
        last_flush = time.monotonic()
        eof = False
        while not stop.is_set() and not eof:
            if time.monotonic() >= deadline:
                print("[bridge] Local session deadline reached; stopping owned SSH process.", file=sys.stderr)
                break
            for key, _ in selector.select(FLUSH_SECONDS):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    eof = True
                    break
                lines.feed(chunk)
            if time.monotonic() - last_flush >= FLUSH_SECONDS:
                writer.flush()
                last_flush = time.monotonic()
        if process.poll() is None:
            if eof and not stop.is_set():
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is None:
                process.terminate()
        try:
            code = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
        if code == 124:
            print("[bridge] Remote capture duration limit reached.", file=sys.stderr)
            return 0
        if stop.is_set():
            return 0
        if code:
            print(f"[bridge] SSH/capture exited with status {code}; no automatic reconnect.", file=sys.stderr)
        return code if code >= 0 else 1
    finally:
        if secret is not None:
            secret[:] = b"\0" * len(secret)
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout:
                process.stdout.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            lines.finish()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/live/events.jsonl"))
    parser.add_argument("--prefix", default="docker-compose_catalogue_")
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--max-mib", type=int, default=32, help="Per-file limit, 1..512 MiB; keeps one rotated file.")
    parser.add_argument("--duration", type=int, default=MAX_DURATION, help="Capture seconds, 1..3600; no unlimited mode.")
    args = parser.parse_args(argv)
    try:
        command = build_capture_command(args.host, args.control, args.prefix, args.sudo, args.duration)
        if not 1 <= args.max_mib <= 512:
            raise ValueError("max-mib must be between 1 and 512")
        if not stat.S_ISSOCK(args.control.stat().st_mode):
            raise ValueError("control must be an existing OpenSSH control socket")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    print(f"[bridge] host={args.host} prefix={args.prefix} duration={args.duration}s output={args.output}", file=sys.stderr)
    try:
        with RotatingCapture(args.output, args.max_mib * 1024 * 1024) as writer:
            return capture(command, writer, stop, args.sudo, args.duration)
    except KeyboardInterrupt:
        print("[bridge] Capture canceled.", file=sys.stderr)
        return 130
    except (OSError, ValueError, EOFError) as exc:
        print(f"[bridge] Capture failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
