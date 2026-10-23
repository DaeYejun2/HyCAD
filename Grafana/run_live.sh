#!/usr/bin/env bash
# marine-normal 에서 sysdig -> HyCAD 판정 -> :9900/metrics 를 띄운다.
#
#   bash run_live.sh check     지금 뜬 컨테이너 이름이 기준선과 맞는지 5초만 떠서 확인
#   bash run_live.sh fg        포그라운드 실행 (systemd 가 이걸 쓴다)
#   bash run_live.sh start     nohup 백그라운드 (수동 실행용)
#   bash run_live.sh stop      sysdig 에 SIGINT (sysdig 는 SIGINT 만 받는다)
#   bash run_live.sh tail      판정 로그 보기
#
# 주의 — 전부 실제로 겪은 것 (CLAUDE.md "서버 작업 시 함정"):
#   * sysdig 는 SIGINT 만 받는다. systemd 유닛도 KillSignal=SIGINT 로 둔다
#   * 포그라운드로 두면 CPU 포화 때 Ctrl+C 가 안 먹는다. 수동 실행은 nohup
#   * sudo 아래에서 $HOME 이 /root 가 된다. 경로를 하드코딩할 것
set -uo pipefail

BASE=${BASE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
LOG=${LOG:-$BASE/live.log}
PORT=${PORT:-9900}
BASELINE=${BASELINE:-cat_baseline}
FILTER=${FILTER:-"container.name contains docker-compose_catalogue_"}
FMT='*%evt.rawtime %container.name %evt.type %evt.category %fd.directory %fd.filename %fd.type %proc.name %proc.cmdline %proc.cwd %proc.exe %proc.args'

need() { command -v "$1" >/dev/null || { echo "없음: $1"; exit 1; }; }

check() {
  need sysdig; need python3
  echo "### 5초만 떠서 컨테이너 이름을 본다"
  sudo -n timeout -s INT 5 sysdig -j -p'*%container.name' "$FILTER" 2>/dev/null \
    | python3 -c '
import sys, json, collections
c = collections.Counter()
for line in sys.stdin:
    try: n = json.loads(line).get("container.name")
    except Exception: continue
    if n: c[n] += 1
if not c:
    print("  이벤트 0건. 필터가 안 맞거나 컨테이너가 안 떠 있다."); raise SystemExit(1)
for k, v in sorted(c.items()):
    print(f"  {k:40} {v:>8,} 이벤트")
print(f"\n  replica {len(c)}대 관측")
'
  echo
  echo "### 기준선이 기대하는 이름"
  PYTHONIOENCODING=utf-8 python3 - <<PYEOF
import sys
sys.path.insert(0, "$BASE/ucbadx")
from evaluate import load, deploy_vocab
B = load("$BASELINE", deploy_vocab("catalogue"))
for r in B["reps"]:
    print(f"  {r}")
print(f"\n  위 두 목록이 다르면 docker-compose 프로젝트 이름이 바뀐 것이다.")
print(f"  FILTER 환경변수로 접두어를 맞추거나 정상 구간을 다시 떠서 기준선을 갱신할 것.")
PYEOF
}

pipeline() {
  sudo -n sysdig -j -p"$FMT" "$FILTER" \
    | PYTHONIOENCODING=utf-8 python3 -u "$BASE/hycad_live.py" live \
        --port "$PORT" --baseline "$BASELINE"
}

case "${1:-fg}" in
  check) check ;;
  fg)    need sysdig; need python3; pipeline ;;
  start)
    [ -f "$BASE/hycad_live.py" ] || { echo "없음: $BASE/hycad_live.py"; exit 1; }
    sudo -v || exit 1
    pgrep -f "hycad_live.py live" >/dev/null && { echo "이미 돌고 있다"; exit 1; }
    nohup bash "${BASH_SOURCE[0]}" fg >>"$LOG" 2>&1 &
    echo "시작. 로그 $LOG"
    echo "메트릭 http://$(hostname -I | awk '{print $1}'):$PORT/metrics"
    echo "첫 판정까지 30초(스냅샷 1개) 걸린다."
    ;;
  stop)
    sudo pkill -INT -f "sysdig -j" && echo "sysdig 에 SIGINT"
    sleep 2
    pkill -f "hycad_live.py live" && echo "판정기 종료"
    ;;
  tail) tail -f "$LOG" ;;
  *) echo "usage: $0 {check|fg|start|stop|tail}"; exit 1 ;;
esac
