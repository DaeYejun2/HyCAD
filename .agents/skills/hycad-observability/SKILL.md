---
name: hycad-observability
description: HyCAD의 replay/live 탐지, Prometheus 지표·경보, Loki 판정 설명과 Grafana 대시보드를 함께 설계·수정·검증한다. 이 저장소의 관측 파이프라인 변경에 사용하며 일반 웹 화면 제작이나 원격 공격 실험에는 적용하지 않는다.
---

# HyCAD 관측 파이프라인

이 스킬은 저장소 안에서 사용하는 프로젝트 전용 지침이다. 프로젝트 루트는 이 파일에서 `../../..`이다. 구성 변경 전에 [아키텍처](../../../docs/architecture.md)를 읽고, 실행·배포·복구 작업에는 [운영 가이드](../../../docs/operations.md)를 함께 확인한다. 시연 요청에는 [발표 실행 가이드](../../../docs/presentation-runbook.md)를 사용한다.

## 변경 경계

- 연구 계산은 `controller.py`, `channels.py`, `preprocess.py`에 있고 운영 연결은 `hycad_monitor/`, `monitoring/`, Compose 파일에 있다. 관측 기능만 바꿀 때는 연구 계산과 임계값을 함께 변경하지 않는다. 모델 변경을 요청받으면 해당 변경과 검증 범위를 명시한다.
- 초기 운영 모드는 정상 기준선 고정이다. `closed_loop.py`의 시뮬레이션을 근거로 자동 재학습이 운영 중이라고 설명하지 않는다.
- 기본 Compose 실행은 기존 캐시 replay다. replay의 레플리카 이상을 현재 서버의 공격으로 설명하지 않는다. 원격 수집기나 실험 환경 조작은 현재 요청의 범위를 확인한 뒤 수행한다.
- 이전 Mac 임시 SSH live 경로는 `scripts/collect_ssh.py` → 로컬 파일 → 네이티브 탐지기 :9109 → `compose.ssh.yaml`의 Prometheus/Grafana다. 빠른 파일 추가가 Docker bind mount에서 지연된 관측에 따른 선택이므로 같은 현상을 다시 만들지 않는다. Linux 직접 마운트 경로인 `compose.live.yaml`은 별도로 지원한다.
- 사용자 요청에 따른 상시 서버 모드는 `collect_service.py`와 `compose.server.yaml`을 systemd가 감독한다. 실제 외부 서버는 Snap Docker이므로 `/var/snap/docker/common/hycad/{current,releases,live}`와 `deployment/systemd/snap/`을 사용한다. `/opt/hycad/current`는 별칭이고 설정·키는 `/etc/hycad`에 둔다. 일반 Docker 환경용 템플릿의 `/opt`·`/var/lib` 경로와 섞지 않는다. Mac 임시 경로의 1시간 제한을 상시 서비스에 적용하지 않는다.
- 실제 서버의 Docker는 `/etc/hycad/monitor.env`의 `DOCKER_HOST=unix:///var/snap/docker/common/hycad/docker.sock`로 고정한다. 수동 CLI는 같은 `--host`를 명시한다. 여러 Docker 설치가 있으므로 기본 소켓으로 daemon을 추정하거나 설치를 임의 제거·전환하지 않는다. Compose `--env-file`만으로 Docker client endpoint가 설정됐다고 가정하지 않는다.
- 과거 Mac의 `io.hycad.dashboard-tunnel` LaunchAgent는 localhost 3000을 서버 Grafana에 연결했다. 현재 외부 HTTPS 운영 경로는 cosmic의 nginx를 사용하며 Mac Docker·터널은 운영에 필요하지 않다. 서버 관측 수명은 Mac 접속과 무관하다. 코드 사본 `~/Documents/HyCAD-Monitor`와 키 보관 위치 `~/Library/Application Support/HyCAD`를 분리하고, 터널 키의 셸·포워딩 범위를 넓히지 않는다.

## 유지할 계약

