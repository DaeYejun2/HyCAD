# HyCAD Ubuntu 상시 운영과 복구

이 가이드는 cosmic의 **catalogue·front-end LIVE, 캡처 자동 순환, 캡처 수동 재생** 구성을 운영하는 절차다. 실제 주소·암호·키는 문서에 넣지 않는다. 새 기능의 배포 성공, 네트워크 복구, 서비스 재시작, 실제 호스트 재부팅은 수행한 항목만 별도 검증 기록에 남긴다.

운영 모니터는 **`compose.yaml` → `compose.server.yaml` → `compose.public.yaml`** 세 파일을 함께 사용한다. nginx는 별도 `compose.ingress.yaml`이다. 앞의 두 파일만 실행하면 익명 접근 설정이 되살아날 수 있으므로 공개 서버에서 구성을 섞지 않는다. 최초 인증서와 외부 경로는 [직접 HTTPS 운영](direct-https.md)을 따른다.

## 1. 서버와 경로

cosmic은 운영 호스트, marine-normal은 그 안의 libvirt 대상 VM이다. VM에서 Sock Shop·Sysdig가 실행되고 cosmic에서 수집 전달·모델·저장소·화면을 제공한다. Mac의 실행 상태와 무관하게 운영한다.

| 위치·단위 | 역할 |
|---|---|
| `/var/snap/docker/common/hycad/current` | 실제 코드·Compose·대시보드 릴리스 경로 |
| 같은 루트의 `releases/` | 버전별 코드 사본. `current`가 활성 버전을 가리킴 |
| `/opt/hycad/current` | 편의용 별칭. 실제 mount 경로로 임의 대체하지 않음 |
| `/etc/hycad/monitor.env` | 서버 환경·대상·데이터 디렉터리·공개 URL·관리자 비밀. root `0600` |
| `/etc/hycad/collector_ed25519`, `context_ed25519`, `known_hosts` | 제한된 수집·조회 키와 검증한 호스트 키 |
| `live/` | `events.jsonl`, 회전본, 두 기준선, 수집·보존 건강 상태 |
| `/var/snap/docker/common/hycad/captures/source/Grafana_caps` | VM에서 복사한 보호된 원본 자료. 재생 컨테이너에 직접 마운트하지 않음 |
| `/var/snap/docker/common/hycad/captures/approved` | `HYCAD_CAPTURE_DIR` 값. 승인 `catalog.json`, 캐시·메타데이터·기준선 |
| `evidence/`, `evidence-frontend/` | 서비스별 LIVE 조사 자료 |
| `/var/snap/docker/common/hycad/docker.sock` | 이 배포가 사용하는 Snap Docker 전용 소켓 |
| `/etc/hycad/ingress.env`, `https/` | nginx 설정·이미지 digest, 인증서·ACME·갱신 자료 |
| `/etc/hycad/grafana-viewer.json` | 조회 계정의 보호된 자격 증명. Git·일반 ZIP 제외 |

이 서버에는 다른 Docker 설치가 공존하므로 기본 소켓을 추정하지 않는다. 환경 파일의 `DOCKER_HOST`와 수동 CLI의 `--host`를 같은 Snap 소켓으로 고정한다. Compose `--env-file`만으로 Docker client endpoint가 설정된다고 가정하지 않는다.

## 2. 환경과 권한 준비

기존 서버의 보호된 환경 파일에는 다음 값을 준비한다. 값 자체를 저장소에 넣지 않는다.

| 설정 | 의미 |
|---|---|
| `DOCKER_HOST` | Snap 전용 Unix 소켓 |
| `HYCAD_DATA_DIR`, `HYCAD_DATA_GID` | LIVE 디렉터리와 읽기 그룹 |
| `HYCAD_CAPTURE_DIR` | `/var/snap/docker/common/hycad/captures/approved`. 원본 `source/Grafana_caps`와 구분 |
| `HYCAD_PUBLIC_URL` | 검증된 외부 HTTPS origin |
| `TARGET_HOST`, `TARGET_PORT` | 고정 수집 대상 SSH |
| `GRAFANA_ADMIN_PASSWORD` | 관리 비밀. 수집 프로세스에서는 제거됨 |

`live/baseline.pkl`은 catalogue, `live/frontend-baseline.pkl`은 front-end 참조다. front-end는 Node 4 과거 정상 캡처를 기준으로 현재 Node 10을 비교하므로 출처·버전 차이를 기록한다. 정상 여부를 검증하지 않은 현재 자료로 기준선을 덮어쓰지 않는다.

