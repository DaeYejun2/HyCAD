# Ubuntu Grafana 운영 구성

이 디렉터리는 원본 Windows 시연 UI를 **현재 Ubuntu 상시 관측 시스템**에 연결하는 진입점이다. 모델은 루트 `controller.py`, `channels.py`를 사용하는 `hycad_monitor` 하나로 관리한다. 원본 UI의 비교/빈도/작업량 그래프, 배수와 B 로그축, 레플리카 색·선 구분을 사용하고 운영 판정 불가·지목 근거를 유지한다.

## 두 대시보드

| 대시보드 | 용도 |
|---|---|
| `/d/hycad-overview` | 서비스(catalogue/front-end), LIVE/캡처 순환 재생, 캡처 선택. LIVE 조사 상세는 접힌 행에 보관 |
| `/d/hycad-replay` | 승인된 캡처 하나를 처음부터 수동 재생. 상단 제어 링크에서 시작·일시정지·재개·정지·배속 지정 |

수동 재생 제어는 같은 HTTPS 주소의 `/hycad/replay/`에서 Grafana 로그인 세션을 확인하고 실행한다. 제어는 POST와 CSRF 검사를 사용한다. LIVE, 자동 순환, 수동 재생은 독립 실행이며 수동 시작/정지로 LIVE를 바꾸지 않는다. 캡처 모드에서 공격 프로그램을 다시 실행하지 않는다.

대시보드 원본은 [`monitoring/grafana/dashboards/`](../monitoring/grafana/dashboards/)이며 [`scripts/build_dashboard.py`](../scripts/build_dashboard.py)로 생성한다. 비교용 원본 디자인은 [`reference/upstream-realtime.json`](reference/upstream-realtime.json)이다. 활성 JSON을 이 디렉터리에 중복 복사해 관리하지 않는다.

[구 배포 번들의 manifest](reference/upstream-bundle-manifest.py)는 `revsh_1of4`~`revsh_4of4` 확장 정의를 포함한 원본 참고 자료다. Windows 경로와 당시 실험 주석은 보존하고 줄바꿈만 Linux용 LF로 정규화했으며 현재 실행에 import하지 않는다. **현재 실제 입력 정의는 승인된 `catalog.json`**이다. 제거한 [이전 배포 번들](https://github.com/DaeYejun2/HyCAD/blob/79bdc1e863b70f7e3430cedee3bbb2b791d39474/Grafana/deploy/hycad-deploy.tar.gz)은 공개 Git 이력에서 복원할 수 있다. 번들에는 루트와 중복인 12개 외에 번들 전용 캐시 15개도 있었으므로 전체를 단순 중복 자료로 간주하지 않는다. 현재 재생은 별도로 검증·등록한 catalog 자료만 사용한다.


## Windows 구성에서 달라진 점

- PowerShell·`.exe`·사용자별 `C:\\...` 경로를 쓰지 않는다. `stack.sh`는 Bash와 Docker Compose를 사용한다.
- 경로는 저장소 루트 또는 서버 환경 파일로 결정하며 쉘 파일은 LF 줄바꿈이다.
- localhost는 브라우저 PC가 아닌 Ubuntu 서버의 내부 연결을 의미한다. 외부는 기존 인증된 nginx HTTPS 경로를 사용한다.
- `deploy/env/짧은 replica` 대신 `deployment/mode/capture/실제 replica`로 연결해 서비스·캡처를 분리한다.
- 원본의 자동 재학습·익명 공개·포괄적인 프로세스 종료·TSDB 삭제 방식은 운영 진입점에서 사용하지 않는다. 정상 기준선은 고정하고 drift는 검토할 변화 후보로 표시한다.
- 과거 `stack.ps1`, `stack/openmetrics.txt`, 배포 tar·pyc 등은 원본 Git 이력에서 확인한다. 운영 설치 자료로 사용하지 않는다.

## Ubuntu 실행

이미 보호된 환경 파일, 정상 참조 캐시, 캡처 목록, SSH 수집 서비스가 설치된 cosmic에서:

```bash
sudo bash Grafana/stack.sh config
sudo bash Grafana/stack.sh build
sudo bash Grafana/stack.sh start
sudo bash Grafana/stack.sh status
```

서버 환경 파일의 `HYCAD_DATA_DIR`, `HYCAD_CAPTURE_DIR`, `HYCAD_PUBLIC_URL`, `GRAFANA_ADMIN_PASSWORD` 등을 준비해야 한다. `stack.sh`는 설정 파일을 셸 코드로 실행하지 않는다. 초기 설치·서비스 자동 시작은 [서버 운영 가이드](../docs/server-operations.md)를 따른다.

별도 시험 환경의 Python 진입점:

```bash
python Grafana/hycad_live.py live --help
python Grafana/hycad_live.py replay --help
python Grafana/hycad_live.py manual --help
python Grafana/backfill.py --help
```

`backfill.py`는 현재 원본→검증된 집계 캐시 변환 도구다. Prometheus 데이터를 삭제하거나 과거 블록을 LIVE 이력에 끼워 넣지 않는다. 운영 캡처·원본 경로·암호는 Git에 넣지 않는다. 전체 배치 그림과 처리 흐름은 [프로젝트 README](../README.md)를 참고한다.
