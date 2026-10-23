# live — HyCAD 실시간 대시보드

판정 코드는 `../ucbadx` 를 **import 만** 한다(복사 금지). 바뀌는 것은 입력뿐이다.

| | 입력 | 도는 곳 | 쓰는 데 |
|---|---|---|---|
| `replay` | `ucbadx/cache/*.pkl` 를 시간 순으로 흘린다 | **Windows 로컬** | 전시회 시연 |
| `live` | `sysdig -j` 표준입력 | **marine-normal** | 진짜 실시간 |

둘 다 같은 `Engine` 을 쓰고, 판정은 `controller.verdict()` 를 그대로 호출한다.
그래프용 replica 별 A·B 값만 한 번 더 계산하며 그 비용은 측정에서 뺀다.

---

## 1. 로컬 리플레이 (Docker 불필요)

```bash
cd live
# 정상 -> 1대 감염 -> 전원 감염. 스냅샷당 2초 (실시간의 15배속)
PYTHONIOENCODING=utf-8 python hycad_live.py replay cat_baseline cat_1of4 cat_4of4 --interval 2

# 전시회용: 공격 투입 경계 앞뒤 12 스냅샷씩만, 5초 간격, 무한 반복 (한 바퀴 8분)
PYTHONIOENCODING=utf-8 python hycad_live.py replay \
    cat_1of4 cat_3of4 cat_4of4 recon_4of4 --limit 12 --interval 5 --loop
```

### demo 모드 — 줄거리 한 바퀴를 배속으로

`replay` 는 한 배포만 다루지만 `demo` 는 **배포를 넘나든다.** catalogue 로 공격 3종을
보여준 뒤 front-end 로 옮겨 실제 드리프트까지 간다. 배포가 바뀌면 그 배포의 정상
캡처에서 임계값을 다시 뽑는다(`Engine.rebase()`).

```bash
PYTHONIOENCODING=utf-8 python hycad_live.py demo --interval 1 --limit 10 --loop --port 9901
```

| 순서 | 배포 | 캡처 | 나오는 판정 |
|---|---|---|---|
| 1 | catalogue | cat_baseline | 정상 |
| 2 | catalogue | cat_1of4 | 정상 → **부분 감염** (1대) |
| 3 | catalogue | cat_3of4 | 정상 → **부분 감염** (3대, Jaccard 역전 구간) |
| 4 | catalogue | cat_4of4 | 정상 → **전원 감염** (A 가 꺼진다) |
| 5 | catalogue | recon_4of4 | 정상 → **전원 감염** (정찰) |
| 6 | catalogue | revsh_4of4 | 정상 → **전원 감염** (리버스 셸) |
| 7 | front-end | fe_n4_norm | 정상 (Node 4.8.0) |
| 8 | front-end | fe_n10_norm | **정상 변화** → 20칸 뒤 **재학습 1회** (Node 10.24.1) |

159 스냅샷이라 `--interval 1` 이면 **한 바퀴 2분 40초**다. `deploy` 라벨이 붙으므로
catalogue 와 front-end 의 replica 가 같은 `_1.._4` 이름이어도 시계열이 안 섞인다.

`--limit N` 은 캡처마다 **공격 투입 경계 앞뒤 N 스냅샷**만 쓴다. 정상 구간을
몇십 분씩 보여줄 수 없으니 판정이 바뀌는 지점만 잘라 쓰는 것이다.
`--interval` 은 `prometheus.yml` 의 `scrape_interval`(기본 5s)과 맞춰야
스냅샷이 빠짐없이 그래프에 찍힌다.

`--interval 30` 이면 논문과 같은 실시간 속도다. 어휘가 배포 단위로 고정돼 있어
**한 번에 한 배포의 캡처만** 섞을 수 있다(catalogue 끼리, front-end 끼리).

메트릭 확인:

```bash
curl -s http://localhost:9900/metrics | grep -v '^#'
```

## 2. 서버 라이브 (marine-normal)

`hycad_live.py` 와 `ucbadx/` 를 서버에 올린 뒤:

