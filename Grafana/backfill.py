"""캡처를 Prometheus TSDB 블록으로 구워 넣는다 (OpenMetrics 백필).

실시간 재생을 기다리지 않고 실험 전체를 타임라인에 한 번에 깔 때 쓴다.
판정은 hycad_live.Engine 을 그대로 돌려서 얻으므로, 라이브 익스포터가 내는
시계열과 똑같은 것이 들어간다.

  python backfill.py
  promtool tsdb create-blocks-from openmetrics stack/openmetrics.txt stack/data
  prometheus --storage.tsdb.path=stack/data --storage.tsdb.retention.time=1y

시각은 **지금을 끝으로 이어 붙인다.** 실제 캡처 날짜(9월 8일 등)를 쓰면 Prometheus
기본 보존기간 15일에 걸려 블록이 통째로 지워진다. 실제 날짜는 hycad_source_info 의
captured 라벨로 남는다.
"""
import argparse, json, os, sys, time
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
# 저장소에서는 ../ucbadx, 서버 배포본에서는 ./ucbadx 에 있다.
for _c in (os.path.join(HERE, os.pardir, "ucbadx"), os.path.join(HERE, "ucbadx")):
    if os.path.isdir(_c):
        sys.path.insert(0, _c)
        break
else:
    sys.exit("ucbadx 를 못 찾았다: " + HERE)

import controller as ctl
import hycad_live as HL

# (기준선, [흘릴 캡처...]). 배포가 바뀌면 임계값을 그 배포의 정상 캡처에서 다시 뽑는다.
#   catalogue : 정상 -> 크립토마이닝 1/3/4 -> 정찰 4 -> 리버스 셸 4 -> 실패한 드리프트 시도
#   front-end : 정상(Node 4.8.0) -> 실제 드리프트(Node 10.24.1)
DEFAULT = [
    ("cat_baseline", ["cat_baseline", "cat_1of4", "cat_3of4", "cat_4of4",
                      "recon_4of4", "revsh_4of4", "cat_drift_workload"]),
    ("fe_n4_norm",   ["fe_n4_norm", "fe_n10_norm"]),
]

# 스크레이프로 붙는 라벨을 백필에도 똑같이 넣는다. 안 넣으면 Prometheus 가
# 백필 시계열과 라이브 시계열을 **다른 series** 로 보고 그래프가 두 줄로 갈라진다.
SCRAPE_LABELS = 'job="hycad",instance="localhost:9900",env="replay"'


def with_job(head, name):
    if "{" in head:
        return head[:-1] + "," + SCRAPE_LABELS + "}"
    return name + "{" + SCRAPE_LABELS + "}"


def collect(groups, gap_min, end_ts, baseline_limit):
    from evaluate import load, seg, deploy_vocab
    from manifest import by_key

    # 배포별 기준선과, 흘릴 행들을 먼저 다 준비해 전체 길이를 잰다
    plan = []
    for base, keys in groups:
        dep = by_key(base)["deploy"]
        vocab = deploy_vocab(dep)
        B = load(base, vocab)
        th = ctl.Thresholds.calibrate(seg(B, "normal"), B["reps"], B["feats"], vocab)
        print(f"{dep:18} 기준선 {base:14} {th}")
        items = []
        for key in keys:
            D = load(key, vocab)
            rows = D["rows"][:baseline_limit] if key == base else D["rows"]
            items.append((key, D, rows))
        plan.append((dep, vocab, B, th, items))

    total = sum(len(r) for _, _, _, _, it in plan for _, _, r in it)
    nblocks = sum(len(it) for *_, it in plan)
    t = end_ts - (total * 30 + (nblocks - 1) * gap_min * 60)

    dep0, v0, B0, th0, _ = plan[0]
    eng = HL.Engine(B0["reps"], B0["feats"], v0, th0, deploy=dep0)

    fams, types, spans = OrderedDict(), {}, []
    for dep, vocab, B, th, items in plan:
        eng.rebase(B["reps"], B["feats"], vocab,
                   ctl.Thresholds.calibrate(seg(B, "normal"), B["reps"],
                                            B["feats"], vocab), dep)
        for key, D, rows in items:
            t_start = t
            for lab, counts, sets in rows:
                truth = D["cap"]["label"] if lab == "attack" else "정상"
                if key == "fe_n10_norm":          # Node4 기준선으로 보면 드리프트다
                    truth = "정상 변화"
                eng.push(counts, sets, ctx=dict(
                    capture=key, segment=lab, truth=truth,
                    captured=(D["cap"].get("attack_at") or "")[:10] or "-"))
                for line in eng.metrics().splitlines():
                    if line.startswith("# TYPE"):
                        _, _, name, _typ = line.split(" ", 3)
                        types[name] = "gauge"      # counter 접미사 규칙을 피한다
                        continue
                    if line.startswith("#") or not line.strip():
                        continue
                    head, _, val = line.rpartition(" ")
                    name = head.split("{", 1)[0]
                    fams.setdefault(name, []).append((with_job(head, name), val, t))
                t += 30
            spans.append((dep, key, t_start, t - 30, len(rows),
                          "정상 변화" if key == "fe_n10_norm" else D["cap"]["label"]))
            t += gap_min * 60

    return eng, fams, types, spans


