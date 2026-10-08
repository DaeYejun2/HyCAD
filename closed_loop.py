"""
닫힌 고리 — 동료 합의를 라벨러로 쓰는 자동 재학습.

스트림:  Node4 정상  ->  Node10 정상(실제 드리프트)  ->  Node10 + 신원은닉 채굴
전 구간 실측 캡처다.

확인하는 것:
  1) 정적 B 는 드리프트 구간 내내 오탐하고 회복하지 못하는가
  2) 닫힌 고리는 몇 스냅샷 만에 적응하는가
  3) 적응 후에도 공격을 잡는가
  4) 공격 데이터가 학습 버퍼에 섞이지 않는가   <- 가장 중요

재학습이 일어나는 조건 (세 가지 모두 필요):
  - 변화가 전 replica 에 균일해야 한다 (A 침묵)
  - 요청당 작업량 변화가 θ_C 미만이어야 한다 (C 안정)
  - 그 상태가 CAND 스냅샷 연속 유지되어야 한다
"""
import numpy as np
import channels as ch
import controller as ctl
from evaluate import load, seg

CAND = 20        # 후보 버퍼가 이만큼 차면 재학습 (20 스냅샷 = 10분)
BUF_MAX = 80


def run(stream, replicas, vocab, init_buffer, closed):
    B = ch.ChannelB(init_buffer, replicas)
    c_ref = float(np.median([ch.work_rate(c, replicas, vocab) for c in init_buffer]))
    buf = list(init_buffer)
    res = {k: [0, 0] for k in ("clean", "drift", "attack")}
    cand, retrains, poisoned, first_ok, dn = [], 0, 0, None, 0

    for lab, counts in stream:
        b_high = B.fired(counts, replicas, ctl.QUORUM_B)
        c = abs(np.log2(ch.work_rate(counts, replicas, vocab) / c_ref))
        v = "정상" if not b_high else ("전원 감염" if c > ctl.THETA_C else "정상 변화")

        res[lab][1] += 1
        res[lab][0] += (v == "정상") if lab in ("clean", "drift") else (v == "전원 감염")
        if lab == "drift":
            dn += 1
            if v == "정상" and first_ok is None:
                first_ok = dn

        if closed:
            if v == "정상":
                buf.append(counts); buf = buf[-BUF_MAX:]; cand = []
            elif v == "정상 변화":
                cand.append(counts)
                if len(cand) >= CAND:                      # 새 기준으로 재정립
                    buf = list(cand)
                    B = ch.ChannelB(buf, replicas)
                    c_ref = float(np.median([ch.work_rate(x, replicas, vocab) for x in buf]))
                    cand = []; retrains += 1
            else:                                          # 전원 감염 -> 후보 폐기
                cand = []
            if lab == "attack" and v != "전원 감염":
                poisoned += 1
    return res, poisoned, retrains, first_ok


def main():
    N  = load("fe_n4_norm")
    D  = load("fe_n10_norm")
    A  = load("fe_n10_hidden")
    reps = N["reps"]
    vocab = sorted(set(N["vocab"]) | set(D["vocab"]) | set(A["vocab"]))

    def recount(Dx, which):
        """공통 vocab 으로 다시 벡터화 (캡처마다 syscall 종류가 다르므로)."""
        out = []
        for c, _ in seg(Dx, which):
            # c 는 해당 캡처의 vocab 순서. 이름으로 다시 매핑한다.
            m = dict(zip(Dx["vocab"], next(iter(c.values()))))  # placeholder
            out.append(None)
        return out

    # 캡처별 vocab 이 다르므로 raw pickle 에서 직접 공통 vocab 으로 만든다
    import pickle
    from preprocess import cache_path
    def rows_common(key, which_label):
        d = pickle.load(open(cache_path(key), "rb"))
        ns = max(s for s, _ in d["sets"]) + 1
        out = []
        for s in range(ns):
            if any((s, r) not in d["freq"] for r in reps):
                continue
            out.append((which_label,
                        {r: np.array([d["freq"][(s, r)].get(k, 0) for k in vocab], float)
                         for r in reps}))
        return out

    clean = rows_common("fe_n4_norm", "clean")
    drift = rows_common("fe_n10_norm", "drift")
    attack = rows_common("fe_n10_hidden", "attack")

    init = [c for _, c in clean[:40]]
    stream = clean[40:] + drift + attack
    print(f"스트림(전부 실측): clean {len(clean[40:])} -> drift {len(drift)} -> attack {len(attack)}")
    print(f"초기 버퍼 {len(init)} 스냅샷, 재학습 임계 {CAND} 스냅샷\n")

    hdr = f"{'':10}{'정상 구간':>10}{'드리프트':>10}{'공격 탐지':>11}{'재학습':>8}{'공격오흡수':>11}{'적응지연':>11}"
    print(hdr)
    for closed in (False, True):
        r, p, t, f = run(stream, reps, vocab, init, closed)
        nm = "정적 B" if not closed else "HyCAD"
        pct = lambda k: 100 * r[k][0] / max(r[k][1], 1)
        print(f"{nm:10}{pct('clean'):9.0f}%{pct('drift'):9.0f}%{pct('attack'):10.0f}%"
              f"{(str(t)+'회' if closed else '-'):>8}{(str(p)+'회' if closed else '-'):>11}"
              f"{((str(f)+' 스냅샷') if (closed and f) else '-'):>11}")


if __name__ == "__main__":
    main()
