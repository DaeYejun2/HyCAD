#!/usr/bin/env bash
# File follower only. The supervised SSH service owns Sysdig on the target VM.
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$root_dir/Grafana/hycad_live.py" live "$@"
