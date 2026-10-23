#!/usr/bin/env bash
# Apply an already prepared Ubuntu server configuration; no package/TSDB reset.
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
bash "$root_dir/Grafana/stack.sh" config
bash "$root_dir/Grafana/stack.sh" build
exec bash "$root_dir/Grafana/stack.sh" start