```bash
bash run_live.sh            # sysdig -> 판정 -> :9900/metrics
```

`cat_baseline` 의 임계값을 그대로 쓴다. 지금 떠 있는 컨테이너 이름이
`docker-compose_catalogue_1..4` 와 달라지면 `manifest.py` 의 `REPLICA_PREFIX` 를 맞출 것.

## 3. Prometheus + Grafana

**설치(MSI) 없이** zip 을 푼 것을 그대로 쓴다. `stack/` 에 이미 받아 뒀다.

```powershell
cd C:\Users\myj68\Desktop\claude\capstone\live

.\stack.ps1 demo         # 전시회용: Prometheus + Grafana + 배속 데모(9901)
.\stack.ps1 start        # Prometheus 9090 + Grafana 3000 만
.\stack.ps1 replay       # 리플레이 익스포터(9900)까지 같이
.\stack.ps1 status       # 지금 뭐가 돌고 있나 + 자원 사용량
.\stack.ps1 stop         # 전부 종료
.\stack.ps1 backfill     # 캡처를 다시 구워 넣기
```

창을 닫아도 프로세스는 계속 돈다. 끄려면 `.\stack.ps1 stop` 을 쳐야 한다.
서비스로 등록하지 않았으므로 재부팅하면 알아서 없어진다.
실측 자원 사용량은 넷을 합쳐 **RAM 343 MB · CPU 0.15%**(14코어 기준)다.

대시보드 두 개다. 둘 다 로그인 없이 보인다.

| 주소 | 보여주는 것 | job | 포트 |
|---|---|---|---|
| `/d/hycad-live` | 백필된 실험 전체 5.1시간 (+ replay 꼬리) | `hycad` | 9900 |
| `/d/hycad-demo` | 배속 데모, 한 바퀴 2분 40초 | `hycad-demo` | 9901 |

패널 질의에 `job` 선택자가 박혀 있어 두 대시보드가 서로의 데이터를 안 끌어온다.

데이터소스와 대시보드는 `grafana-v11.6.0/conf/provisioning/` 으로 자동 등록되므로
Import 를 누를 일이 없다. 다른 Grafana 에 옮길 때만 `grafana_hycad.json` 을 Import 한다.

둘 다 **127.0.0.1 에만** 바인딩한다(방화벽 경고 회피). 다른 기기에서 봐야 하면
`conf/custom.ini` 의 `http_addr` 과 `--web.listen-address` 를 `0.0.0.0` 으로 바꾼다.

## 4. 백필 — 실시간을 기다리지 않는 방법

전시회에서 30분짜리 공격이 끝나기를 기다릴 수는 없다. 캡처를 통째로 Prometheus 에
구워 넣으면 타임라인을 스크럽하면서 설명할 수 있다.

```bash
python backfill.py                       # stack/openmetrics.txt 생성
promtool tsdb create-blocks-from openmetrics stack/openmetrics.txt stack/data
```

`hycad_live.Engine` 을 그대로 돌려서 얻은 판정이라 라이브 익스포터와 같은 시계열이
들어간다. 기본 시나리오는 **공격 3종 + 드리프트 2종**을 5분 간격으로 이어 붙인 약
5.1시간(528 스냅샷 / 샘플 25,344개)이다.

| 배포 | 캡처 | 정답 |
|---|---|---|
| catalogue | cat_baseline (60칸) | 정상 |
| catalogue | cat_1of4 · cat_3of4 | 부분 감염 |
| catalogue | cat_4of4 · recon_4of4 · revsh_4of4 | 전원 감염 |
| catalogue | cat_drift_workload | 정상 (실패한 드리프트 시도) |
| front-end | fe_n4_norm | 정상 (Node 4.8.0) |
| front-end | fe_n10_norm | **정상 변화** (실제 드리프트, 재학습 1회) |

