# 서버에 직접 연결하는 HTTPS 화면

외부 80·443의 전달 경로가 실제로 이 서버에 도착하는지 먼저 검증한다. 서버의
사설 주소에 임시 응답을 띄우고 외부 주소에서 같은 응답을 읽어 확인할 수 있다.
기존 경로가 확인되면 별도 계정이나 Mac 터널 없이 서버의 HTTPS 프록시를 쓴다.
공유기 설정이 없는 환경에서는 이 문서의 파일만 설치해도 외부 접속이 생기지 않는다.

`외부 HTTPS → hycad-ingress의 nginx → grafana:3000` 경로다. 게이트웨이는 별도
Compose 프로젝트이며 기존 `hycad-monitor_default` 네트워크에 연결한다. 수집기,
탐지기와 저장소를 재시작할 필요가 없다. Grafana·Prometheus·Loki·탐지기의 기존
호스트 포트는 loopback에 유지한다. 외부 프록시는 Grafana 경로만 전달한다.

## 최초 설치

1. `deployment/ingress/ingress.env.example`을 `/etc/hycad/ingress.env`에 복사하고
   실제 LAN 바인딩 주소·검증한 이미지 digest를 넣는다. nginx는 공식 이미지,
   Certbot은 IP 인증서를 지원하는 5.4 이상을 사용한다. 환경 파일은 0600으로 둔다.
2. `/var/snap/docker/common/hycad/https` 아래 `nginx/conf.d`, `www`, `letsencrypt`,
   `certbot-work`, `certbot-logs`를 만든다. 비밀키가 있는 `letsencrypt`는 보호하고
   일반 코드 백업·Git에 넣지 않는다. nginx의 인증서 마운트는 읽기 전용이다.
3. `nginx-bootstrap.conf`를 `https/nginx/conf.d/default.conf`로 복사한다. 이 모드는
   HTTP ACME 검증 경로만 열고 나머지 요청은 503으로 돌려보낸다. 로그인을 평문으로
   제공하지 않는다. `compose.ingress.yaml`의 게이트웨이를 실행한다.
4. `www/.well-known/acme-challenge/`의 임시 시험 파일이 외부 HTTP 주소에서 읽히는지
   확인하고 시험 파일을 지운다. Certbot 컨테이너에 아래 네 디렉터리를 같은 경로로
   연결해 인증서를 발급한다.

| 서버 HTTPS 디렉터리 아래 | Certbot 내부 |
|---|---|
| `letsencrypt` | `/etc/letsencrypt` |
| `www` | `/var/www/certbot` |
| `certbot-work` | `/var/lib/letsencrypt` |
| `certbot-logs` | `/var/log/letsencrypt` |

공인 IP 인증서는 `certonly --webroot --webroot-path /var/www/certbot
--preferred-challenges http --preferred-profile shortlived --ip-address <실제 공인 IP>
--cert-name <HYCAD_CERT_NAME>`를 사용한다. ACME 계정 등록 시 이용약관 및 연락처
옵션을 명시한다. 도메인 인증서를 쓸 때는 검증된 DNS 이름을 `-d`로 지정한다.

5. `nginx-tls.conf.template`의 `@@PUBLIC_HOST@@`, `@@CERT_NAME@@` 두 표시만 실제
   외부 호스트와 인증서 이름으로 바꾼다. 공인 IPv4 또는 DNS 이름을 사용하며 경로나
   쿼리를 넣지 않는다. nginx의 `$host`, `$request_uri` 같은 변수까지 치환하지 않는다.
6. 운영 Compose를 `compose.yaml` + `compose.server.yaml` + `compose.public.yaml`의
   세 파일로 구성하고,
   `/etc/hycad/monitor.env`의 `HYCAD_PUBLIC_URL`을 실제 `https://호스트/`로 설정한다.
   조회 계정을 확인한 뒤 Grafana를 재생성한다. 기존 Funnel 컨테이너는 이 구성의
   `--remove-orphans`로 제거하며 장치 신원이 담긴 볼륨은 보존한다. 직접 HTTPS 모드는
   Tailscale에 의존하지 않는다. 탐지기는 건드리지 않는다.