캐시·기준선·입력 파일은 detector UID 10001과 `HYCAD_DATA_GID`가 읽을 수 있어야 한다. 오프라인 importer의 출력은 기본 `0600`이므로 배포 시 필요한 읽기 그룹과 `0640` 권한, 상위 디렉터리 통과 권한을 부여한다. 디렉터리 전체를 누구나 읽게 만들지 않는다.

관리자 권한으로 고정 래퍼와 서비스 파일을 설치한다. 기준선·catalog·키가 존재하고 올바른 사용자에게 읽히는지 확인한 뒤 이미지를 빌드한다. systemd의 기동 명령은 자동 빌드하지 않는다.

## 3. VM 수집·컨텍스트 래퍼

수집 키는 고정 `hycad-capture` 명령만 요청한다. 기존 파일명 `hycad-sysdig-catalogue`는 호환성을 위해 유지하지만 실제 `serve_capture.py`의 허용 목록은 **catalogue 4개와 front-end 4개**다. 래퍼와 Python 감독기를 root 소유로 함께 배포한다.

- [`hycad-sysdig-catalogue`](../deployment/capture/hycad-sysdig-catalogue) → VM `/usr/local/libexec/hycad-sysdig-catalogue`
- [`serve_capture.py`](../deployment/capture/serve_capture.py) → VM `/usr/local/libexec/hycad-serve-capture.py`
- [`hycad-context-catalogue`](../deployment/capture/hycad-context-catalogue) → VM `/usr/local/libexec/hycad-context-catalogue`
- [`serve_context.py`](../deployment/capture/serve_context.py) → VM `/usr/local/libexec/hycad-serve-context.py`

수집 래퍼는 무인자 명령·잠금·정확한 컨테이너 이름으로 제한한다. 종료 때 자신이 실행한 Sysdig 프로세스 그룹만 정리한다. 원격 전체 프로세스를 일괄 종료하지 않으며 상시 서비스에 1시간 종료 제한을 넣지 않는다.

컨텍스트 래퍼는 지정된 catalogue/front-end 레플리카와 catalogue DB의 제한된 최근 로그·이미지·상태만 읽는다. 호출자가 컨테이너 이름이나 명령을 전달하지 않는다. 수집 키와 컨텍스트 키는 별도로 제한한다.

상시 수집기는 실패 후 대기 간격을 늘려 재접속하고 입력이 90초간 멈추면 연결을 다시 시도한다. 회전 파일은 각각 최대 256MiB인 `events.jsonl`, `events.jsonl.1`이다. VM에 새 원본을 무기한 쌓지 않는다. 기존 `Grafana_caps`는 이번 LIVE 회전 버퍼와 별개다.

VM과 가상 네트워크의 자동 시작·주소 예약, 대상 컨테이너의 실제 실행을 확인한다. 호스트가 켜져도 VM·네트워크·서비스가 준비되지 않으면 탐지기는 판정 불가일 수 있다. 재접속은 누락된 이벤트를 복원하지 않는다.

## 4. 캡처 반입과 catalog

이번 원본 대상은 `Grafana_caps`의 15개 gzip JSONL이다. 원본을 변경하지 않고 cosmic의 `/var/snap/docker/common/hycad/captures/source/Grafana_caps`로 복사하여 전송 전후 SHA-256을 비교한다. 저장 공간과 해시 검증 뒤 오프라인 변환한다. 전체 자료의 정상/공격 정답 여부를 파일명만으로 인증하지 않는다.

```bash
python -m hycad_monitor.capture_import --input SOURCE.jsonl.gz --output CAPTURE.pkl --replicas REPLICA_1,REPLICA_2,REPLICA_3,REPLICA_4
```

변환 결과의 UTC 창·품질·원본 해시·레플리카·진단을 `*.metadata.json`에서 확인한다. 생성한 캐시·메타데이터·기준선과 해시를 고정한 catalog는 `/var/snap/docker/common/hycad/captures/approved`에 둔다. 캐시/기준선 경로는 catalog 디렉터리 내부 상대 경로다. 이 승인 디렉터리만 `HYCAD_CAPTURE_DIR`로 read-only 마운트하며 원본 보관 디렉터리는 분리한다.

