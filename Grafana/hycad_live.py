#!/usr/bin/env python3
"""Ubuntu entry points backed by the production HyCAD monitor.

The research controller is imported by hycad_monitor, never copied here.
Use live --help, replay --help or manual --help for the current contracts.
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MODES = {"live": "hycad_monitor", "replay": "hycad_monitor.capture_replay",
         "manual": "hycad_monitor.replay_control"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in MODES:
        print("Usage: hycad_live.py {live|replay|manual} [arguments]", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    os.chdir(ROOT)
    command = [sys.executable, "-m", MODES[mode]]
    if mode == "live":
        command.extend(["--mode", "live"])
    os.execv(sys.executable, command + sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
