"""
문서 HyCAD_설계와검증.html 의 표들을 재생성한다.

사용:
  python preprocess.py --all      # 먼저 캡처를 cache/ 로 전처리 (10~15분)
  python evaluate.py              # 전체 표
  python evaluate.py identify     # 식별 역전 (표 3절)
  python evaluate.py attenuation  # 감쇠 법칙 (4절)
  python evaluate.py hidden       # 신원 은닉 공격 (5절)
  python evaluate.py controller   # 판단기 전 시나리오 (8절)
  python evaluate.py channelc     # C 채널 (8절)
"""
import pickle, sys, datetime as dt
import numpy as np
import channels as ch
import controller as ctl
from manifest import CAPTURES, BASELINE, by_key
from preprocess import cache_path


# ------------------------------------------------------------------ 로딩
_vocab_cache = {}


def deploy_vocab(deploy):
    """배포 안의 모든 캡처를 합친 공통 syscall 어휘.

    캡처마다 등장하는 syscall 종류가 다르므로(버전 변경 시 특히),
    벡터 차원을 맞추려면 배포 단위로 어휘를 고정해야 한다.
    """
    if deploy in _vocab_cache:
        return _vocab_cache[deploy]
    v = set()
    for c in CAPTURES:
        if c["deploy"] != deploy:
            continue
        try:
            d = pickle.load(open(cache_path(c["key"]), "rb"))
        except FileNotFoundError:
            continue
        v |= {k for f in d["freq"].values() for k in f}
    if deploy.startswith("front-end"):
        for c in CAPTURES:
            if not c["deploy"].startswith("front-end"): continue
            try: d = pickle.load(open(cache_path(c["key"]), "rb"))
            except FileNotFoundError: continue
            v |= {k for f in d["freq"].values() for k in f}
    _vocab_cache[deploy] = sorted(v)
    return _vocab_cache[deploy]


def load(key, vocab=None):
    d = pickle.load(open(cache_path(key), "rb"))
    cap = by_key(key)
    reps = sorted({r for _, r in d["sets"]})
    ns = max(s for s, _ in d["sets"]) + 1
    vocab = vocab or deploy_vocab(cap["deploy"])
    rows = []                                   # (label, counts, sets)
    if cap["attack_at"]:
        a = (dt.datetime.fromisoformat(cap["attack_at"]).timestamp() * 1e9 - d["t0"]) / 30e9
    else:
        a = None
    for s in range(ns):
        if any((s, r) not in d["sets"] for r in reps):
            continue
        counts = {r: np.array([d["freq"][(s, r)].get(k, 0) for k in vocab], float) for r in reps}
        sets = {r: d["sets"][(s, r)] for r in reps}
        if a is None:
            lab = cap.get("seg", "normal")
        elif s + 1 <= a:
            lab = "normal"
        elif s >= a + 1:
            lab = "attack"
        else:
            continue                            # 전이 스냅샷 제외
        rows.append((lab, counts, sets))
    return dict(cap=cap, reps=reps, feats=d["feats"], vocab=vocab, rows=rows)


def seg(D, which):
    return [(c, s) for lab, c, s in D["rows"] if lab == which]


