"""
평가 절용 표 두 개.

  표 1  정면 비교   ReplicaWatcher vs HyCAD   (같은 캡처 · 같은 지표)
  표 2  절제 실험   채널을 하나씩 켜면서 무엇이 달라지는가

공정성을 위해 지킨 것
  - RW 와 HyCAD 가 같은 전처리를 거친 같은 스냅샷을 본다 (NOISE_THREADS 포함)
  - 임계값 산출 규칙이 동일하다: 정상 baseline 최댓값 x 계수
    RW 는 집합 축이므로 A1 과 같은 계수 2.00 을 쓴다
  - 두 시스템 모두 공격 데이터를 임계값 결정에 쓰지 않는다

사용:
  python compare.py          # 표 1 + 표 2
  python compare.py t1
  python compare.py t2
"""
import sys
import numpy as np
import channels as ch
import controller as ctl
from evaluate import load, seg
from manifest import CAPTURES, BASELINE, by_key

ALARM = ("부분 감염", "전원 감염")      # 실제로 경보를 울리는 판정


# ------------------------------------------------------------ 임계값 보정
def calibrate(deploy):
    """배포 하나의 정상 baseline 으로 RW · HyCAD 임계값을 함께 뽑는다."""
    B = load(BASELINE[deploy])
    base = seg(B, "normal") or seg(B, "attack")
    th = ctl.Thresholds.calibrate(base, B["reps"], B["feats"], B["vocab"])
    rw = ctl.FACTOR_A1 * max(max(ch.rw_scores(s, B["reps"], B["feats"]).values())
                             for _, s in base)
    return B, th, rw


def rows_of(key):
    D = load(key)
    which = "attack" if any(l == "attack" for l, _, _ in D["rows"]) else "normal"
    return D, seg(D, which)


# =================================================================== 표 1
def table1():
    cal = {d: calibrate(d) for d in BASELINE}
    print("\n" + "=" * 96)
    print("표 1  정면 비교 — ReplicaWatcher vs HyCAD")
    print("=" * 96)
    for d, (_, th, rw) in cal.items():
        print(f"  [{d}]  θ_RW={rw:.4f}   θ_A={th.a1:.4f}  "
              f"θ_B={th.b.threshold:.4g}  θ_C={th.c:.4f}")
    print()
    print(f"{'시나리오':22}{'정답':>10}│{'탐지':>8}{'지목':>8}{'집합':>8}"
          f" │{'탐지':>8}{'지목':>8}{'집합':>8}")
    print(f"{'':22}{'':>10}│{'--- ReplicaWatcher ---':>24} │{'------- HyCAD -------':>24}")
    print('-' * 92)

    for cap in CAPTURES:
        D, rows = rows_of(cap["key"])
        R, F = D["reps"], D["feats"]
        _, th, rw_th = cal[cap["deploy"]]
        truth = {R[i] for i in cap["infected"]}
        attack = bool(truth)

        rw_det = rw_top = rw_set_ok = x_det = x_top = x_set_ok = 0
        for c, s in rows:
            rws = ch.rw_scores(s, R, F)
            a1 = ch.a1_scores(s, R, F)
            rw_set = {r for r in R if rws[r] > rw_th}
            v, x_set, _ = ctl.verdict(c, s, R, F, D["vocab"], th)
            rw_det += bool(rw_set)
            x_det += (v in ALARM)
            rw_set_ok += (rw_set == truth)
            x_set_ok += (x_set == truth)
            if attack:
                # 지목 = 점수 1위가 감염 replica 인가. 전원 감염이면 항상 참이라 제외
                rw_top += (max(rws, key=rws.get) in truth)
                x_top += (max(a1, key=a1.get) in truth)
        n = len(rows)
        f = lambda x: f"{100*x/n:.0f}%"
        full = attack and truth == set(R)      # 전원 감염이면 '지목'이 자명하다
        top = lambda x: ("자명" if full else f(x)) if attack else "—"
        print(f"{cap['key']:22}{cap['label']:>10}│{f(rw_det):>8}{top(rw_top):>8}"
              f"{f(rw_set_ok) if attack else '—':>8} │{f(x_det):>8}{top(x_top):>8}"
              f"{f(x_set_ok) if attack else '—':>8}")
    print("-" * 92)
    print("  탐지 = 경보를 울린 스냅샷 비율.  정상 캡처(정답=정상)에서는 이 값이 곧 오탐율")
    print("  지목 = 점수 1위가 실제 감염 replica 인 비율.  전원 감염은 자명하므로 생략")
    print("  집합 = 임계값을 넘은 replica 집합이 정답 집합과 정확히 일치한 비율")
    print("         RW 는 대칭 지표라 감염 1개일 때도 4개 전부가 임계값을 넘는다 -> 0%")


