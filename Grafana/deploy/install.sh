#!/usr/bin/env bash
# HyCAD 대시보드를 marine-normal(Ubuntu)에 설치한다.
#
#   GRAFANA_ADMIN_PASSWORD='긴비밀번호' bash install.sh
#
# 하는 일
#   1. 사전 점검 (python3 / numpy / sysdig / 포트 충돌)
#   2. Prometheus · Grafana 리눅스 tarball 내려받아 $BASE 에 펼침
#   3. 캡처를 TSDB 블록으로 구움 (백필)
#   4. Grafana 데이터소스·대시보드 3개 프로비저닝
#   5. systemd 유닛 4개 등록
#
# 되돌리기
#   bash install.sh uninstall
set -euo pipefail

BASE=${BASE:-$HOME/hycad}
PROM_VER=${PROM_VER:-3.15.1}
GRAF_VER=${GRAF_VER:-11.6.0}
GRAFANA_PORT=${GRAFANA_PORT:-3000}
GRAFANA_BIND=${GRAFANA_BIND:-0.0.0.0}      # 외부에서 봐야 하므로 기본이 0.0.0.0
ANON=${ANON:-true}                          # 로그인 없이 열람 허용
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\033[31m!! %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 제거
if [ "${1:-install}" = "uninstall" ]; then
  say "systemd 유닛 제거"
  for u in hycad-live hycad-demo hycad-grafana hycad-prometheus; do
    sudo systemctl disable --now "$u" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$u.service"
  done
  sudo systemctl daemon-reload
  echo "유닛을 지웠다. 파일은 그대로다: $BASE"
  echo "전부 지우려면: rm -rf $BASE"
  exit 0
fi

# ---------------------------------------------------------------- 1. 점검
say "1. 사전 점검"
[ -n "${GRAFANA_ADMIN_PASSWORD:-}" ] || die \
  "관리자 비밀번호를 정할 것:  GRAFANA_ADMIN_PASSWORD='...' bash install.sh"
[ "${#GRAFANA_ADMIN_PASSWORD}" -ge 12 ] || die "비밀번호는 12자 이상으로"

for c in curl tar python3 systemctl; do
  command -v "$c" >/dev/null || die "없음: $c"
done
echo "  필수 명령 OK"

HAVE_NUMPY=0
python3 -c 'import numpy' 2>/dev/null && HAVE_NUMPY=1
if [ $HAVE_NUMPY -eq 1 ]; then
  echo "  numpy OK  (배속 데모·실시간 판정 가능)"
else
  echo "  numpy 없음 -> 백필 화면만 설치한다"
  echo "              나중에: sudo apt install -y python3-numpy  후 install.sh 재실행"
fi

HAVE_SYSDIG=0
command -v sysdig >/dev/null && HAVE_SYSDIG=1
[ $HAVE_SYSDIG -eq 1 ] && echo "  sysdig OK  (실시간 감시 가능)" \
                       || echo "  sysdig 없음 -> 실시간 감시는 건너뛴다"

for p in "$GRAFANA_PORT" 9090 9900 9901; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$p\$"; then
    die "포트 $p 를 이미 누가 쓰고 있다. GRAFANA_PORT 를 바꾸거나 그 프로세스를 정리할 것"
  fi
done
echo "  포트 $GRAFANA_PORT / 9090 / 9900 / 9901 비어 있음"

ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  GOARCH=amd64 ;;
  aarch64) GOARCH=arm64 ;;
  *) die "지원 안 하는 아키텍처: $ARCH" ;;
esac
echo "  아키텍처 $ARCH -> linux-$GOARCH"

