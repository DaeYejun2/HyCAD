"""
HyCAD 네 채널.

A1  동료 x 집합 x 비대칭   누가 남들에게 없는 원소를 갖고 있나
A2  동료 x 빈도 x 비대칭   누가 남들보다 많이 호출하나
B   자기 x 빈도            평소의 자기와 다른가  (z점수 / 오토인코더 교체 가능)
C   요청당 작업량          요청 하나에 드는 일이 변했나

비교를 위해 참고 구현도 포함:
RW   동료 x 집합 x 대칭 (ReplicaWatcher 원본)
TV   동료 x 빈도 x 대칭 (대칭 지표가 빈도 축에서도 역전하는지 확인용)
"""
import os
import numpy as np

# 간헐적으로 출몰하는 런타임 스레드. 집합 특성에서 제외하면 RW 오탐이 사라진다.
# (Node 4 의 V8 워커 스레드. 제외하지 않으면 정상 상태에서 80% 오탐)
NOISE_THREADS = {"V WorkerThread", "V WorkerThread server.js"}

ACCEPT_SYSCALLS = {"accept4", "accept"}


# ---------------------------------------------------------------- 집합 축
def _clean(feature, values, strip_threads):
    s = set(values)
    if strip_threads and feature in ("procs", "commands"):
        s -= NOISE_THREADS
    return s


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _containment(a, b):
    """|a \\ b| / |a| — 비대칭. a 가 가진 것 중 b 에 없는 비율."""
    return 0.0 if not a else len(a - b) / len(a)


def rw_scores(snapshot_sets, replicas, features, strip_threads=True):
    """ReplicaWatcher 원본. 대칭 Jaccard. replica별 점수 dict 반환."""
    S = {r: {f: _clean(f, snapshot_sets[r].get(f, []), strip_threads) for f in features}
         for r in replicas}
    return {i: float(np.linalg.norm(
        [np.mean([1 - _jaccard(S[i][f], S[j][f]) for j in replicas if j != i]) for f in features]))
        for i in replicas}


def a1_scores(snapshot_sets, replicas, features, strip_threads=True):
    """A1. 포함관계 기반 비대칭. replica별 점수 dict."""
    S = {r: {f: _clean(f, snapshot_sets[r].get(f, []), strip_threads) for f in features}
         for r in replicas}
    return {i: float(np.linalg.norm(
        [np.mean([_containment(S[i][f], S[j][f]) for j in replicas if j != i]) for f in features]))
        for i in replicas}


# ---------------------------------------------------------------- 빈도 축
def tv_scores(counts, replicas):
    """대칭 TV 거리. 비교용 — 과반 감염에서 역전한다."""
    p = {r: counts[r] / counts[r].sum() for r in replicas}
    return {i: float(np.mean([0.5 * np.abs(p[i] - p[j]).sum() for j in replicas if j != i]))
            for i in replicas}


def a2_scores(counts, replicas):
    """A2. 호출 수 초과분 비율. 비대칭."""
    return {i: float(np.mean([np.maximum(counts[i] - counts[j], 0).sum() / counts[i].sum()
                              for j in replicas if j != i]))
            for i in replicas}


# ---------------------------------------------------------------- 자기 참조
class _ChannelBBase:
    """자기 참조 축의 공통 뼈대.

    입력은 스냅샷마다의 syscall 구성비 벡터 p = counts / counts.sum() 이다.
    하위 클래스는 score_one() 만 구현하면 된다.
    임계값은 정상 버퍼에서 나온 점수의 최댓값으로 잡는다 (두 구현 공통).
    """

    def score_one(self, v):
        raise NotImplementedError

    def scores(self, counts, replicas):
        return {r: self.score_one(counts[r]) for r in replicas}

    def fired(self, counts, replicas, quorum=0.25):
        """replica 중 quorum 이상이 임계값을 넘으면 True."""
        hits = [self.score_one(counts[r]) > self.threshold for r in replicas]
        return float(np.mean(hits)) >= quorum - 1e-9


class ChannelBZ(_ChannelBBase):
    """정상 기준 구성비 대비 차원별 z 점수의 최댓값. 학습 없음.

    설계 동결(2026-09-15) 시점의 기본 구현. k8s held-out 검증은 이것으로 수행한다.
    """

    def __init__(self, buffer_counts, replicas):
        P = np.array([c[r] / c[r].sum() for c in buffer_counts for r in replicas])
        self.mu = P.mean(0)
        self.sd = np.maximum(P.std(0), 1e-5)
        self.threshold = max(self.score_one(c[r]) for c in buffer_counts for r in replicas)

    def score_one(self, v):
        return float(np.abs((v / v.sum() - self.mu) / self.sd).max())