백필을 돌리면 **대시보드 `hycad-live` 의 기본 시간 범위가 데이터 구간으로 고정된다.**
`now-6h` 같은 상대 범위로 두면 시간이 지날수록 오른쪽이 비고, 결국 창 밖으로 밀려난다.
특히 `지금 흐르는 입력` 패널은 instant 질의라 **범위 끝 시각**으로 평가되므로
그 시각에 데이터가 없으면 통째로 빈칸이 된다.

> `.\stack.ps1 backfill` 은 `stack/data` 를 지우고 다시 만든다.
> 그때까지 쌓인 **데모·리플레이 기록도 같이 지워진다**(고정된 백필만 남는다).

세 가지 함정:

- **시각을 "지금"을 끝으로 당겨 붙인다.** 실제 캡처 날짜(9월 8일 등)를 쓰면
  Prometheus 기본 보존기간 15일에 걸려 블록이 통째로 지워진다. 실제 날짜는
  `hycad_source_info` 의 `captured` 라벨로 남긴다.
- **줄바꿈이 LF 여야 한다.** Windows 기본 CRLF 로 쓰면
  `invalid metric type "gauge\r"` 로 죽는다.
- **스크레이프 라벨(`job`·`instance`·`env`)을 백필에도 똑같이 넣는다.** 안 넣으면
  Prometheus 가 백필과 라이브를 서로 다른 시계열로 보고 그래프가 두 줄로 갈라진다.
  넣으면 과거 3.8시간 뒤에 실시간이 그대로 이어진다.

---

## 메트릭

| 이름 | 종류 | 뜻 |
|---|---|---|
| `hycad_verdict_code` | gauge | 0 정상 · 1 정상 변화 · 2 부분 감염 · 3 전원 감염 |
| `hycad_verdict{state}` | gauge | 같은 것을 one-hot 으로 |
| `hycad_a1_score{replica}` / `_ratio` | gauge | 채널 A 원값 / θ_A 대비 배수 |
| `hycad_b_score{replica}` / `_ratio` | gauge | 채널 B 원값 / θ_B 대비 배수 |
| `hycad_b_crossed` | gauge | B 임계값을 넘은 replica 수 |
| `hycad_c_score`, `hycad_work_rate` | gauge | 채널 C, 요청당 작업량 w |
| `hycad_threshold{channel}` | gauge | θ_A · θ_B · θ_C |
| `hycad_flagged{replica}` | gauge | A 가 지목하면 1 |
| `hycad_scoring_seconds` | gauge | `controller.verdict()` 한 번의 시간 |
| `hycad_retrains_total` | counter | 채널 B 재학습 횟수 |
| `hycad_oov_events_total` | counter | 어휘에 없는 syscall (라이브 전용) |
| `hycad_source_info{...}` | gauge | 지금 흐르는 캡처와 정답 라벨 |

## 대시보드 설계 메모

- **B 는 로그축이어야 한다.** z 점수가 정상 1 내외에서 공격 때 10³~10⁴ 까지 뛴다.
  baseline 에 한 번도 안 나온 syscall 이 `sd = max(std, 1e-5)` 바닥값에 걸려
  등장만 해도 z = 구성비/1e-5 가 되기 때문이다(catalogue 어휘 82차원 중 68개가 미등장).
  선형축으로 그리면 정상 구간이 바닥에 붙어 아무것도 안 보인다.
- **A 와 B 는 배수(`_ratio`)로 그린다.** 배포마다 θ 가 달라 원값끼리는 못 비교한다.
  1.0 이 임계선이다.
- **replica 색은 Okabe-Ito 4색**(`#0072B2 #D55E00 #009E73 #CC79A7`)이고,
  전 쌍을 protan·deutan·tritan 으로 검증했다. 녹↔분홍 deutan ΔE 7.6 한 쌍만
  목표치 8 아래라 **선 종류(실선·파선·점선·일점쇄선)로 2차 부호화**를 걸어 두었다.
  `replica 별 지목` 패널이 색에 기대지 않는 같은 정보를 한 번 더 보여준다.
- **스크레이프 15초, 스냅샷 30초.** 같은 값이 두 번 찍혀 계단이 된다. 30초로 맞추면
  경계가 어긋났을 때 스냅샷을 통째로 놓칠 수 있어 일부러 더 자주 긁는다.