# ------------------------------------------------------------------ 3절
def show_identify():
    print("\n=== 식별: 대칭 Jaccard vs 비대칭 포함관계 (catalogue) ===")
    print(f"{'감염':>6}{'Jaccard 정확도':>16}{'간격':>9}{'비대칭 정확도':>15}{'간격':>9}")
    for k, key in ((1, "cat_1of4"), (2, "cat_2of4"), (3, "cat_3of4")):
        D = load(key); R = D["reps"]; INF = {R[i] for i in D["cap"]["infected"]}
        jo, ja, do, da = [], [], [], []
        for c, s in seg(D, "attack"):
            J = ch.rw_scores(s, R, D["feats"])
            A = ch.a1_scores(s, R, D["feats"])
            for V, ok, gap in ((J, jo, ja), (A, do, da)):
                ok.append(max(V, key=V.get) in INF)
                gap.append(min(V[i] for i in INF) - max(V[i] for i in R if i not in INF))
        print(f"{k}/4{100*np.mean(jo):14.0f}%{np.mean(ja):+9.3f}"
              f"{100*np.mean(do):14.0f}%{np.mean(da):+9.3f}")

    print("\n=== 빈도 축에서도 같은 역전이 일어나는가 ===")
    print(f"{'감염':>6}{'TV(대칭)':>12}{'간격':>9}{'초과분(비대칭)':>16}{'간격':>9}")
    for k, key in ((1, "cat_1of4"), (2, "cat_2of4"), (3, "cat_3of4")):
        D = load(key); R = D["reps"]; INF = {R[i] for i in D["cap"]["infected"]}
        to, ta, eo, ea = [], [], [], []
        for c, s in seg(D, "attack"):
            T = ch.tv_scores(c, R); E = ch.a2_scores(c, R)
            for V, ok, gap in ((T, to, ta), (E, eo, ea)):
                ok.append(max(V, key=V.get) in INF)
                gap.append(min(V[i] for i in INF) - max(V[i] for i in R if i not in INF))
        print(f"{k}/4{100*np.mean(to):11.0f}%{np.mean(ta):+9.3f}"
              f"{100*np.mean(eo):15.0f}%{np.mean(ea):+9.3f}")


# ------------------------------------------------------------------ 4절
def show_attenuation():
    print("\n=== 감쇠 법칙  D(k) = D1 * (n-k)/(n-1) ===")
    print(f"{'k':>3}{'실측':>10}{'예측':>10}{'오차':>10}")
    d1 = None
    for k, key in ((1, "cat_1of4"), (2, "cat_2of4"), (3, "cat_3of4"), (4, "cat_4of4")):
        D = load(key); R = D["reps"]; INF = {R[i] for i in D["cap"]["infected"]}
        v = [ch.a1_scores(s, R, D["feats"])[i] for c, s in seg(D, "attack") for i in INF]
        m = float(np.median(v))
        if d1 is None:
            d1 = m
        pred = d1 * (4 - k) / 3
        print(f"{k:>3}{m:10.4f}{pred:10.4f}{m-pred:+10.4f}")


# ------------------------------------------------------------------ 5절
def show_hidden():
    for norm, atk, tag in (("fe_n4_norm", "fe_n4_hidden", "Node 4.8"),
                           ("fe_n10_norm", "fe_n10_hidden", "Node 10.24")):
        N, A = load(norm), load(atk)
        R = N["reps"]; F = N["feats"]; INF = {R[i] for i in A["cap"]["infected"]}
        print(f"\n=== 신원 은닉 공격 — {tag} ===")
        # 특성 집합 변화
        def union(D, which, rep):
            out = {f: set() for f in F}
            for c, s in seg(D, which):
                for f in F:
                    out[f] |= set(s[rep].get(f, []))
            return out
        un, ua = union(N, "normal", R[0]), union(A, "attack", R[0])
        print(f"  {'feature':22}{'정상':>6}{'공격':>6}   새 원소")
        for f in F:
            new = sorted(ua[f] - un[f])
            print(f"  {f:22}{len(un[f]):6}{len(ua[f]):6}   {new[:3] if new else '없음'}")
        # 탐지기 비교
        def col(D, which, fn):
            return np.array([fn(c, s) for c, s in seg(D, which)])
        fns = [("ReplicaWatcher", lambda c, s: max(ch.rw_scores(s, R, F).values())),
               ("A1 방향성(집합)", lambda c, s: max(ch.a1_scores(s, R, F).values())),
               ("TV(빈도,대칭)",   lambda c, s: max(ch.tv_scores(c, R).values())),
               ("A2 초과분(빈도)", lambda c, s: max(ch.a2_scores(c, R).values()))]
        print(f"\n  {'탐지기':18}{'정상 max':>10}{'공격 중앙':>11}{'AUC':>8}{'분리폭':>10}")
        for nm, fn in fns:
            n, a = col(N, "normal", fn), col(A, "attack", fn)
            auc = sum((x > y) + 0.5 * (x == y) for x in a for y in n) / (len(a) * len(n))
            print(f"  {nm:18}{n.max():10.4f}{np.median(a):11.4f}{auc:8.3f}{a.min()-n.max():+10.4f}")
        # 집합 복원
        th = ctl.Thresholds.calibrate(seg(N, "normal"), R, F, N["vocab"])
        ok = np.mean([{r for r in R if ch.a1_scores(s, R, F)[r] > th.a1} == INF
                      for c, s in seg(A, "attack")])
        print(f"  A 로 감염 집합 복원 {100*ok:.0f}%   (θ_A={th.a1:.4f})")
        print("  집합이 변하지 않는 공격이라 A 는 잡지 못한다. 위협 모델 밖이다.")