catalog 변경은 재생 서비스 재시작 시 읽힌다. 새 파일의 해시·기준선·서비스 대응을 검토한 뒤 자동·수동 worker에 함께 반영한다. LIVE 입력·기준선은 이 작업과 별개다. 허용 상한과 schema는 [UI·재생 구현](ui-and-replay-feasibility.md)을 참고한다.

## 5. 기동과 배포 확인

기존 설치가 준비된 Ubuntu에서 다음 진입점은 Snap 소켓과 세 Compose 파일을 고정한다. Bash가 환경 파일을 셸 코드로 실행하지 않는다. 전체 실제 설정을 출력해 비밀을 노출하는 대신 `config --quiet`를 사용한다.

```bash
cd /var/snap/docker/common/hycad/current
sudo bash Grafana/stack.sh config
sudo bash Grafana/stack.sh build
sudo bash Grafana/stack.sh start
sudo bash Grafana/stack.sh status
```

`deployment/systemd/snap/`의 모니터·수집·두 보존·ingress·갱신 단위와 `hycad-monitor-direct-public.conf` drop-in을 설치한다. front-end 보존 단위와 보호된 디렉터리도 빠뜨리지 않는다. 설치 후 필요한 서비스들을 enable한다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hycad-monitor.service hycad-collector.service hycad-evidence.service hycad-evidence-frontend.service hycad-ingress.service hycad-cert-renew.timer
sudo systemctl status hycad-monitor.service hycad-collector.service hycad-evidence.service hycad-evidence-frontend.service hycad-ingress.service --no-pager
```

| 서비스·확인점 | 의미 |
|---|---|
| `hycad-monitor.service`의 `active (exited)` | Compose 기동 명령이 끝났다는 뜻. 모든 컨테이너 정상 보장은 아님 |
| `detector:9108`, 호스트 loopback `9108` | catalogue LIVE |
| `detector-frontend:9108`, 호스트 loopback `9109` | front-end LIVE |
| `capture-replay:9110` | 모든 승인 캡처의 자동 순환 |
| `replay-control:9111` | 수동 제어·재생 하나. metrics는 내부용 |
| `/d/hycad-overview`, `/d/hycad-replay` | 서비스·모드·캡처 조건이 맞는 결과를 조회하는 두 화면 |
| `/hycad/replay/` | Grafana 로그인과 Origin·CSRF 검사를 거치는 수동 제어 폼 |

`up`과 `hycad_ready`를 구별한다. 두 LIVE 서비스에서 원본 창이 현재 시각으로 진행하고 각 4개 레플리카가 관측되는지 확인한다. 자동 캡처는 원본 역사 시각과 이번 처리 시각을 분리해 확인한다. 수동 시작·일시정지·재개·중단 중에도 LIVE가 계속 진행해야 한다.

외부에서는 HTTPS 인증서, Viewer 로그인, 두 화면, 제어 폼, 비로그인·잘못된 Origin/CSRF 거부를 확인한다. 인증 문제를 익명 공개나 보안 쿠키 해제로 우회하지 않는다. [공개 접속](public-access.md), [인증서 갱신](direct-https.md)

## 6. 중단·업데이트·장애 복구

전체 관측을 의도적으로 멈출 때는 갱신 타이머와 두 보존 서비스를 포함한다. 단순히 SSH 연결만 끊으면 수집기가 다시 접속한다. `Grafana/stack.sh stop`은 컨테이너만 멈추므로 전체 중단과 같지 않다.

```bash
sudo systemctl stop hycad-cert-renew.timer hycad-ingress.service hycad-evidence-frontend.service hycad-evidence.service hycad-collector.service hycad-monitor.service
```

업데이트는 새 릴리스를 `releases/`에 배치하고 `current`를 전환한다. 이미지·대시보드·수집 래퍼·두 LIVE·재생 구성·nginx 경로를 같은 버전으로 맞춘다. 환경·mount·command가 바뀌면 해당 컨테이너를 재생성하며 단순 restart가 새 설정을 적용한다고 가정하지 않는다.

`live/`, `captures/`, 사건 디렉터리, Prometheus·Loki·Grafana·두 incident-state 볼륨을 삭제하지 않는다. 기준선 교체는 코드 업데이트와 분리해 출처·해시·시각을 남긴다. 이전 릴리스로 복귀하더라도 공개 인증과 데이터 상태를 보존한다.

```bash
sudo systemctl start hycad-monitor.service hycad-collector.service hycad-evidence.service hycad-evidence-frontend.service hycad-ingress.service hycad-cert-renew.timer
```

| 증상 | 확인·복구 |
|---|---|
| LIVE 시각 정지·판정 불가 | VM·Sysdig·SSH·collector 상태와 두 서비스 입력을 확인. 공백을 정상으로 채우지 않음 |
| 한 서비스만 입력 부족 | 해당 4개 컨테이너·기준선 cohort·accept 수 확인. 다른 서비스 정상으로 대체하지 않음 |
| 캡처 worker 시작 실패 | catalog 상대 경로·hash·읽기 그룹·cache schema·한도 확인. 원본을 자동 수정하지 않음 |
| 수동 시작 충돌 | 공유 작업이 재생/일시정지 중인지 확인하고 중단 후 변경 |
| 수동 제어 401·403 | 동일 HTTPS 로그인·세션·Origin·만료 CSRF 확인. 폼을 새로 열어 시도 |
| 잘못된 Docker·mount | 명시 소켓·Docker Root Dir·Snap 실제 경로 확인. 다른 daemon으로 임의 전환 금지 |
| HTTPS 502·연결 실패 | Grafana·수동 controller·nginx DNS·TLS를 확인. 문제 없는 LIVE 수집을 함께 재시작하지 않음 |
| 인증서 갱신 실패 | 갱신 journal·외부 ACME 경로·인증서 SAN 확인. 기존 인증서 삭제 금지 |
| 디스크 부족 | 자료 보존 필요 여부 확인 후 관리. 볼륨·TSDB 일괄 삭제 금지 |

Prometheus는 7일·2GB, Loki는 7일이다. LIVE 보존은 서비스별 기본 7일·5GiB여서 두 서비스를 합하면 더 큰 공간이 필요하다. container 로그는 각각 10MB 3개, ingress는 5MB 2개다. 보존 기간만으로 전체 디스크 사용량이 고정되지는 않는다.

Grafana는 로컬 SQLite 단일 인스턴스다. WAL, `high_availability=false`, 쿼리 3회 재시도, 컨테이너 1GiB와 `GOMEMLIMIT=768MiB`를 유지한다. 과거 잠금 오류와 메모리 근접이 관측됐지만 인과가 확정된 것은 아니다. 재발하면 로그·OOM·메모리를 보존하고 DB 파일을 삭제하지 않는다.

## 7. 코드와 복구 자료

이번 공개 코드 업데이트 대상은 사용자 요청의 [DaeYejun2/HyCAD](https://github.com/DaeYejun2/HyCAD)다. 비공개 운영 사본과 Mac 백업도 유지한다. 실제 push 권한·커밋·원격 반영을 확인하고, 저장소가 있다는 이유만으로 업로드 완료라고 기록하지 않는다.

| 자료 | 보관 방법 |
|---|---|
| 코드·대시보드·템플릿·문서 | 공개 프로젝트의 검토된 커밋, 비공개 사본, Mac Git bundle·소스 archive |
| 기준선·catalog·캡처·메타데이터 | 원본/캐시 hash와 출처를 포함한 보호된 별도 자료. 일반 공개 Git 제외 |
| `/etc/hycad`·개인키·조회 암호·ACME 신원 | 접근 제한 또는 암호화된 별도 백업. Git·일반 복구 ZIP 제외 |
| 시계열·Grafana DB·사건 자료 | 선택한 Snap daemon의 일관된 볼륨/파일 스냅샷. 실행 중 DB 단순 복사 지양 |
| Snap daemon 설정 | 전용 소켓·실제 Docker Root Dir·서비스 설정·변경 전 설정 기록 |

미커밋 변경은 Git bundle에 들어가지 않는다. 커밋과 업로드를 확인한 뒤 bundle을 생성·검증하고 배포 커밋·기준선·catalog 해시를 함께 남긴다. 공개 자료에는 운영 주소·자격 증명·민감 이벤트를 포함하지 않는다.

전체 복구는 코드 → Snap daemon·소켓·실제 경로 → 보호된 설정·키·기준선·catalog → 필요한 상태 볼륨 → 이미지 → systemd·VM 준비 → HTTPS·수동 제어·두 LIVE 원본 창 진행 순서로 확인한다. 원본 운영 서버를 덮어쓰며 복구 시험하지 않는다. 실제 호스트 재부팅을 하지 않았다면 서비스 재시작 결과만 보고한다.
