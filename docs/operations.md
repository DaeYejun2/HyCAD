# HyCAD 모니터 운영 가이드

명령은 저장소 루트에서 실행한다. 구조와 점수 해석은 [아키텍처](architecture.md)를 참고한다. **상시 운영·재부팅 복구·백업은 [서버 운영 가이드](server-operations.md)**를 따른다. 이 문서는 기본 replay와 Linux 직접 입력, Mac 임시 SSH 경로를 다룬다. 발표 시연은 [발표 실행 가이드](presentation-runbook.md)를 참고한다. live 관측을 운영 판단에 쓰려면 해당 배포의 정상 기준선을 검증해야 한다.

## 1. 로컬 대시보드 실행

Docker Engine과 Docker Compose가 실행 가능한 환경에서 다음을 사용한다.

상시 서버용 자동 터널이 설치된 Mac에서는 localhost 3000이 서버 화면용으로 사용된다. 이 Mac의 로컬 replay는 [발표 실행 가이드](presentation-runbook.md)의 13000 포트 구성을 사용한다. 아래 기본 명령은 localhost 3000이 비어 있는 별도 로컬 환경을 위한 것이다.

```bash
python3 scripts/setup_local.py
docker compose up --build -d
docker compose ps
```

설정 스크립트는 관리자 비밀번호를 `.env`에 만들고 이미 존재하는 파일은 유지한다. 로컬 대시보드는 로그인 없이 Viewer 권한으로 열린다. 편집·관리 작업의 사용자명은 `admin`이며 비밀번호는 로컬 `.env`의 `GRAFANA_ADMIN_PASSWORD` 값이다. 이 파일을 저장소나 공유 문서에 넣지 않는다.

