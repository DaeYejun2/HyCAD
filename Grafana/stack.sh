#!/usr/bin/env bash
# Existing Ubuntu installation only. Never delete volumes or process-name kill.
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
case "$(uname -s)" in Linux) ;; *) echo 'Run this server entry point on Ubuntu/Linux.' >&2; exit 2;; esac
config_file="${HYCAD_ENV_FILE:-/etc/hycad/monitor.env}"
docker_socket="${HYCAD_DOCKER_HOST:-unix:///var/snap/docker/common/hycad/docker.sock}"
[[ -r "$config_file" ]] || { echo 'Prepare the protected server environment file first.' >&2; exit 2; }
compose=(docker --host "$docker_socket" compose --project-directory "$root_dir" --env-file "$config_file" -f "$root_dir/compose.yaml" -f "$root_dir/compose.server.yaml" -f "$root_dir/compose.public.yaml")
case "${1:-status}" in
  config) exec "${compose[@]}" config --quiet ;;
  build) exec "${compose[@]}" build detector ;;
  start) exec "${compose[@]}" up --detach --no-build ;;
  stop) exec "${compose[@]}" stop --timeout 30 ;;
  status) exec "${compose[@]}" ps ;;
  logs) exec "${compose[@]}" logs --tail=100 ;;
  *) echo 'Usage: stack.sh {config|build|start|stop|status|logs}' >&2; exit 2 ;;
esac
