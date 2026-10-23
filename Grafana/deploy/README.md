# marine-normal 배포

로컬에서 꾸러미를 만들어 서버에 올리고, 서버에서 한 줄 실행하면 끝난다.
윈도우 바이너리는 가져가지 않는다 — 서버가 리눅스판을 직접 받는다.

## 1. 로컬에서 꾸러미 만들기

```powershell
cd C:\Users\myj68\Desktop\claude\capstone\live
python deploy\make_bundle.py
```

`live/deploy/hycad-deploy.tar.gz` 가 생긴다 (약 8 MB).

## 2. 서버로 올리기

```bash
scp live/deploy/hycad-deploy.tar.gz cap-normal:~/
```

`cap-normal` 은 `~/.ssh/config` 에 이미 있다 (capstone-gw 를 거쳐 marine@192.168.122.113).
MobaXterm 으로 올려도 된다.

## 3. 서버에서 한 줄

```bash
tar xzf hycad-deploy.tar.gz && \
GRAFANA_ADMIN_PASSWORD='아무도_모르는_긴_비밀번호' bash hycad-deploy/install.sh
```

설치 위치는 `~/hycad` 다. `BASE=/opt/hycad` 처럼 바꿀 수 있다.

## 설치되는 것

| 서비스 | 포트 | 내용 | 자동 기동 |
|---|---|---|---|
| `hycad-prometheus` | 127.0.0.1:9090 | TSDB. 밖으로 안 연다 | O |
| `hycad-grafana` | 0.0.0.0:3000 | 대시보드 3개 | O |
| `hycad-demo` | 127.0.0.1:9901 | 배속 데모 (한 바퀴 2분 39초) | O |
| `hycad-live` | 127.0.0.1:9900 | **sysdig 실시간 감시** | X (수동) |

| 대시보드 | 보여주는 것 |
|---|---|
| `/d/hycad-live` | 백필된 실험 기록 5.1시간 |
| `/d/hycad-demo` | 공격 3종 + 정상 드리프트 배속 |
| `/d/hycad-realtime` | **지금 도는 컨테이너 실시간** |

## 4. 실시간 감시 켜기 — 확인부터

컨테이너 이름이 기준선(`cat_baseline`, 2026-09-08 캡처)과 같아야 한다.
`docker-compose` 프로젝트 이름이 바뀌었으면 레플리카를 못 찾는다.

```bash
bash ~/hycad/run_live.sh check     # 5초만 떠서 지금 이름과 기대 이름을 나란히 출력
```

두 목록이 같으면:

```bash
sudo systemctl start hycad-live
sudo journalctl -u hycad-live -f    # 30초마다 판정 한 줄
```

다르면 `FILTER` 를 맞추거나, 정상 구간을 다시 떠서 기준선을 갱신해야 한다.

## 5. 밖에서 접속

**Grafana(3000)만** 열면 된다. 데이터소스가 `access: proxy` 라 Grafana 서버가
Prometheus 를 대신 조회하므로 9090 · 9900 · 9901 은 열 필요가 없고, 열어서도 안 된다.

| 방식 | 인바운드 포트 | HTTPS | 비고 |
|---|---|---|---|
| Cloudflare Tunnel | 불필요 | 자동 | `cloudflared tunnel --url http://localhost:3000` |
| 포트 포워딩 + nginx | 필요 | 직접 | 인증서 갱신까지 직접 관리 |

익명 열람이 켜져 있어 **주소를 아는 사람은 로그인 없이 본다.** 끄려면
`ANON=false` 로 재설치하거나 `custom.ini` 의 `[auth.anonymous] enabled` 를 false 로.

## 비용

sysdig 를 상시 돌리면 CPU 를 쓴다. 실측 이벤트 부하는 1,678~5,338건/초이고
판정 쪽 파싱 처리량은 213,353건/초(여유 40~126배)지만, **sysdig 자체의 비용은 별도다.**
2 vCPU 장비이니 실험을 다시 뜰 때는 `hycad-live` 를 꺼 두는 편이 안전하다.

```bash
sudo systemctl stop hycad-live
```

## 되돌리기

```bash
bash hycad-deploy/install.sh uninstall   # 서비스 제거 (파일은 남김)
rm -rf ~/hycad                            # 전부 삭제
```