7. TLS 설정을 활성 설정으로 교체하고 `nginx -t` 성공 뒤 게이트웨이를 reload한다.
   HTTP는 고정 HTTPS 주소로 이동하며, Docker DNS를 재조회하여 Grafana 재생성 후에도
   새 컨테이너로 연결한다. WebSocket과 원래 경로·쿼리를 함께 전달한다.

gateway 명령은 항상 전용 Docker 소켓과 별도 환경 파일을 사용한다.

```bash
docker --host unix:///var/snap/docker/common/hycad/docker.sock compose \
  --env-file /etc/hycad/ingress.env -f compose.ingress.yaml config --quiet
docker --host unix:///var/snap/docker/common/hycad/docker.sock compose \
  --env-file /etc/hycad/ingress.env -f compose.ingress.yaml up --detach --no-build
```

## 자동 시작과 인증서 갱신

Snap 서버용 `hycad-ingress.service`, `hycad-cert-renew.service`,
`hycad-cert-renew.timer`를 `/etc/systemd/system/`에 설치한다. 실제 인증서와 TLS
설정이 동작한 후 `daemon-reload` 및 ingress 서비스·갱신 타이머 활성화를 수행한다.
타이머는 시간마다 갱신 필요 여부를 확인하고 부팅 동안 놓친 실행도 재개한다.

같은 디렉터리의 `hycad-monitor-direct-public.conf`를
`/etc/systemd/system/hycad-monitor.service.d/public.conf`에 설치해 기존 bootstrap
또는 Funnel용 drop-in을 교체한다. 부팅·정지 때도 base + server + public의 세 파일을
사용하는지 확인한다. Funnel용 네 파일 drop-in과 직접 HTTPS용 세 파일 drop-in을
동시에 설치하지 않는다. 서비스 정의를 갱신하기 위해 수집기나 탐지기를 다시 시작할
필요는 없다.

갱신 스크립트는 digest로 고정한 Certbot 이미지와 기존 webroot를 사용한다.
Certbot 성공 뒤에만 nginx 설정 검사와 reload를 실행한다. `--dry-run`은 ACME
staging 갱신을 검증하며 nginx를 reload하지 않는다. 실제 갱신 서비스 실행과
타이머의 다음 예정 시각·최근 결과를 확인한다. 실패를 정상으로 덮지 않으며
실패 로그는 `journalctl -u hycad-cert-renew.service`에서 확인한다.

IP 인증서는 약 6일(160시간) 유효하므로 자동 갱신이 필수다. 공인 IP 변경,
외부 HTTP 차단 또는 인증기관 통신 실패는 갱신을 막을 수 있다. 인증서 발급·갱신,
게이트웨이 재시작 및 실제 호스트 재부팅 검증을 서로 구분해 기록한다.

## 확인할 결과

Mac의 localhost를 거치지 않고 실제 외부 HTTPS 주소에서 인증서 검증, 로그인,
대시보드 수치와 최신 판정 조회를 확인한다. 비로그인 대시보드 API 401, Viewer의
읽기 성공과 쓰기·관리 API 403을 확인한다. HTTPS에서는 Secure 쿠키가 설정되어야
하며 HTTP에서는 로그인 화면 대신 HTTPS로 이동해야 한다. 게이트웨이 재시작 뒤
같은 주소를 다시 확인한다. Prometheus·Loki·탐지기 포트는 공개하지 않는다.

공식 근거: [Grafana 프록시](https://grafana.com/tutorials/run-grafana-behind-a-proxy/),
[Let's Encrypt IP 인증서](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability/),
[Certbot webroot 및 갱신](https://eff-certbot.readthedocs.io/en/stable/using.html).