# ---------------------------------------------------------------- 2. 설치
say "2. 파일 배치"
mkdir -p "$BASE"/{dl,data,dashboards}
cp -r "$SRC/app/." "$BASE/"
cp "$SRC/prometheus.yml" "$BASE/prometheus.yml"
mv -f "$BASE/dashboards"/*.json "$BASE/dashboards/" 2>/dev/null || true
chmod +x "$BASE/run_live.sh"
echo "  $BASE 에 코드·캐시·대시보드 배치"

fetch() {  # url  out
  [ -s "$2" ] && { echo "  이미 있음: $(basename "$2")"; return; }
  echo "  받는 중: $(basename "$2")"
  curl -fL --progress-bar -o "$2" "$1"
}

PROM_TGZ="$BASE/dl/prometheus-$PROM_VER.linux-$GOARCH.tar.gz"
GRAF_TGZ="$BASE/dl/grafana-$GRAF_VER.linux-$GOARCH.tar.gz"
fetch "https://github.com/prometheus/prometheus/releases/download/v$PROM_VER/prometheus-$PROM_VER.linux-$GOARCH.tar.gz" "$PROM_TGZ"
fetch "https://dl.grafana.com/oss/release/grafana-$GRAF_VER.linux-$GOARCH.tar.gz" "$GRAF_TGZ"

rm -rf "$BASE/prometheus" "$BASE/grafana"
mkdir -p "$BASE/prometheus" "$BASE/grafana"
tar xzf "$PROM_TGZ" -C "$BASE/prometheus" --strip-components=1
tar xzf "$GRAF_TGZ" -C "$BASE/grafana" --strip-components=1
echo "  Prometheus $PROM_VER · Grafana $GRAF_VER 펼침"

# ---------------------------------------------------------------- 3. 백필
say "3. 캡처를 TSDB 블록으로"
OM="$BASE/openmetrics.txt"
if [ $HAVE_NUMPY -eq 1 ]; then
  echo "  지금 시각을 끝으로 다시 생성한다"
  (cd "$BASE" && PYTHONIOENCODING=utf-8 python3 backfill.py --out "$OM" | tail -16)
else
  echo "  numpy 가 없어 꾸러미에 든 것을 그대로 쓴다 (시각이 만든 날에 맞춰져 있다)"
fi
rm -rf "$BASE/data"; mkdir -p "$BASE/data"
"$BASE/prometheus/promtool" tsdb create-blocks-from openmetrics "$OM" "$BASE/data" | tail -5

# ---------------------------------------------------------------- 4. Grafana
say "4. Grafana 설정"
PROV="$BASE/grafana/conf/provisioning"
mkdir -p "$PROV/datasources" "$PROV/dashboards"

cat > "$PROV/datasources/hycad.yaml" <<EOF
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: hycad-prom
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
    jsonData:
      timeInterval: 30s
EOF

cat > "$PROV/dashboards/hycad.yaml" <<EOF
apiVersion: 1
providers:
  - name: HyCAD
    type: file
    allowUiUpdates: true
    options:
      path: $BASE/dashboards
EOF

cat > "$BASE/grafana/conf/custom.ini" <<EOF
[server]
http_addr = $GRAFANA_BIND
http_port = $GRAFANA_PORT

[security]
admin_user = admin
admin_password = $GRAFANA_ADMIN_PASSWORD
cookie_secure = false

[auth.anonymous]
enabled = $ANON
org_role = Viewer

[users]
allow_sign_up = false

[analytics]
reporting_enabled = false
check_for_updates = false

[news]
news_feed_enabled = false
EOF
chmod 600 "$BASE/grafana/conf/custom.ini"
echo "  custom.ini 작성 (권한 600). 바인딩 $GRAFANA_BIND:$GRAFANA_PORT · 익명 열람 $ANON"

# ---------------------------------------------------------------- 5. systemd
say "5. systemd 유닛"
unit() { sudo tee "/etc/systemd/system/$1.service" >/dev/null; }

unit hycad-prometheus <<EOF
[Unit]
Description=HyCAD Prometheus
After=network-online.target

[Service]
User=$USER_NAME
ExecStart=$BASE/prometheus/prometheus --config.file=$BASE/prometheus.yml \\
  --storage.tsdb.path=$BASE/data --web.listen-address=127.0.0.1:9090 \\
  --storage.tsdb.retention.time=1y
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

unit hycad-grafana <<EOF
[Unit]
Description=HyCAD Grafana
After=network-online.target hycad-prometheus.service

[Service]
User=$USER_NAME
WorkingDirectory=$BASE/grafana
ExecStart=$BASE/grafana/bin/grafana-server --homepath $BASE/grafana \\
  --config $BASE/grafana/conf/custom.ini
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

if [ $HAVE_NUMPY -eq 1 ]; then
unit hycad-demo <<EOF
[Unit]
Description=HyCAD 배속 데모 (공격 3종 + 정상 드리프트)
After=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$BASE
Environment=PYTHONIOENCODING=utf-8
ExecStart=/usr/bin/python3 -u $BASE/hycad_live.py demo \\
  --interval 1 --limit 10 --loop --port 9901
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
fi

if [ $HAVE_NUMPY -eq 1 ] && [ $HAVE_SYSDIG -eq 1 ]; then
# sysdig 는 root 가 필요하고 SIGINT 만 받는다. 파이프는 run_live.sh 안에 둔다
# (포맷 문자열의 % 를 systemd 가 지시자로 먹기 때문).
unit hycad-live <<EOF
[Unit]
Description=HyCAD 실시간 감시 (sysdig -> 판정)
After=network-online.target docker.service

[Service]
User=root
WorkingDirectory=$BASE
Environment=BASE=$BASE
ExecStart=/bin/bash $BASE/run_live.sh fg
KillSignal=SIGINT
TimeoutStopSec=20
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
fi

sudo systemctl daemon-reload
sudo systemctl enable --now hycad-prometheus hycad-grafana
[ $HAVE_NUMPY -eq 1 ] && sudo systemctl enable --now hycad-demo
echo "  실시간 감시는 수동으로 켠다:  sudo systemctl start hycad-live"

# ---------------------------------------------------------------- 마무리
say "설치 끝"
sleep 5
systemctl --no-pager --lines=0 status hycad-prometheus hycad-grafana 2>/dev/null \
  | grep -E "●|Active:" || true

IP=$(hostname -I | awk '{print $1}')
cat <<EOF

  대시보드  http://$IP:$GRAFANA_PORT/d/hycad-live       백필된 실험 기록 5.1시간
            http://$IP:$GRAFANA_PORT/d/hycad-demo       배속 데모 (한 바퀴 2분 39초)
            http://$IP:$GRAFANA_PORT/d/hycad-realtime   실시간 (hycad-live 서비스 켜야 뜸)

  다음에 할 것
    1) 실시간 감시 전에 컨테이너 이름부터 확인
         bash $BASE/run_live.sh check
    2) 맞으면 켜기
         sudo systemctl start hycad-live
         sudo journalctl -u hycad-live -f
    3) 밖에서 접속하려면 포트 $GRAFANA_PORT 를 열거나 Cloudflare Tunnel 을 쓸 것
         (9090 · 9900 · 9901 은 절대 열지 말 것 — Grafana 가 대신 조회한다)

  상태   systemctl status hycad-prometheus hycad-grafana hycad-demo hycad-live
  정지   sudo systemctl stop hycad-live hycad-demo
  제거   bash $SRC/install.sh uninstall
EOF
