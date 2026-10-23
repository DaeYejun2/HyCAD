#!/usr/bin/env python3
"""Prepare or enable a login-time macOS SSH tunnel for the private dashboard.

Authorize the printed public key for ONLY the remote loopback Grafana port.
This script never copies or stores account passwords.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = "io.hycad.dashboard-tunnel"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH user@host")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--local-port", type=int, default=3000)
    parser.add_argument("--known-hosts", type=Path, required=True,
                        help="Previously verified host public key file")
    parser.add_argument("--enable", action="store_true",
                        help="Enable after authorizing the public key and freeing the local port")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer is for macOS; server services use systemd")
    if args.host.startswith("-") or not args.host or any(c.isspace() for c in args.host):
        parser.error("host must be an SSH user@host without options or whitespace")
    if not all(1 <= port <= 65535 for port in (args.port, args.local_port)):
        parser.error("ports must be in 1..65535")
    if not args.known_hosts.is_file() or args.known_hosts.stat().st_size == 0:
        parser.error("A nonempty verified known-hosts file is required")
    home = Path.home()
    config = home / "Library/Application Support/HyCAD"
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.chmod(0o700)
    key = config / "dashboard_ed25519"
    known = config / "known_hosts"
    if args.known_hosts.resolve() != known.resolve():
        shutil.copyfile(args.known_hosts, known)
    known.chmod(0o600)
    if not key.exists():
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-C", "hycad-dashboard-only", "-f", str(key)], check=True)
    key.chmod(0o600)
    agent = home / "Library/LaunchAgents" / (LABEL + ".plist")
    agent.parent.mkdir(parents=True, exist_ok=True)
    spec = {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/ssh", "-F", "/dev/null", "-N", "-T", "-p", str(args.port),
            "-o", 'IdentityFile="' + str(key) + '"',
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none", "-o", "PasswordAuthentication=no",
            "-o", "StrictHostKeyChecking=yes", "-o", 'UserKnownHostsFile="' + str(known) + '"',
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{args.local_port}:127.0.0.1:3000", args.host,
        ],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
        "ProcessType": "Background", "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    with agent.open("wb") as handle:
        plistlib.dump(spec, handle)
    agent.chmod(0o600)
    subprocess.run(["/usr/bin/plutil", "-lint", str(agent)], check=True)
    print("Public key to restrict to remote Grafana forwarding:")
    print(key.with_suffix(".pub").read_text().strip())
    print("LaunchAgent:", agent)
    if args.enable:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["/bin/launchctl", "bootout", domain + "/" + LABEL],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(agent)], check=True)
        print(f"Enabled. Dashboard: http://127.0.0.1:{args.local_port}")
    else:
        print("Prepared only. Authorize the key, then rerun with --enable.")


if __name__ == "__main__":
    main()