def set_dashboard_window(path, t0, t1, tail_min=10):
    """대시보드 기본 시간 범위를 '실제 데이터 구간'으로 못 박는다.

    백필은 고정된 과거다. 범위를 now-6h 같은 상대값으로 두면 시간이 지날수록
    오른쪽이 비고 결국 창 밖으로 밀려난다. instant 질의를 쓰는 패널
    (지금 흐르는 입력)은 범위 끝 시각으로 평가되므로 아예 빈칸이 된다.
    """
    if not os.path.exists(path):
        return None
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
    d = json.load(open(path, encoding="utf-8"))
    d["time"] = {"from": iso(t0 - 60), "to": iso(t1 + tail_min * 60)}
    d["refresh"] = False                       # 고정 구간이라 새로고침할 것이 없다
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return d["time"]


def write_om(path, fams, types):
    # OpenMetrics 는 줄바꿈이 LF 여야 한다. Windows 기본 변환을 끈다
    # (CRLF 로 쓰면 promtool 이 invalid metric type "gauge\r" 로 죽는다)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for name, rows in fams.items():
            f.write(f"# TYPE {name} {types.get(name, 'gauge')}\n")
            for head, val, ts in rows:
                f.write(f"{head} {val} {ts:.3f}\n")
        f.write("# EOF\n")
    return sum(len(v) for v in fams.values())


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--baseline-limit", type=int, default=60,
                   help="기준선 캡처에서 쓸 스냅샷 수 (200개 전부면 100분)")
    p.add_argument("--gap", type=int, default=5, help="캡처 사이 공백 (분)")
    p.add_argument("--out", default=os.path.join(HERE, "stack", "openmetrics.txt"))
    a = p.parse_args()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    end = time.time()
    eng, fams, types, spans = collect(DEFAULT, a.gap, end, a.baseline_limit)
    n = write_om(a.out, fams, types)

    hm = lambda x: time.strftime("%H:%M", time.localtime(x))
    print(f"\n{'배포':18}{'캡처':20}{'스냅샷':>7}{'정답':>10}   구간")
    for dep, key, t0, t1, cnt, label in spans:
        print(f"{dep:18}{key:20}{cnt:>7}{label:>10}   {hm(t0)} ~ {hm(t1)}")
    print(f"\n판정 누적: {dict(eng.vtotal)}  재학습 {eng.retrains}회")
    print(f"시계열 {len(fams)}종 / 샘플 {n:,}개 -> {a.out}")

    t0, t1 = spans[0][2], spans[-1][3]
    for dash in (os.path.join(HERE, "stack", "dashboards", "hycad_live.json"),
                 os.path.join(HERE, "dashboards", "hycad_live.json")):
        w = set_dashboard_window(dash, t0, t1)
        if w:
            print(f"대시보드 기본 범위 고정: {hm(t0)} ~ {hm(t1)} (+10분)  <- {dash}")
    print(f"전체 {(t1 - t0) / 3600:.1f}시간")


if __name__ == "__main__":
    main()