| 접속 주소 | 확인할 내용 |
|---|---|
| [Grafana 대시보드](http://127.0.0.1:3000/d/hycad-overview) | 실행 모드, 최근 판정, A/B/C 점수와 임계값, 레플리카별 상태, 입력 신선도 |
| [Prometheus targets](http://127.0.0.1:9090/targets) | `hycad` 대상의 수집 성공 여부 |
| [Prometheus alerts](http://127.0.0.1:9090/alerts) | 경보 규칙 상태. replay에서는 live 이상 경보가 발화하지 않는다. |
| [탐지기 지표](http://127.0.0.1:9108/metrics) | 현재 탐지 상태와 지표 원문 |
| [탐지기 상태](http://127.0.0.1:9108/status) | 판정·입력 오류 사유·처리 시간의 JSON |
| [Loki 준비 상태](http://127.0.0.1:3100/ready) | 판정 설명 로그 저장소의 준비 여부 |

`/healthz`는 프로세스의 응답 여부를 확인한다. `/readyz`는 유효한 최근 판정이 있을 때 200, 데이터가 부족하거나 오래되면 503을 반환한다. Docker의 healthy 상태만으로 live 입력이 준비됐다고 판단하지 않는다.

Compose의 기본 replay는 `cat_1of4` → `cat_4of4` 캐시를 순환하고 기준선은 `cat_baseline`을 사용한다. 각 공격 캡처의 초기 정상 구간도 그대로 재생한다. 원본의 30초 창을 5초마다 한 개씩 처리하므로 화면의 재생 속도와 원본 수집 시간은 다르다. 캐시의 마지막 창은 불완전할 수 있어 판정하지 않는다. CLI에서 `--replay`를 생략하면 정상 기준선 전체도 먼저 재생한다.

**화면이 replay 모드일 때 레플리카 1의 이상 표시는 과거 자료의 재생 결과다.** 현재 서버에서 새로운 공격이 실행 중이라는 뜻이 아니다. 이번 구축 과정에서 새 공격은 실행하지 않았다. 대시보드의 실행 모드와 30초 집계 창을 먼저 확인한다.

로그와 종료 명령:

```bash
docker compose logs --tail=100 detector prometheus grafana loki
docker compose down
```

`down`은 볼륨의 저장 내용을 지우지 않는다. 데이터 삭제가 필요하지 않으면 `--volumes` 옵션을 붙이지 않는다. 설정 파일로 제공되는 대시보드를 고치려면 `monitoring/grafana/dashboards/hycad-overview.json`을 수정한다. UI에서 임시로 만든 화면과 저장소의 기본 대시보드는 별도로 관리한다.

## 2. Docker 없이 탐지기 실행

Python 3.12 이상 환경을 사용한다. 고정된 NumPy 의존성이 Python 3.12 이상을 요구한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-monitor.txt
python -m hycad_monitor --help
python -m hycad_monitor \
  --mode replay \
  --baseline cache/cat_baseline.pkl \
  --replay cache/cat_baseline.pkl cache/cat_1of4.pkl cache/cat_4of4.pkl \
  --interval 5 --loop \
  --bind 127.0.0.1 --port 9108 \
  --deployment catalogue
```

이 명령은 탐지기만 실행한다. Prometheus·Grafana·Loki는 별도 실행이 필요하다. Compose의 `detector`와 동시에 같은 포트로 실행하지 않는다. 분석 패널로 설명 로그를 보내려면 `--loki-url http://127.0.0.1:3100/loki/api/v1/push`를 추가한다. 기본값을 환경변수로 지정할 때는 `HYCAD_LOKI_URL`을 사용한다. 로컬 판정 기록도 필요하면 쓰기 가능한 경로를 `--journal`로 지정한다.

```bash
mkdir -p work
python -m hycad_monitor \
  --mode replay --baseline cache/cat_baseline.pkl \
  --replay cache/cat_1of4.pkl \
  --interval 5 --journal work/replay-decisions.jsonl
```

유한 replay가 끝나도 HTTP 확인을 위해 프로세스는 대기하며, 마지막 결과는 신선도 한도 이후 사용할 수 없게 된다. 종료는 `Ctrl+C`다. 판정 기록은 자동으로 크기를 제한하거나 회전하지 않는다. 보관 크기를 관리하고, 새 기록 파일을 사용할 때는 프로세스를 새 경로로 다시 시작한다.

## 3. live 입력 준비

수집 대상 Linux 환경에서 Sysdig 또는 기존 수집기가 **한 줄에 한 이벤트**를 기록해야 한다. 이 모니터 실행만으로 커널 이벤트 수집이 시작되지는 않는다. live 입력 파일은 압축하지 않은 UTF-8 NDJSON이며 줄이 완료될 때 개행을 기록한다. 지원 형태는 평평한 이벤트 객체와 `{"output_fields": { ... }}` 객체다.

모니터는 처음 파일을 열 때 끝으로 이동하고 이후 추가된 줄을 처리한다. 기존 파일 내용을 처음부터 처리하는 배치 명령이 아니다. 파일 교체 또는 축소를 감지한 뒤에는 새 내용을 처음부터 읽는다. 기본 설정에서 현재보다 5초 넘게 미래이거나 90초 넘게 오래된 이벤트는 유효한 live 입력으로 사용하지 않는다.

| 필드 | 의미 |
|---|---|
| `evt.rawtime` | Unix epoch 기준 나노초 정수. 수집 호스트와 모니터의 시계를 맞춘다. |
| `container.name` 또는 `k8s.pod.name` | 실제 레플리카 구분자. Kubernetes에서는 Pod 이름을 사용한다. |
| `evt.type`, `evt.category` | 시스템콜과 범주 |
| `fd.directory`, `fd.filename`, `fd.type` | 파일 관련 특성 |
| `proc.name`, `proc.cmdline`, `proc.cwd`, `proc.exe`, `proc.args` | 프로세스 관련 특성 |

필드 추출과 숫자 제거 등 정규화는 기존 전처리 정의를 사용한다. 서로 다른 레플리카가 같은 이름으로 합쳐지지 않는지 먼저 확인한다. 원본 이벤트를 다른 형식에서 변환할 경우 시각·구분자·시스템콜을 보존해야 한다.

정상 기준선은 현재 서비스와 이미지, 수집 정책으로 얻은 **확인된 정상 구간**에서 만든다. 기존 전처리의 입력은 평평한 JSON 객체가 들어 있는 `jsonl.gz`다.

```bash
python preprocess.py /path/to/normal.jsonl.gz /path/to/hycad-data/baseline.pkl
```

`preprocess.py`는 `output_fields` 래퍼를 직접 펼치지 않는다. 수집 형식이 래퍼형이면 별도로 평평한 입력을 준비한 뒤 전처리한다. 결과 파일은 신뢰할 수 있는 원본에서 생성한 것만 사용한다. pickle 파일을 읽는 과정은 안전하지 않은 외부 파일에 사용하면 안 된다.

기준선과 함께 원본 경로·해시, 실제 수집 시간, 이미지 식별자, 레플리카 목록, 수집 필터, 이벤트 방향·성공 여부 정책, 코드 버전을 보관한다. `cache/cat_baseline.pkl`은 로컬 데모용 과거 연구 자료다. 운영 기준선이 아직 없다면 live 준비가 끝난 것으로 간주하지 않는다.

## 4. live 탐지기 실행

### 4.1 Linux의 파일을 직접 읽는 구성

직접 실행하는 경우:

```bash
python -m hycad_monitor \
  --mode live \
  --baseline /path/to/hycad-data/baseline.pkl \
  --events /path/to/hycad-data/events.jsonl \
  --deployment catalogue \
  --bind 127.0.0.1 --port 9108
```

기본 레플리카 이름은 기준선에 저장된 값이다. 이름이 바뀌었다면 `--replicas replica-1,replica-2,replica-3,replica-4`처럼 전달한다. **정렬된 기준선 이름 순서와 인자로 전달한 순서**를 대응시킨다. 레플리카 수는 기준선과 같아야 하며 최소 2개다. 임의의 새 서비스나 다른 이미지에 이름만 대응시켜 기존 기준선을 재사용하지 않는다.

Docker 실행은 `compose.live.yaml`을 기본 구성 위에 적용한다. 실제 파일이 있는 디렉터리를 마운트해 `/data/baseline.pkl`과 `/data/events.jsonl`로 제공한다. 디렉터리 마운트를 유지해야 파일 회전 후 새 파일도 읽을 수 있다.

```bash
export HYCAD_DATA_DIR=/absolute/path/to/hycad-data
export HYCAD_DEPLOYMENT=catalogue
docker compose -f compose.yaml -f compose.live.yaml config
docker compose -f compose.yaml -f compose.live.yaml up --build -d
```

처음 실행이라면 앞의 `python3 scripts/setup_local.py`를 먼저 수행한다. `HYCAD_DATA_DIR`에는 두 파일을 담은 실제 절대 경로를 넣는다. 기본 Compose live 명령은 기준선 레플리카 이름을 그대로 사용한다. 이름을 대응시켜야 한다면 live override의 탐지기 명령에 `--replicas`를 명시한다. `docker compose config` 출력에는 환경변수로 전달한 값이 포함될 수 있으므로 그대로 공유하지 않는다.

live용 Prometheus 설정은 수집 대상에 `mode=live`를 붙인다. 이 값은 exporter가 내려갔을 때도 해당 대상이 live임을 식별하는 데 쓰이므로 탐지기 모드와 함께 바꿔야 한다. 혼합된 replay/live 설정을 임의로 재사용하지 않는다.

첫 UTC 정렬 30초 창은 시작 시점 이전 데이터가 없을 수 있어 `insufficient_data`다. 이후 모든 레플리카가 관측되고 각 레플리카의 이벤트·accept 계열 호출 수가 0보다 큰 완전한 창에서 점수를 계산한다. 트래픽이 없는 서비스에서 `insufficient_data`는 정상적인 품질 신호이며 정상 판정으로 바꾸지 않는다.

### 4.2 이 Mac에서 SSH로 잠시 관찰하는 구성

이 환경에서는 빠르게 추가되는 live 파일이 Docker bind mount 안에 반영되기까지 약 32.5초 지연되는 현상을 관측했다. 따라서 **수집 파일을 네이티브 Python 탐지기가 직접 읽고, Docker의 Prometheus가 그 지표를 수집**한다. 앞의 Linux 직접 마운트 구성은 유지하며 이 Mac에서는 다음 경로를 사용한다.

이 절은 상시 서버 배포 전에 사용한 임시 접속 경로다. 서버 상시 운영을 확인하려면 [서버 운영 가이드](server-operations.md)를 사용한다. 자동 화면 터널이 localhost 3000을 사용 중인 환경에서 이 임시 Grafana를 같은 포트로 동시에 실행하지 않는다.

사전에 Python 환경과 Grafana `.env`를 준비한다. `JUMP_HOST`와 `TARGET_HOST`는 각각 실제 `사용자@호스트`로 바꾼다. 새 SSH 세션이 필요한 경우 터미널에서 직접 로그인하여 제어 소켓을 만든다. 이미 유효한 세션이 있으면 재사용한다.

```bash
mkdir -p work data/live
ssh -M -S work/outer.sock -o ControlPersist=1h -fN JUMP_HOST
ssh -M -S work/inner.sock -o ControlPersist=1h \
  -o 'ProxyCommand=ssh -S work/outer.sock -W %h:%p JUMP_HOST' \
  -fN TARGET_HOST
```

`JUMP_HOST`는 두 곳을 모두 바꾼다. 호스트 키와 로그인은 기존 SSH 방식으로 직접 확인한다. 비밀번호를 명령 인자, 환경변수, 파일에 넣지 않는다. 연결 확인에 사용한 소켓은 `work/outer.sock`, `work/inner.sock`이며 자격 증명 자체를 저장하는 파일이 아니다.

기준선은 `data/live/baseline.pkl`에 둔다. 이번 연결 확인에서 사용한 파일은 기존 서버의 2026-09-08 자료이고, 유효한 보정 창 199개와 다음 SHA-256을 가진다.

```text
849419403c9c80e5a34de4a5cbd7ed7b7b3dccba009bfa6ac2c839a0d19555a9
```

이는 현재 환경에서 새로 검증한 정상 기준선이 아니다. 현재 입력을 처리하고 화면으로 연결하는 경로를 확인한 것으로 해석하며, 오탐률·탐지 정확도 검증으로 보고하지 않는다. `data/live/`는 Git에서 제외한다.

첫 번째 터미널에서 원격 수집 전달을 실행한다.

```bash
.venv/bin/python scripts/collect_ssh.py \
  --host TARGET_HOST --control work/inner.sock \
  --output data/live/events.jsonl \
  --prefix docker-compose_catalogue_ \
  --sudo --duration 3600 --max-mib 128
```

대상 서버에 이미 설치된 Sysdig와 `timeout`을 사용한다. `--sudo` 비밀번호는 숨김 입력으로 받고 SSH 표준입력으로만 전송한다. 원격 디스크에 캡처를 저장하거나 패키지를 설치하지 않는다. 수집은 최대 1시간이며 자동 재연결하지 않는다. 로컬의 `events.jsonl`과 `events.jsonl.1`은 각각 최대 128MiB, 합계 최대 256MiB다. 이전 `.1`은 다음 회전에 교체된다. `Ctrl+C`로 수집을 종료할 수 있고, 모니터는 종료 표시를 받아 열린 창을 불완전하게 처리한다.

두 번째 터미널에서 Mac 탐지기를 실행한다.

```bash
.venv/bin/python -m hycad_monitor \
  --mode live \
  --baseline data/live/baseline.pkl \
  --events data/live/events.jsonl \
  --deployment catalogue \
  --bind 127.0.0.1 --port 9109 \
  --loki-url http://127.0.0.1:3100/loki/api/v1/push
```

세 번째 터미널에서 컨테이너 탐지기를 중지하고 SSH 구성을 적용한다.

```bash
docker compose stop detector
docker compose -f compose.yaml -f compose.ssh.yaml up -d
```

Grafana와 Prometheus의 주소는 그대로다. 탐지기 상태와 지표는 각각 [localhost:9109/status](http://127.0.0.1:9109/status), [localhost:9109/metrics](http://127.0.0.1:9109/metrics)에서 확인한다. Prometheus 대상은 `host.docker.internal:9109`이고 `mode=live`여야 한다. 대시보드에서도 live 모드인지 확인한다. 5초는 수집·화면 갱신 간격이며 탐지는 30초 단위다.

SSH 수집이 중단되거나 원본 이벤트 시각이 다음 창으로 진행하지 않으면 판정을 유보한다. 전송이 복구된 첫 창도 불완전할 수 있어 제외한다. 수집을 마친 뒤 더 사용하지 않는 SSH 세션은 안쪽부터 닫는다.

```bash
ssh -S work/inner.sock -O exit TARGET_HOST
ssh -S work/outer.sock -O exit JUMP_HOST
```

## 5. 동작 확인

화면에 그래프가 있다는 사실만으로 live 수집이 검증되지는 않는다. 다음 순서로 확인한다.

1. `hycad` 수집 대상이 `UP`이고 화면 실행 모드가 의도한 값인지 확인한다.
2. live에서는 원본 창 종료 시각이 현재 시각을 따라오는지 확인한다. 최근 처리 시각만 갱신되는 과거 파일 재생과 구별한다.
3. 모든 예상 레플리카가 표시되고 이벤트와 accept 계열 호출 수가 집계되는지 확인한다.
4. 유효 창에서 `hycad_ready=1`이 되는지 확인한다. 입력이 지연되거나 불완전하면 `hycad_ready=0`, `insufficient_data`와 점수 미표시로 전환되어야 한다.
5. replay에서는 이상 상태가 보여도 live 이상 경보가 발화하지 않는지 확인한다. 외부 알림 수신처는 기본 구성에 없다.
6. 분석 패널의 설명이 선택한 모드·원본 30초 창과 맞는지 확인한다. 표시된 채널 점수·임계값·제한된 특성 근거로 판정 분기를 이해한다. 과거 replay 설명이나 모델 추론을 현재의 침해 확인으로 읽지 않는다.

분석 패널은 Loki에 저장한 판정 JSON을 표시한다. 설명은 실제 탐지 결과에서 생성한 한국어 요약이며 외부 언어 모델의 추측을 덧붙이지 않는다. 제한된 프로세스 이름과 시스템콜을 근거로 사용하고 원본 명령행·인자는 노출하지 않는다. 전체 이벤트 원문 조사는 별도로 보관한 수집 자료를 사용한다. native 실행에서 설명이 없다면 `--loki-url`, Loki의 `/ready`, 탐지기 로그를 확인한다.

상단의 `최근 판정 분석 · 최신 10건`에서 요약을 읽고, 하단의 `판정 근거 상세 · 행을 펼쳐 확인`에서 행을 펼쳐 JSON 근거를 확인한다. 로그 옆 시각은 처리 시각이고 원본 창은 설명 안에 따로 표시된다. 로그가 없다는 것은 설명 근거가 없다는 뜻이며 정상 판정이나 공격 판정이 아니다.

Grafana Explore의 `HyCAD Decision Logs` 데이터소스에서 live 설명을 직접 확인할 때는 다음 LogQL을 사용할 수 있다. 배포 이름을 바꿨다면 `deployment`도 바꾼다.

```logql
{job="hycad-decisions",deployment="catalogue",mode="live"}
  | json summary="explanation.summary"
  | line_format "{{.summary}}"
```

자주 쓰는 PromQL:

```promql
up{job="hycad"}
hycad_ready
hycad_verdict == 1
hycad_channel_a_score
hycad_channel_b_score
hycad_channel_c_score
hycad_window_accepts
hycad_input_errors_total
time() - hycad_window_end_timestamp_seconds{mode="live"}
```

현재 판정만 확인할 때는 `hycad_ready`와 `up`도 함께 본다. exporter가 중단되면 Prometheus가 이전 값을 잠시 보관할 수 있다. 기본 대시보드와 이상 경보는 현재 수집 성공 및 유효한 창 조건을 적용한다.

## 6. 흔한 상태와 조치

| 상태 | 확인할 내용 |
|---|---|
| `insufficient_data`가 계속 표시됨 | 입력 파일 추가 기록, 현재 시각, 레플리카 이름·개수, 각 레플리카의 accept 존재 여부를 확인한다. |
| 새 시작 직후 점수가 없음 | 첫 불완전 창은 제외한다. 다음 완전한 창까지 기다린다. |
| 입력 처리 오류가 증가함 | 줄 단위 JSON, 필드명·자료형, 파일 회전, 시간 순서, 제한 초과 여부를 로그와 판정 기록에서 확인한다. |
| exporter는 살아 있지만 결과가 오래됨 | 프로세스 생존과 수집 진행은 별개다. 수집기와 파일 갱신 상태를 확인한다. 기본 입력 신선도 한도는 90초다. |
| 이미지 변경 뒤 B/C가 변함 | 새 이미지·연결 정책·정상 부하를 확인한다. 운영 중 자동 재학습하지 않으므로 정상 변화 검증 후 기준선을 교체한다. |
| 일부 레플리카만 누락됨 | 스케일 변경·Pod 이름 교체·필터 누락을 확인한다. 예상 목록에서 임의로 빼서 정상으로 만들지 않는다. |
| A가 낮지만 B/C가 높음 | 모든 레플리카가 균일하게 바뀌는 경우가 있다. 원본 증거 없이 A만으로 정상이라고 결론 내리지 않는다. |
| replay 화면에 옛 날짜가 표시됨 | 원본 연구 캡처 시각이다. 현재 처리 시각과 구분해서 보는 정상 동작이다. |
| Mac에서 처리 지연이나 지연 이벤트가 반복됨 | Docker 파일 마운트 대신 4.2의 네이티브 탐지기와 `compose.ssh.yaml` 경로를 사용했는지 확인한다. |
| Mac 임시 SSH live가 약 1시간 뒤 중단됨 | `collect_ssh.py`의 시간 제한이다. 필요한 경우 수집 범위를 확인하고 새 세션을 실행한다. 상시 서버 모드의 자동 재연결과 구분한다. |
| `source_not_advanced` 또는 `recovery_partial_window` | 원본 이벤트 시각의 진행이 확인되지 않았거나 전송 복구 직후의 일부 창이다. 다음 완전한 창을 기다리고 수집·전송 상태를 확인한다. |
| 점수는 보이지만 분석 설명이 없음 | Loki 준비 상태, native 탐지기의 `--loki-url`, 모드·시간 필터와 로그 전송 상태를 확인한다. |

live 입력 지연 한도는 `--stale-seconds`로 조정할 수 있으며 최소 30초다. 기본 데이터 사용 불가 경보의 대기 시간 90초는 별도 Prometheus 규칙이다. 이 값을 바꿀 때는 이벤트 지연 허용 범위, 화면의 유효성, 경보 지연을 함께 검토한다.

## 7. 검증과 운영 범위

개발 의존성을 설치한 뒤 저장소의 테스트를 실행한다.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
docker compose config
```

Prometheus 규칙이나 Grafana 질의를 변경하면 실제 구성으로 지표 수집·경보·화면을 함께 확인한다. 테스트 통과, 캐시 재생, 실제 live 수집, 정확도 실험은 서로 다른 검증 단계다. 수행한 단계만 결과로 기록한다.

기본 구성은 로컬 관찰용이다. 원격 공유 배포를 할 때는 바인딩, 인증, TLS와 로그 보관 정책을 해당 환경에 맞춰 정한다. 현재 버전에는 온라인 재학습, 자동 차단, 동적 레플리카 변경, 외부 알림 발송, 원본 이벤트 검색 UI가 포함되지 않는다.