# ------------------------------------------------------------------ 8절
def show_controller():
    print("\n=== 판단기 — 전 시나리오 ===")
    cal = {}
    for deploy, bkey in BASELINE.items():
        B = load(bkey)
        cal[deploy] = (B, ctl.Thresholds.calibrate(seg(B, "normal") or seg(B, "attack"),
                                                   B["reps"], B["feats"], B["vocab"]))
        print(f"  [{deploy}] {cal[deploy][1]}")
    print(f"\n{'캡처':22}{'정답':>10}{'판정 정확도':>12}{'집합 복원':>10}   주 판정")
    for cap in CAPTURES:
        if cap["key"] in BASELINE.values() and cap["label"] == "정상":
            pass
        D = load(cap["key"]); R = D["reps"]; F = D["feats"]
        _, th = cal[cap["deploy"]]
        which = "attack" if any(l == "attack" for l, _, _ in D["rows"]) else "normal"
        rows = seg(D, which)
        truth = {R[i] for i in cap["infected"]}
        hits, sets_ok, dist = 0, 0, {}
        for c, s in rows:
            v, pred, _ = ctl.verdict(c, s, R, F, D["vocab"], th)
            dist[v] = dist.get(v, 0) + 1
            hits += (v == cap["label"]); sets_ok += (pred == truth)
        top = max(dist, key=dist.get)
        print(f"{cap['key']:22}{cap['label']:>10}{100*hits/len(rows):11.0f}%"
              f"{100*sets_ok/len(rows):9.0f}%   {top} {round(100*dist[top]/len(rows))}%")


def show_channel_c():
    print("\n=== C 채널 — 요청당 작업량 ===")
    ref, cth = {}, {}
    for deploy, bkey in BASELINE.items():
        B = load(bkey)
        base = seg(B, "normal") or seg(B, "attack")
        ref[deploy] = float(np.median([ch.work_rate(c, B["reps"], B["vocab"])
                                       for c, _ in base]))
        cth[deploy] = ctl.THETA_C
    for d in cth:
        print(f"  [{d}] c_ref={ref[d]:.1f}  θ_C={cth[d]:.4f}")
    print(f"{'캡처':22}{'정답':>10}{'C 중앙값':>11}{'발화율':>9}")
    for cap in CAPTURES:
        D = load(cap["key"]); R = D["reps"]
        which = "attack" if any(l == "attack" for l, _, _ in D["rows"]) else "normal"
        v = np.array([ch.c_score(c, R, D["vocab"], ref[cap["deploy"]]) for c, _ in seg(D, which)])
        print(f"{cap['key']:22}{cap['label']:>10}{np.median(v):11.4f}"
              f"{100*np.mean(v>cth[cap['deploy']]):8.0f}%")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "identify"):    show_identify()
    if what in ("all", "attenuation"): show_attenuation()
    if what in ("all", "hidden"):      show_hidden()
    if what in ("all", "channelc"):    show_channel_c()
    if what in ("all", "controller"):  show_controller()