class ChannelBAE(_ChannelBBase):
    """잡음 제거 오토인코더. 재구성 오차(MSE)를 이상 점수로 쓴다.

    입력은 구성비를 정상 버퍼의 mu/sd 로 표준화한 벡터다.
    표준화하지 않으면 read/write(비율 0.4)가 손실을 독점하고
    희소 syscall(1e-4)의 변화가 묻힌다.

    구조는 차원 d 에 맞춰 자동으로 잡는다:  d -> d/2 -> d/4 -> d/2 -> d
    (catalogue d=14 -> 14-7-3-7-14,  front-end d=31 -> 31-15-7-15-31)

    과적합 주의:  버퍼가 수백 행뿐이라 AE 가 항등함수를 외우면
    학습 오차가 0 에 수렴해 임계값이 0 이 되고 전부 오탐이 된다.
    막는 장치 세 가지 —
      (1) 병목을 d/4 로 좁힌다
      (2) 학습 중 입력에 가우시안 잡음을 섞는다 (denoising)
      (3) weight decay + 고정 epoch 수
    """

    EPOCHS   = 400
    LR       = 1e-3
    NOISE    = 0.1     # 표준화 공간에서의 잡음 크기
    DECAY    = 1e-4
    SEED     = 0

    def __init__(self, buffer_counts, replicas):
        import torch, torch.nn as nn

        P = np.array([c[r] / c[r].sum() for c in buffer_counts for r in replicas])
        self.mu = P.mean(0)
        self.sd = np.maximum(P.std(0), 1e-5)
        X = (P - self.mu) / self.sd

        d = X.shape[1]
        h = max(4, d // 2)
        z = max(2, d // 4)

        torch.manual_seed(self.SEED)
        self.net = nn.Sequential(
            nn.Linear(d, h), nn.ReLU(),
            nn.Linear(h, z), nn.ReLU(),
            nn.Linear(z, h), nn.ReLU(),
            nn.Linear(h, d),
        )
        xt = torch.tensor(X, dtype=torch.float32)
        opt = torch.optim.Adam(self.net.parameters(), lr=self.LR, weight_decay=self.DECAY)
        lossf = nn.MSELoss()
        self.net.train()
        for _ in range(self.EPOCHS):
            opt.zero_grad()
            noisy = xt + self.NOISE * torch.randn_like(xt)
            lossf(self.net(noisy), xt).backward()
            opt.step()
        self.net.eval()
        self._torch = torch

        self.threshold = max(self.score_one(c[r]) for c in buffer_counts for r in replicas)

    def score_one(self, v):
        torch = self._torch
        x = (v / v.sum() - self.mu) / self.sd
        xt = torch.tensor(x, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            return float(((self.net(xt) - xt) ** 2).mean())


# 어느 구현을 쓸지.  환경변수 HyCADX_B=ae 로도 바꿀 수 있다.
#   "z"  : z 점수 (기본값, 설계 동결 시점 구현, 의존성 numpy 만)
#   "ae" : 오토인코더 (torch 필요)
CHANNEL_B_KIND = os.environ.get("HyCADX_B", "z").lower()

_B_IMPL = {"z": ChannelBZ, "zscore": ChannelBZ, "ae": ChannelBAE}


def ChannelB(buffer_counts, replicas, kind=None):
    """채널 B 팩토리.  기존 호출부는 그대로 두면 된다."""
    k = (kind or CHANNEL_B_KIND)
    if k not in _B_IMPL:
        raise ValueError(f"CHANNEL_B_KIND 는 {list(_B_IMPL)} 중 하나여야 한다: {k!r}")
    return _B_IMPL[k](buffer_counts, replicas)


# ---------------------------------------------------------------- 작업률
def work_rate(counts, replicas, vocab):
    """요청당 이벤트 수 = 전체 이벤트 / accept 수.

    총 이벤트 수를 그대로 쓰면 실제 드리프트(이벤트 -9%)와 정상 변동(±6.6%)이
    겹쳐 52% 오판한다. 요청 수로 나누면 분리된다.
    """
    idx = [i for i, k in enumerate(vocab) if k in ACCEPT_SYSCALLS]
    total = sum(counts[r].sum() for r in replicas)
    acc = sum(counts[r][idx].sum() for r in replicas)
    return total / max(acc, 1)


def c_score(counts, replicas, vocab, reference):
    return abs(np.log2(work_rate(counts, replicas, vocab) / reference))