## 확인한 것 (2026-10-23)

**논문 표 1 (기존 기법과의 비교, 공격 3종 평균) 재현** — 5행 전부 일치한다.
논문의 정의 그대로 썼다: 탐지 = 공격 구간 중 경보를 울린 비율, 오탐 = 정상 구간 중
경보를 울린 비율, 식별 = 지목이 실제 감염된 쪽과 하나도 틀리지 않은 비율.

| | 크립토마이닝 | 정찰 | 리버스 셸 | 3종 평균 | 논문 |
|---|---|---|---|---|---|
| 부분 감염(1~3/4) 탐지 | 100% | 100% | 100% | **100%** | 100% |
| 전원 감염(4/4) 탐지 | 100% | 100% | 100% | **100%** | 100% |
| 과반(3/4) 감염 식별 | 100% | 100% | 100% | **100%** | 100% |
| 정상 변화 오탐 | — | — | — | **1%** | 1% |
| 정상 구간 오탐 | — | — | — | **0%** | 0% |

- 4행은 front-end Node 4.8.0 ↔ 10.24.1 양방향이다. 업그레이드 0.0% / 롤백 1.7% → 평균 1%.
- 5행은 **정상 캡처 5종**(cat_baseline · cat_drift_workload · cat_drift_version ·
  fe_n4_norm · fe_n10_norm, 399 스냅샷) 기준이다. 공격 캡처의 앞쪽 정상 구간으로 재면
  정찰에서 10% 가 나오는데, 이는 투입 시각을 `docker exec` 루프 **뒤에** 기록해 몇 초가
  정상 라벨에 섞인 전이 스냅샷 문제다 (`../ucbadx/README.md` 8절).
- 2행에서 리버스 셸 4/4 만 "전원 감염"으로 **정확히** 판정한 비율이 94% 다. 나머지 6% 는
  "부분 감염"이라 경보는 울렸고, 논문의 탐지 정의로는 100% 다.

**스택** — Prometheus 3.15.1 / Grafana 11.6.0. 백필 샘플 25,344개(시계열 22종)가
블록 3개로 들어갔고 대시보드 2개 × 12패널이 프로비저닝으로 올라간다.
배속 데모는 159 스냅샷을 1초 간격으로 돌며 네 판정과 재학습 1회가 모두 나온다.

**리플레이**

| 캡처 | 판정 | 지목 | A/θ_A | B/θ_B | C/θ_C |
|---|---|---|---|---|---|
| `cat_1of4` | 부분 감염 | 1 | 7.83 | 7.3 | 2.82 |
| `cat_3of4` | 부분 감염 | 1,2,3 | 2.66 | 7.2 | 7.35 |
| `cat_4of4` | 전원 감염 | 전원 | 0.36 | 6.9 | 6.64 |
| `recon_4of4` | 전원 감염 | 전원 | 0.20 | 1191.7 | 6.30 |
| `cat_drift_workload` | 정상 | — | 0.00 | 0.2 | 0.28 |

판정 비용 중앙값 **0.18 ms**(논문 0.28 ms와 같은 자리수, 측정 기계가 다름).
HTTP 스크레이프까지 포함해 58 스냅샷 재생 중 판정 전이가 그대로 나왔다.

## 한계

- 리플레이는 **이미 찍어 둔 캡처**를 흘리는 것이다. "실시간 탐지"가 아니라
  "실시간처럼 재생"이라고 말해야 맞다. 전시회에서 그렇게 설명할 것.
- 라이브 모드는 **아직 서버에서 돌려보지 않았다.** 로컬에서 검증한 것은
  리플레이 경로뿐이고, 라이브 경로는 같은 `Engine` 을 쓰지만 sysdig 파싱·
  스냅샷 경계 처리는 미검증이다.
- 임계값은 `cat_baseline`(2026-09-08 캡처)에서 뽑는다. 서버 상태가 그때와
  달라졌으면 정상 구간부터 다시 떠서 baseline 을 갱신해야 한다.