# =================================================================== 표 2
def ab_verdict(cfg, c, s, R, F, vocab, th):
    """절제 구성별 판정.  cfg 에 들어간 채널만 쓴다."""
    flagged = set()
    if "A" in cfg:
        flagged |= {r for r in R if ch.a1_scores(s, R, F)[r] > th.a1}
    if flagged:
        return "부분 감염", flagged
    if "B" not in cfg:
        return "정상", set()
    if not th.b.fired(c, R, ctl.QUORUM_B):
        return "정상", set()
    if "C" not in cfg:
        # C 가 없으면 '전원이 같이 변했다'를 공격과 정상 변화로 가를 수 없다.
        # 가장 관대하게 잡아 경보 쪽으로 보낸다.
        return "전원 감염", set(R)
    if ch.c_score(c, R, vocab, th.c_ref) > th.c:
        return "전원 감염", set(R)
    return "정상 변화", set()


GROUPS = [
    ("부분 감염(신원 노출)", ["cat_1of4", "cat_2of4", "cat_3of4"],        "정답률"),
    ("부분 감염(신원 은닉)", ["fe_n4_hidden", "fe_n10_hidden"],           "정답률"),
    ("전원 감염",           ["cat_4of4", "cat_external"],                "정답률"),
    ("정상 오탐",           ["cat_baseline", "fe_n4_norm", "fe_n10_norm",
                            "cat_drift_workload", "cat_drift_version"],  "오탐율"),
]

CONFIGS = [("A만", {"A"}), ("A+B", {"A", "B"}), ("전체(A+B+C)", {"A", "B", "C"})]


def table2():
    cal = {d: calibrate(d) for d in BASELINE}
    # 실제 드리프트: Node 10 정상 데이터를 Node 4 기준선으로 본다
    n4_th = cal["front-end-node4"][1]
    Dd, drift_rows = rows_of("fe_n10_norm")

    print("\n" + "=" * 96)
    print("표 2  절제 실험 — 채널을 하나씩 켜면 무엇이 달라지는가")
    print("=" * 96)
    hdr = f"{'구성':12}"
    for nm, _, kind in GROUPS:
        hdr += f"{nm:>22}"
    hdr += f"{'실제 드리프트 오탐':>20}"
    print(hdr)
    print("-" * 96)

    for cname, cfg in CONFIGS:
        line = f"{cname:12}"
        for gname, keys, kind in GROUPS:
            hit = tot = 0
            for k in keys:
                cap = by_key(k)
                D, rows = rows_of(k)
                R, F = D["reps"], D["feats"]
                th = cal[cap["deploy"]][1]
                for c, s in rows:
                    v, _ = ab_verdict(cfg, c, s, R, F, D["vocab"], th)
                    tot += 1
                    hit += (v in ALARM) if kind == "오탐율" else (v == cap["label"])
            line += f"{100*hit/tot:21.0f}%"
        # 실제 드리프트 (Node4 기준선으로 Node10 정상을 본다)
        hit = sum(ab_verdict(cfg, c, s, Dd["reps"], Dd["feats"], Dd["vocab"], n4_th)[0] in ALARM
                  for c, s in drift_rows)
        line += f"{100*hit/len(drift_rows):19.0f}%"
        print(line)
    print("-" * 96)
    print("  정답률 = 판정이 정답 라벨과 일치한 비율 (높을수록 좋음)")
    print("  오탐율 = 정상인데 경보를 울린 비율 (낮을수록 좋음)")
    print("  실제 드리프트 = Node 10 정상 데이터를 Node 4 기준선으로 평가. 재학습 전 상태")


if __name__ == "__main__":
    w = sys.argv[1] if len(sys.argv) > 1 else "all"
    if w in ("all", "t1"): table1()
    if w in ("all", "t2"): table2()
