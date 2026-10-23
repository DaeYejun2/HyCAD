#!/usr/bin/env python3
"""Renew the public certificate, then validate and reload only the HTTPS gateway."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess

SNAP_DOCKER_HOST = "unix:///var/snap/docker/common/hycad/docker.sock"


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or any(char in value for char in "\r\n\x00"):
        raise ValueError(f"Missing or invalid {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="ACME staging renewal; do not reload nginx")
    args = parser.parse_args()
    if required("DOCKER_HOST") != SNAP_DOCKER_HOST:
        raise ValueError("This service requires the dedicated Snap Docker socket")
    image = required("HYCAD_CERTBOT_IMAGE")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[a-f0-9]{64}", image):
        raise ValueError("HYCAD_CERTBOT_IMAGE must include a verified sha256 digest")
    cert_name = required("HYCAD_CERT_NAME")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", cert_name):
        raise ValueError("Invalid certificate lineage name")
    root = Path(required("HYCAD_HTTPS_DIR"))
    if not root.is_absolute() or "," in str(root):
        raise ValueError("HYCAD_HTTPS_DIR must be an absolute path without commas")
    mounts = {"letsencrypt": "/etc/letsencrypt", "www": "/var/www/certbot",
              "certbot-work": "/var/lib/letsencrypt", "certbot-logs": "/var/log/letsencrypt"}
    docker = ["/usr/bin/docker", "--host", SNAP_DOCKER_HOST]
    command = docker + ["run", "--rm"]
    for directory, target in mounts.items():
        source = root / directory
        if not source.is_dir():
            raise ValueError(f"Missing HTTPS directory: {source}")
        command += ["--mount", f"type=bind,src={source},dst={target}"]
    command += [image, "renew", "--non-interactive", "--no-random-sleep-on-renew",
                "--cert-name", cert_name, "--webroot", "--webroot-path", "/var/www/certbot",
                "--preferred-challenges", "http", "--preferred-profile", "shortlived"]
    if args.dry_run:
        command.append("--dry-run")
    # check=True prevents a failed renewal from being reported as successful.
    subprocess.run(command, check=True)
    if args.dry_run:
        return
    repo = Path(__file__).resolve().parents[1]
    compose = docker + ["compose", "--project-directory", str(repo),
                        "--env-file", "/etc/hycad/ingress.env",
                        "-f", str(repo / "compose.ingress.yaml")]
    subprocess.run(compose + ["exec", "-T", "gateway", "nginx", "-t"], check=True)
    subprocess.run(compose + ["exec", "-T", "gateway", "nginx", "-s", "reload"], check=True)


if __name__ == "__main__":
    main()