- 운영 판단에는 해당 서비스·이미지·레플리카 집합·수집 정책으로 검증한 정상 기준선을 사용한다. 기존 서버에서 가져온 `data/live/baseline.pkl`도 새로 검증한 정상 자료가 아니므로 연결 확인과 정확도 검증을 구분한다. 기준선 보정에 공격 데이터를 넣지 않는다.
- 30초 창, 입력 필드, 정규화, 알려지지 않은 시스템콜 처리, 레플리카 매핑을 변경하면 기준선 호환성과 연구 계산의 의미를 함께 확인한다.
- 누락·불완전·분모 0·오래된 입력은 `insufficient_data`다. 원본 이벤트 시각 진행을 확인하지 않은 창을 벽시계만으로 정상 확정하지 않고, 중단 후 복구한 첫 창은 제외한다. exporter 생존과 유효한 정상 판정을 구별한다. 이상 경보는 live이면서 입력이 충분하고 신선할 때만 발화하도록 유지한다.
- 임시 `collect_ssh.py`는 기존 제어 소켓·숨김 sudo 입력·최대 1시간·파일 크기 제한을 유지한다. 상시 `collect_service.py`는 제한된 전용 SSH 키와 root 소유 고정 명령 래퍼로 비밀번호 저장 없이 재접속한다. 키의 명령 제한을 풀거나 임의 셸·포워딩 권한을 추가하지 않는다. 재시작과 재연결은 서비스 중지 지시를 따르며 내부 대상에 원본 캡처를 쌓지 않는다.
- 원격 `serve_capture.py`는 SSH 출력 파이프 닫힘과 종료 신호를 감시하고 자신이 실행한 Sysdig 그룹만 정리한다. 잠금은 감독기에 남기고 자식에 상속하지 않는다. 수집기 재접속 문제가 생겨도 원격의 관련 없는 프로세스를 일괄 종료하지 않는다.
- C의 accept 계열 호출 수는 HTTP 요청 수가 아니다. 연결 재사용·성공 여부·이벤트 방향 정책의 일관성을 확인하며 요청당 실제 비용으로 표현하지 않는다.
- 프로세스 명령행·파일 경로·원본 이벤트를 Prometheus 라벨에 넣지 않는다. 판정 근거는 선택적 JSONL 기록과 원본 수집 자료로 추적한다.
- 분석 설명은 실제 판정 분기·점수·임계값·기여 특성에서 생성한다. 제한된 프로세스 이름·시스템콜을 사용하고 명령행·인자·비밀을 그대로 노출하지 않는다. 설명 JSON은 Loki 본문에 저장하며 긴 설명이나 개별 특성값을 Prometheus/Loki 라벨로 만들지 않는다. 원본 창과 replay/live 모드를 유지하고 모델 근거를 침해 확인으로 표현하지 않는다.

## 작업 흐름

현재 CLI와 메트릭 정의를 먼저 읽고 필요한 탐지기·Prometheus 질의·Grafana 패널을 한 묶음으로 수정한다. 지표 이름·라벨·상태값을 바꾸면 경보와 대시보드 질의도 갱신한다. 실행 명령은 운영 가이드와 실제 `--help`를 맞춘다.

데이터 품질이나 탐지 로직을 바꿀 때는 관련 회귀 검증을 실행한다. 유효 입력·누락 레플리카·0 accept·지연 입력·캐시 마지막 창·replay 경보 차단 중 변경에 영향을 받는 경로를 확인한다. 인프라 변경은 Compose 구성 해석, Prometheus 규칙, 실제 수집 여부와 Grafana 패널을 가능한 환경에서 확인한다. 실행할 수 없는 검증은 이유와 함께 미검증으로 남긴다.

상시 운영 변경에서는 서비스 재시작과 연결 중단 후 복구를 구분해서 확인한다. 실제 호스트 재부팅 시험은 수행한 경우에만 검증으로 기록한다. 수집 공백이나 복구 첫 창을 정상으로 메우지 않는다. 코드 보존은 검토된 배포 커밋과 Git 스냅샷을 사용한다. 공개 upstream에 반영하라는 명시적 요청이 있으면 공개 대상 트리를 검토하고 upstream의 현재 이력 위에 반영한다. 비공개 저장소의 과거 이력이나 비밀·원본 캡처를 공개 이력에 합치지 않는다. 사용자가 선택한 비공개 저장소 사본과 Mac 복구용 백업도 유지한다. 서버 설치·복구의 세부 계약은 [서버 운영 가이드](../../../docs/server-operations.md)를 확인한다.

완료 보고에는 실제 실행한 모드, 입력 자료, 확인한 경로, 실행하지 않은 live 검증을 구분한다. 연구 결과를 새 환경의 정확도로 옮기지 않는다. 불필요한 외부 알림, 자동 차단, 전역 설정 변경을 작업에 덧붙이지 않는다.

## 조사 자료 보존

화면의 부분 감염 분류명은 ‘부분 이상 의심’으로 표시한다. 내부 `partial_infection` 값과 연구 임계값은 유지한다. 지속 시간에는 연속 유효 창만 포함하고 공백은 정상 복귀로 표시하지 않는다. 보호된 원본·연결 목적지·서비스 로그 보존은 [조사 자료 보존](../../../docs/incident-preservation.md)을 따르며, 수집 키와 읽기 전용 컨텍스트 키를 분리한다. 시험용 사건을 실제 LIVE 판정으로 등록하거나 과거 사건의 없던 원본을 만들어내지 않는다.
