# 서버 화면 공유

모델·수집·Grafana와 HTTPS 프록시는 졸작 서버에서 실행된다. 현재 접속 주소는
서버 `/etc/hycad/monitor.env`에서 검증한 `HYCAD_PUBLIC_URL`이다. Mac의 전원·
인터넷·SSH 터널을 거치지 않고 이 HTTPS 주소에서 조회 계정으로 로그인한다.

2026-10-23에는 기존 외부 80·443 전달 경로가 실제 서버에 도착하는 것을 확인하고
직접 HTTPS 공유를 구성했다. 공유기를 변경하지 않았다. Let's Encrypt의 공인 IP
인증서 발급, 외부 TLS 인증서 검증, 로그인 페이지 응답, 비로그인 대시보드 API의
401 차단과 실제 브라우저의 Viewer 로그인을 확인했다. 시간마다 확인하는 인증서
갱신 타이머를 활성화했다. 실제 호스트 재부팅과 갱신 dry-run 성공을 이 기록으로
주장하지 않는다.

현재 모니터는 `compose.yaml` + `compose.server.yaml` + `compose.public.yaml`의
세 파일이며, 별도 `compose.ingress.yaml` 프로젝트가 nginx를 실행한다.
부팅 override는 `hycad-monitor-direct-public.conf`를 사용한다. 실제 주소·
인증서 키는 저장소에 넣지 않는다. 설치와 복구는 [직접 HTTPS 운영](direct-https.md)을
따른다. HTTPS 보안 쿠키를 사용하므로 예전 localhost HTTP 터널은 로그인용 기본
접속 경로가 아니다.

Tailscale Funnel은 연구실 계정 정책에서 허용되지 않아 현재 공개 경로로 사용하지
않는다. 기존 Funnel 컨테이너는 제거했으며 등록 상태 볼륨은 보존했다. 아래의
Funnel 설치 절차는 나중에 그 방식을 다시 선택할 경우에만 사용하는 선택 사항이다.

## 공유 전 공통 준비

`scripts/ensure_grafana_viewer.py`를 서버에서 실행하면 모니터링 조직의 조회 전용
`hycad-viewer` 계정을 만든다. 관리자 암호는 보호된 `/etc/hycad/monitor.env`에서
읽고, 조회 암호는 `/etc/hycad/grafana-viewer.json`에 0600으로 저장한다. 콘솔에
암호를 출력하지 않는다. 기존 계정이나 암호를 임의로 덮어쓰지 않는다. 이 파일은
Git·일반 복구 ZIP에서 제외하고 사용 권한이 있는 사람에게 별도로 전달한다.

외부 접속점을 준비한 뒤 `/etc/hycad/monitor.env`에 확인된 HTTPS URL을
`HYCAD_PUBLIC_URL=https://실제주소/`로 기록하고, Compose와 부팅 서비스에
`compose.public.yaml`을 추가한다. 이 설정은 익명 접근을 끄고 회원가입·조직 생성·
Viewer 편집을 막는다. HTTPS 쿠키를 사용하므로 이후에는 외부 HTTPS 주소에서
로그인한다. 관리자는 기존 관리자 계정을 사용하며, 공유용 계정은 관리자가 아니다.

Grafana와 Prometheus·Loki·탐지기의 호스트 포트는 loopback 바인딩을 유지한다.
포트 3000을 인증 없이 직접 인터넷에 노출하지 않는다. Viewer는 같은 조직의
데이터소스에 질의할 수 있으므로 보호된 원본 이벤트·서비스 로그는 Loki에 넣지
않고, 제한된 판정·검토 설명만 전달한다.

## 접속점 선택

- 외부 전달 가능한 80·443이 있으면 서버 앞에 HTTPS 프록시를 둔다. 현재는 이
  방식이며 도메인 대신 검증된 공인 IP 인증서를 사용한다. 공유기 뒤의 서버는
  호스트 포트만 열어서는 외부 접속이 성립하지 않는다.
- Tailscale Funnel은 계정과 장치 등록을 통해 고정된 ts.net HTTPS 주소를 제공한다.
  Grafana와 같은 컨테이너 네트워크 공간의 전용 userspace 컨테이너를 사용하면
  호스트의 다른 loopback 서비스에 대한 접근을 분리할 수 있다. 호스트 네트워크,
  TUN, Tailscale SSH, 서브넷·출구 노드 광고를 사용하지 않는다. 장치 상태는
  영속 볼륨에 저장하고 Funnel은 `--bg`로 구성한다. Grafana를 재생성하면 연결
  컨테이너도 함께 재생성한다. 장치 키 만료 정책과 계정 정책을 별도 확인한다.
