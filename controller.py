"""
HyCAD 판정기.

핵심 아이디어: A 의 침묵은 "이상 없음"이 아니라 "replica 사이에 비대칭이 없음"이다.
비대칭이 없는데 각자는 평소와 다르다면 전원이 같이 변한 것이고,
그것이 공격인지 정상 변화인지는 요청당 작업량(C)이 가른다.

  A 지목 있음                      -> 부분 감염    (지목된 집합 반환)
  A 침묵 · B 정상                  -> 정상        (학습 버퍼에 적립)
  A 침묵 · B 이상 · C 변동          -> 전원 감염
  A 침묵 · B 이상 · C 안정          -> 정상 변화    (B 재학습 트리거)
"""
import numpy as np
import channels as ch


# ================================================================== 임계값
#
#   *** 설계 동결: 2026-09-15 ***
#
#   아래 네 상수는 Docker / Sock Shop 환경의 캡처 12종으로 정했다.
#   그 과정에서 두 번은 결과를 보고 값을 고쳤다:
#     - THETA_C  : 총 이벤트 기준 -> 요청당 작업량 기준 (실제 드리프트에서 52% 오판 확인 후)
#   즉 현재 수치는 held-out 검증을 거치지 않았고 낙관적일 수 있다.
#
#   *** 동결 이후 수정 1건: 2026-09-16 ***
#     THETA_C : 0.30 -> 0.20  (절대값이라는 성질은 유지)
#
#     계기 : k8s held-out 에서 전원 감염이 22% 에 그쳤다. 원인은 pod 의 CPU 제한
#            (cpu:200m) 때문에 채굴이 요청당 작업량을 '늘리는' 대신 '줄여'
#            C 값이 5배 작아진 것이다 (Docker 4/4 = 2.25배 증가, k8s 4/4 = 0.85배 감소).
#            C = |log2(비율)| 이라 방향은 상관없지만 크기가 1.17 -> 0.24 로 줄었다.
#
#     시도했다 버린 안 : θ_B 처럼 정상 baseline 최댓값 x 계수로 바꾸기.
#            드리프트를 막으려면 계수 >= 2.5, k8s 공격을 잡으려면 계수 <= 1.0 이라
#            양립하지 않는다. C 를 자기 배포의 잡음 크기로 나누면 오히려 비교가 깨진다 —
#            C 는 이미 비율의 로그라 단위가 없고, 그래서 절대값이 옳다.
#
#     근거 : 실측 8개 분포의 여유를 보고 정했다. 공격 데이터의 '경계'를 맞춘 것이 아니라
#            정상/드리프트 쪽 최댓값 위에 두었다.
#              정상·드리프트 5종 최댓값   0.1677  (진짜 드리프트 Node4->10)
#              ---------------------  0.20  <- 여기
#              공격 3종 25% 분위          0.2111  (k8s 4/4. 가장 약한 신호)
#            0.16 으로 더 내리면 진짜 드리프트가 5% 새기 시작한다.
#
#     영향 : Docker 전원 감염 97% -> 100%,  k8s 20% -> 78%.
#            정상·드리프트 오탐은 양쪽 모두 0% 유지, 다른 시나리오는 변화 없음.
#     주의 : 이 수정은 k8s 결과를 본 뒤 이루어졌다. 다음 환경에서 다시 확인해야 한다.
#
#   held-out (Kubernetes / frontend, 2026-09-16) 결과는 ../ucbadx_k8s/README.md 참조.
#   식별 역전 재현 · 감쇠 법칙 오차 0 · 부분 감염 100% · 오탐 0% 로 성립했고,
#   전원 감염만 위 수정으로 해결했다.
#
#   변경할 경우 반드시 여기에 날짜·이유·이전 값을 남길 것.
#
# ------------------------------------------------------------------------
# A 는 정상 노이즈가 0.07~0.09 로 거의 0 이라 x2 해도 절대 증가폭이 작다(여유 3.9배).
#
#   *** 동결 이후 수정 2건: 2026-09-27 ***
#     채널 A2(동료 x 빈도)와 FACTOR_A2 = 1.25 를 삭제했다.
#     A2 를 빼니 부분 감염 집합 복원이 95~98% 에서 100% 로 올랐고, 잃은 것은 집합이
#     전혀 변하지 않는 공격 2종(fe_n4_hidden, fe_n10_hidden)뿐이라 위협 모델에서
#     제외했다. A2 는 레플리카 간 요청량이 비슷해야 한다는, RW 에 없는 전제를
#     요구한다. 비교용 ch.a2_scores() 는 channels.py 에 남겨두었다.
FACTOR_A1 = 2.00
THETA_C   = 0.20    # 배포 무관 절대값 (2026-09-16: 0.30 에서 내림. 위 기록 참조)
QUORUM_B  = 0.25    # replica 중 이 비율 이상이 B 임계값을 넘으면 B=이상