- 기존 Cloudflare 계정·도메인이 있다면 고정 named Tunnel을 사용할 수 있다.
  임시 Quick Tunnel 주소를 재부팅 후에도 고정된 발표 주소로 설명하지 않는다.

계정/도메인/외부 전달 경로를 실제로 확인하기 전에는 공유 설정이 완료됐다고
보고하지 않는다. 상시 구성도 외부 통신 사업자나 릴레이의 무중단을 보증하지 않는다.

## 선택 사항: Tailscale Funnel 설치 순서

이 절은 Funnel 방식을 선택한 뒤 서버 운영자가 적용하는 절차다. 선택하지 않은
환경에는 `compose.funnel.yaml`을 추가하지 않는다. 파일을 준비했다는 것과 실제
계정 연결·공개가 끝났다는 것은 다르다. 고정 이미지 버전은 `v1.104.2`다.

전용 컨테이너는 Grafana의 네트워크 공간만 공유한다. userspace 모드는 들어오는
tailnet 연결을 같은 localhost 포트로 전달하므로 **호스트에서 직접 실행하거나
`network_mode: host`로 바꾸지 않는다.** 공유 공간에는 Grafana만 있어야 한다.
Tailscale 설정은 호스트 DNS·경로·SSH·VM 네트워크를 변경하지 않는다. Funnel은
`shields-up=true`와 함께 동작하지 않으므로 컨테이너 분리와 계정 정책으로 범위를
제한한다. 컨테이너의 `/var/lib/tailscale`은 0700이며 전용 영속 볼륨을 사용한다.

### 1. 조회 계정과 비공개 데몬 준비

아래 명령은 Snap Docker 운영 서버의 저장소 루트에서 실행한다. 서버에서 root
권한으로 실행하며 `/etc/hycad/monitor.env`에 기존 운영 설정이 있어야 한다.
비밀번호·인증 키를 명령 인자나 셸 기록에 넣지 않는다.

```bash
cd /var/snap/docker/common/hycad/current
python3 scripts/ensure_grafana_viewer.py

hycad_bootstrap() {
  docker --host unix:///var/snap/docker/common/hycad/docker.sock compose \
    --env-file /etc/hycad/monitor.env \
    -f compose.yaml -f compose.server.yaml -f compose.funnel.yaml "$@"
}
hycad_bootstrap config --quiet
hycad_bootstrap pull funnel
hycad_bootstrap up --detach --no-build grafana funnel
```

`compose.funnel.yaml`은 공개 URL을 얻기 전부터 익명 접근을 차단한다. 최초의 빈
상태 볼륨에서는 데몬만 실행되며 계정 등록, `tailscale up`, Funnel 활성화를
자동 실행하지 않는다. `TS_AUTHKEY`를 환경변수로 주입하지 않는다. 이전에 직접
활성화한 상태 볼륨을 재사용하면 저장된 Funnel 설정이 복구될 수 있으므로 새
설치와 재시작을 구별한다.

계정 등록 전에 인증 없는 `/api/dashboards/uid/hycad-overview` 요청이 401인지,
대시보드 URL이 로그인으로 이동하는지 확인한다. `/api/health`의 200은 데이터
공개가 아니며 이 검사를 대신하지 않는다.

```bash
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  http://127.0.0.1:3000/api/dashboards/uid/hycad-overview
hycad_bootstrap exec -T funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock status --json
```

### 2. 수동 장치 등록과 URL 확인

```bash
hycad_bootstrap exec funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock up \
  --hostname=hycad-grafana --accept-dns=false --accept-routes=false \
  --ssh=false --shields-up=false
```

명령이 제시하는 로그인 링크에서 사용자가 자신의 계정으로 장치를 등록한다.
이 단계에서는 Funnel을 실행하지 않는다. 등록 후 다음 상태의 `Self.DNSName`을
확인한다. 이미 같은 이름의 장치가 있으면 이름이 달라질 수 있으므로 URL을
추측하지 않는다. DNS 이름 끝의 점을 제거하고 `https://실제DNS이름/` 형태로
`/etc/hycad/monitor.env`의 `HYCAD_PUBLIC_URL`에 기록한다. 이 파일의 기존 비밀과
권한은 유지한다.

```bash
hycad_bootstrap exec -T funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock status --json
hycad_bootstrap exec -T funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock funnel status
```

관리 콘솔에서 MagicDNS·HTTPS 인증서·이 장치의 Funnel 허용 여부를 확인한다.
연구실 관리자는 기존 정책을 보존한 채 `nodeAttrs` 배열에 아래 항목만 추가할 수
있다. `VERIFIED_TAILSCALE_IP`는 이 전용 컨테이너의 실제 `100.x` 주소로 바꾼다.
호스트의 외부 주소나 VM 주소를 쓰지 않는다. 새 정책 전체로 덮어쓰거나
`autogroup:member` 전체에 Funnel을 허용할 필요는 없다.

```json
{
  "target": ["VERIFIED_TAILSCALE_IP"],
  "attr": ["funnel"]
}
```

이 항목은 공개 기능을 사용할 장치만 제한하며, 공개된 화면의 방문자를 tailnet
사용자로 제한하는 인증 규칙은 아니다. 화면 접근은 별도의 Grafana 로그인으로
보호한다. [공식 Funnel 조건](https://tailscale.com/docs/features/tailscale-funnel)과
[IP 대상 지원](https://tailscale.com/docs/reference/targets-and-selectors)을 따른다.

2026-10-23 등록 직후 확인된 이 장치의 키 만료일은 2027-04-21이다. 졸업 작품
발표가 그 이전이면 현재 만료 정책을 유지하며, 만료 해제나 전체 tailnet 정책
변경을 요청하지 않는다. 그 이후까지 운영할 경우에는 만료 전 재인증이 필요하다.
장치 이름이나 tailnet 도메인을 바꾸면 발표 URL도 달라질 수 있다.

### 3. HTTPS 설정과 재부팅 구성을 먼저 확정

선택한 tailnet의 Funnel 사용 권한과 실제 DNS를 확인한 뒤 공통 공개 설정을
적용한다. 관리자 허용을 기다리는 bootstrap 단계에 이 설정을 적용하면 localhost
HTTP 로그인에 제약이 생기므로 먼저 적용해 두지 않는다. 이후 공개 모드의
관리·배포 명령에서는 네 개의 Compose 파일을 같은 순서로 사용한다.

```bash
hycad_public() {
  docker --host unix:///var/snap/docker/common/hycad/docker.sock compose \
    --env-file /etc/hycad/monitor.env \
    -f compose.yaml -f compose.server.yaml \
    -f compose.public.yaml -f compose.funnel.yaml "$@"
}
hycad_public config --quiet
hycad_public up --detach --no-build --force-recreate grafana funnel
python3 scripts/ensure_grafana_viewer.py
```

Grafana와 Funnel을 함께 재생성해 공유 네트워크 공간이 엇갈리지 않게 한다.
`depends_on.restart: true`는 Compose가 관리하는 의존 서비스 변경을 처리하며,
Docker 런타임의 모든 장애 복구를 보증하지 않는다. Grafana 컨테이너를 개별적으로
교체하지 않는다.

부팅 시 공개 설정이 빠져 익명 접근이 다시 켜지지 않도록 `hycad-monitor.service`의
`ExecStart`와 `ExecStop`에도 네 파일을 모두 넣는다. 실제 서비스의 기존 설정을
확인한 뒤 저장소의 `deployment/systemd/snap/hycad-monitor-public.conf` 템플릿을
`/etc/systemd/system/hycad-monitor.service.d/public.conf`에 설치한다. 초기 등록
대기 동안 사용한 세 파일 bootstrap drop-in이 있다면 공개 모드의 네 파일
템플릿으로 교체한다. 이 문서나 Compose 파일 자체가 서비스를 수정하지는 않는다.

```bash
install -d -m 0755 /etc/systemd/system/hycad-monitor.service.d
install -m 0644 deployment/systemd/snap/hycad-monitor-public.conf \
  /etc/systemd/system/hycad-monitor.service.d/public.conf
systemctl daemon-reload
systemctl cat hycad-monitor.service
systemctl is-enabled hycad-monitor.service
```

실제 unit의 `ExecStart`와 `ExecStop`이 네 파일을 포함하는지 확인한다. 단순 설정
확인을 위해 수집·모델 서비스를 전체 재시작할 필요는 없다. `--remove-orphans`가
있는 기존 두 파일 명령으로 되돌아가면 Funnel이 제거되므로 배포 절차도 함께
갱신한다. `docker compose config`의 전체 출력에는 암호가 포함될 수 있으므로
공유하거나 기록하지 말고 `--quiet`로 형식만 검사한다.

### 4. 차단 검증 뒤 공개 활성화

공개 전 loopback에서 비로그인 API 401, Viewer 대시보드 읽기 성공, 관리자 API
403을 다시 확인한다. `ensure_grafana_viewer.py`는 조회 계정의 조직 역할과 읽기·
관리 권한을 점검한다. Viewer의 대시보드 쓰기 요청도 403인지 별도로 확인한다.
조회 자격 증명은 보호된 파일에서 읽고 출력하지 않는다. 준비가 끝난 뒤에만
다음 명령을 실행한다.

```bash
hycad_public exec funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock funnel \
  --bg --https=443 http://127.0.0.1:3000
hycad_public exec -T funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock funnel status
```

Funnel 허용이 아직 없으면 해당 tailnet 관리자가 웹 설정 단계를 완료해야 한다.
일반 구성원 계정으로 관리자 화면에 접근할 수 없다면 등록 성공을 공개 허용으로
간주하지 않는다. 공개 연결은 Grafana 로그인으로 보호하며 Tailscale 계정 헤더를
Grafana 인증으로 사용하지 않는다. 로그인 링크나 새 장치 인증 값을 Git·발표
자료에 복사하지 않는다.

외부 네트워크에서 실제 HTTPS 인증서와 로그인·대시보드를 확인한다. 같은 환경에서
비로그인 API 차단과 Viewer 쓰기·관리 차단을 확인한다. tailnet 장치가 있다면
게이트웨이 IP의 9090·3100·9108·22가 열리지 않는지도 확인한다. 검증 중 공개
데이터가 인증 없이 조회되면 Funnel을 즉시 끄고 설정을 고친다.

### 5. 자동 복구와 중지

`--bg`의 설정과 장치 키는 전용 볼륨에 남고 Docker가 게이트웨이를 다시 시작한다.
Mac 터널을 사용하지 않는 외부 접속과 `hycad_public restart funnel` 뒤 같은 URL의
복구를 확인한다. 프로세스 상태나 `funnel status`의 활성 표시만으로 외부 접속
성공을 판단하지 않는다. 실제 서버 재부팅은 별도로 실행한 경우에만 검증 완료로
기록한다. 서버의 전원·인터넷 및 Tailscale 계정·중계 서비스는 여전히 필요하다.

공개 경로만 해제하려면 다음을 사용한다. Grafana·수집·모델은 계속 실행된다.

```bash
hycad_public exec -T funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock funnel --https=443 off
hycad_public exec -T funnel /usr/local/bin/tailscale \
  --socket=/var/run/tailscale/tailscaled.sock funnel status
```

보존된 장치 신원을 삭제하는 `down --volumes`나 임의 상태 볼륨 삭제를 복구 방법으로
사용하지 않는다. `funnel-state`는 코드 백업 ZIP에서 제외하고, 별도로 백업할 경우
자격 증명과 같은 수준으로 보호한다. 만료·연결 장애가 생기면 먼저 로컬 Grafana,
장치 인증, 외부 HTTPS 순서로 확인한다.

## 확인할 결과

외부 HTTPS 로그인과 대시보드 조회, 비로그인 요청 차단, Viewer의 관리 기능 차단을
확인한다. Mac SSH 터널과 관계없는 외부 접속 경로를 검사하고, 게이트웨이 재시작
뒤 같은 주소가 복구되는지 확인한다. 실제 호스트 재부팅 시험과 구분한다.

공식 근거: [Grafana reverse proxy](https://grafana.com/tutorials/run-grafana-behind-a-proxy/),
[Grafana 보안](https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/),
[Funnel](https://tailscale.com/docs/reference/tailscale-cli/funnel),
[Tailscale Docker와 userspace 전달](https://hub.docker.com/r/tailscale/tailscale),
[공식 이미지의 바이너리 경로](https://github.com/tailscale/tailscale/blob/v1.104.2/build_docker.sh),
[Quick Tunnel 한계](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).