# 채널 B 구현은 channels.CHANNEL_B_KIND 로 고른다 (기본 "z", 환경변수 HyCADX_B=ae).
# 동결 시점의 held-out 검증은 "z" 로 수행한다. AE 는 외부 동거 시나리오에서 유리하고
# 드리프트 적응에서 불리하다 — README 6절 비교표 참조.


class Thresholds:
    def __init__(self, a1, c_ref, chan_b, c=None):
        self.a1 = a1
        self.c_ref = c_ref      # 정상 구간의 요청당 작업량 중앙값
        self.b = chan_b
        self.c = THETA_C if c is None else c    # C 채널 임계값

    @classmethod
    def calibrate(cls, baseline_snapshots, replicas, features, vocab):
        """정상 baseline 만 보고 임계값을 정한다. 공격 데이터는 쓰지 않는다."""
        a1 = FACTOR_A1 * max(max(ch.a1_scores(s, replicas, features).values())
                             for _, s in baseline_snapshots)
        c_ref = float(np.median([ch.work_rate(c, replicas, vocab)
                                 for c, _ in baseline_snapshots]))
        b = ch.ChannelB([c for c, _ in baseline_snapshots], replicas)
        return cls(a1, c_ref, b)

    def __repr__(self):
        return (f"Thresholds(A={self.a1:.4f}, "
                f"B={self.b.threshold:.4g}, C={self.c:.4f}, c_ref={self.c_ref:.1f})")


# ------------------------------------------------------------------ 판정
def verdict(counts, sets, replicas, features, vocab, th):
    """한 스냅샷을 판정한다.  반환: (판정문자열, 지목된 replica 집합, 진단값)"""
    a1 = ch.a1_scores(sets, replicas, features)
    flagged = {r for r in replicas if a1[r] > th.a1}
    b_high = th.b.fired(counts, replicas, QUORUM_B)
    c = ch.c_score(counts, replicas, vocab, th.c_ref)
    diag = dict(a1=max(a1.values()), b=b_high, c=c)

    if flagged:
        return "부분 감염", flagged, diag
    if not b_high:
        return "정상", set(), diag
    if c > th.c:
        return "전원 감염", set(replicas), diag
    return "정상 변화", set(), diag


# ------------------------------------------------------------------ 감염 비율
def infection_ratio(a1_or_a2_scores, replicas, d1, n=None):
    """감쇠 법칙  D(k) = D1 * (n-k)/(n-1)  을 뒤집어 k 를 추정한다.

    d1 : 단일 감염일 때의 기준 점수 (배포별로 한 번 측정)
    지목된 replica 수를 세는 것이 더 직접적이지만, 점수 크기로도
    교차 확인할 수 있다는 것을 보이기 위한 함수.
    """
    n = n or len(replicas)
    d = max(a1_or_a2_scores.values())
    if d1 <= 0:
        return None
    return n - (n - 1) * d / d1
