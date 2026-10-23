"""HyCAD 실시간 판정 + Prometheus 익스포터.

판정 코드는 ../ucbadx 를 import 만 한다 (복사 금지 규칙). 입력만 두 가지다.

  replay  캐시된 캡처를 "방금 들어온 스냅샷"처럼 흘린다   -> Windows 로컬에서 동작
  live    sysdig -j 출력을 표준입력으로 받는다            -> marine-normal 에서 동작

메트릭은 http://0.0.0.0:9900/metrics 에 Prometheus 텍스트 형식으로 뜬다.
의존성은 numpy 뿐 (prometheus_client 불필요).

  # 로컬 리플레이: 정상 -> 1/4 -> 4/4 를 스냅샷당 2초로
  python hycad_live.py replay cat_baseline cat_1of4 cat_4of4 --interval 2

  # 서버 라이브: 진짜 30초 스냅샷
  FMT='*%evt.rawtime %container.name %evt.type %evt.category %fd.directory %fd.filename
       %fd.type %proc.name %proc.cmdline %proc.cwd %proc.exe %proc.args'
  sudo sysdig -j -p"$FMT" "container.name contains docker-compose_catalogue_" \
    | python3 hycad_live.py live --baseline cat_baseline
"""
import argparse, json, os, sys, threading, time
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# 저장소에서는 ../ucbadx, 서버 배포본에서는 ./ucbadx 에 있다.
for _c in (os.path.join(HERE, os.pardir, "ucbadx"), os.path.join(HERE, "ucbadx")):
    if os.path.isdir(_c):
        sys.path.insert(0, _c)
        break
else:
    sys.exit("ucbadx 를 못 찾았다: " + HERE)

import numpy as np
import channels as ch
import controller as ctl
from closed_loop import CAND, BUF_MAX
from preprocess import TAU_NS, DIGITS, FIELD_MAP, FEATURES

VERDICTS = ("정상", "부분 감염", "전원 감염", "정상 변화")
CODE = {"정상": 0, "정상 변화": 1, "부분 감염": 2, "전원 감염": 3}


# ====================================================================== 판정
class Engine:
    """스냅샷 하나를 받아 판정하고 메트릭 상태를 갱신한다.

    판정 자체는 controller.verdict() 를 그대로 호출한다. 대시보드에 replica 별
    값을 그리려고 A1·B 를 한 번 더 계산하는데, 그 비용은 측정에서 제외한다.
    """

    def __init__(self, reps, feats, vocab, th, retrain=True, deploy="catalogue"):
        self.retrain = retrain
        self.n = self.retrains = self.skipped = self.oov = 0
        self.vtotal = Counter()
        self.events = Counter()
        self.ctx = {}                       # 라벨용 부가 정보 (캡처 이름, 정답 등)
        self.lock = threading.Lock()
        self.last = None
        self.rebase(reps, feats, vocab, th, deploy)

    def rebase(self, reps, feats, vocab, th, deploy):
        """배포가 바뀌면 임계값·어휘·replica 를 통째로 갈아끼운다.

        누적 카운터는 유지한다. 한 줄거리 안에서 catalogue -> front-end 처럼
        배포를 옮겨 다니는 시연에 쓴다.
        """
        self.reps, self.feats, self.vocab, self.th = reps, feats, vocab, th
        self.deploy = deploy
        self.buf, self.cand = [], []
        self.last = None

    def push(self, counts, sets, ctx=None):
        t0 = time.perf_counter()
        v, flagged, diag = ctl.verdict(counts, sets, self.reps, self.feats, self.vocab, self.th)
        cost = time.perf_counter() - t0

        a1 = ch.a1_scores(sets, self.reps, self.feats)
        b = {r: self.th.b.score_one(counts[r]) for r in self.reps}
        w = ch.work_rate(counts, self.reps, self.vocab)

        if self.retrain:
            self._learn(counts, v)

        with self.lock:
            self.n += 1
            self.vtotal[v] += 1
            for r in self.reps:
                self.events[r] += int(counts[r].sum())
            self.ctx = ctx or {}
            self.last = dict(verdict=v, flagged=flagged, a1=a1, b=b, c=diag["c"],
                             w=w, cost=cost, t=time.time())
        return v, flagged

    def _learn(self, counts, v):
        """closed_loop.run() 과 같은 규칙. A 임계값은 재학습하지 않는다."""
        if v == "정상":
            self.buf.append(counts)
            self.buf = self.buf[-BUF_MAX:]
            self.cand = []
        elif v == "정상 변화":
            self.cand.append(counts)
            if len(self.cand) >= CAND:
                self.buf = list(self.cand)
                self.th.b = ch.ChannelB(self.buf, self.reps)
                self.th.c_ref = float(np.median(
                    [ch.work_rate(x, self.reps, self.vocab) for x in self.buf]))
                self.cand = []
                self.retrains += 1
        else:
            self.cand = []

    # ------------------------------------------------------------ 메트릭
    def metrics(self):
        with self.lock:
            L, ctx = self.last, dict(self.ctx)
            n, rt, sk, oov = self.n, self.retrains, self.skipped, self.oov
            vt, ev = dict(self.vtotal), dict(self.events)
            th = self.th
        o = []

        def block(name, typ, help_, lines):
            o.append("# HELP " + name + " " + help_)
            o.append("# TYPE " + name + " " + typ)
            o.extend(lines)

        # 배포마다 replica 이름이 _1.._4 로 같아서, deploy 라벨이 없으면
        # catalogue 와 front-end 의 시계열이 한 줄로 섞인다.
        dep = 'deploy="' + self.deploy + '"'
        short = {r: r.rsplit("_", 1)[-1] for r in self.reps}
        lbl = lambda r: "{" + dep + ',replica="' + short[r] + '"}'
        D = "{" + dep + "}"

        block("hycad_up", "gauge", "익스포터 동작 여부", ["hycad_up 1"])
        block("hycad_snapshots_total", "counter", "판정한 스냅샷 수",
              [f"hycad_snapshots_total {n}"])
        block("hycad_skipped_snapshots_total", "counter",
              "replica 가 빠져 버린 스냅샷 수", [f"hycad_skipped_snapshots_total {sk}"])
        block("hycad_oov_events_total", "counter",
              "어휘에 없는 syscall 이벤트 수(라이브 전용)", [f"hycad_oov_events_total {oov}"])
        block("hycad_retrains_total", "counter", "채널 B 재학습 횟수",
              [f"hycad_retrains_total {rt}"])
        block("hycad_verdicts_total", "counter", "판정별 누적 스냅샷 수",
              [f'hycad_verdicts_total{{state="{v}"}} {vt.get(v, 0)}' for v in VERDICTS])
        block("hycad_events_total", "counter", "replica 별 누적 커널 이벤트 수",
              [f"hycad_events_total{lbl(r)} {ev.get(r, 0)}" for r in self.reps])

        block("hycad_threshold", "gauge", "채널별 임계값",
              [f'hycad_threshold{{{dep},channel="A"}} {th.a1:.6g}',
               f'hycad_threshold{{{dep},channel="B"}} {th.b.threshold:.6g}',
               f'hycad_threshold{{{dep},channel="C"}} {th.c:.6g}'])
        block("hycad_work_rate_ref", "gauge", "기준 요청당 작업량 w_ref",
              [f"hycad_work_rate_ref{D} {th.c_ref:.6g}"])

        if L is None:
            return "\n".join(o) + "\n"

        block("hycad_a1_score", "gauge", "채널 A 비대칭 집합 점수 (replica 별)",
              [f"hycad_a1_score{lbl(r)} {L['a1'][r]:.6g}" for r in self.reps])
        block("hycad_a1_ratio", "gauge", "A 점수 / 세타_A. 1 을 넘으면 지목",
              [f"hycad_a1_ratio{lbl(r)} {L['a1'][r] / th.a1:.6g}" for r in self.reps])
        block("hycad_b_score", "gauge", "채널 B 구성비 z 점수 최댓값 (replica 별)",
              [f"hycad_b_score{lbl(r)} {L['b'][r]:.6g}" for r in self.reps])
        block("hycad_b_ratio", "gauge", "B 점수 / 세타_B. 값이 10^4 까지 뛰므로 배수로 본다",
              [f"hycad_b_ratio{lbl(r)} {L['b'][r] / th.b.threshold:.6g}" for r in self.reps])
        block("hycad_b_crossed", "gauge", "B 임계값을 넘은 replica 수",
              [f"hycad_b_crossed {sum(L['b'][r] > th.b.threshold for r in self.reps)}"])
        block("hycad_c_score", "gauge", "채널 C |log2(w / w_ref)|",
              [f"hycad_c_score{D} {L['c']:.6g}"])
        block("hycad_work_rate", "gauge", "요청당 작업량 w = 총 이벤트 / accept",
              [f"hycad_work_rate{D} {L['w']:.6g}"])
        block("hycad_flagged", "gauge", "A 가 지목한 replica 는 1",
              [f"hycad_flagged{lbl(r)} {int(r in L['flagged'])}" for r in self.reps])
        block("hycad_infected_count", "gauge", "지목된 replica 수",
              [f"hycad_infected_count {len(L['flagged'])}"])
        block("hycad_verdict", "gauge", "현재 판정 (one-hot)",
              [f'hycad_verdict{{state="{v}"}} {int(L["verdict"] == v)}' for v in VERDICTS])
        block("hycad_verdict_code", "gauge",
              "현재 판정 0=정상 1=정상 변화 2=부분 감염 3=전원 감염 (Grafana state timeline 용)",
              [f"hycad_verdict_code {CODE[L['verdict']]}"])
        block("hycad_scoring_seconds", "gauge",
              "controller.verdict() 한 번의 소요 시간 (논문 0.28ms)",
              [f"hycad_scoring_seconds {L['cost']:.9f}"])
        if ctx:
            tags = ",".join([dep] + [f'{k}="{v}"' for k, v in ctx.items()])
            block("hycad_source_info", "gauge", "지금 흐르는 입력 (리플레이는 정답 라벨 포함)",
                  ["hycad_source_info{" + tags + "} 1"])
        return "\n".join(o) + "\n"


# ====================================================================== HTTP
def serve(engine, port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/metrics"):
                body = engine.metrics().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"HyCAD exporter. /metrics\n")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ==================================================================== 리플레이
def run_replay(args):
    from evaluate import load, seg, deploy_vocab
    from manifest import by_key

    deploys = {by_key(k)["deploy"] for k in args.captures + [args.baseline]}
    if len(deploys) > 1:
        sys.exit("어휘가 달라진다. 같은 배포끼리만 섞을 것: " + str(deploys))
    vocab = deploy_vocab(deploys.pop())

    B = load(args.baseline, vocab)
    th = ctl.Thresholds.calibrate(seg(B, "normal"), B["reps"], B["feats"], vocab)
    eng = Engine(B["reps"], B["feats"], vocab, th, retrain=not args.no_retrain)
    print(f"기준선 {args.baseline}: {th}")
    print(f"replica {len(B['reps'])}대, 어휘 {len(vocab)}차원, 스냅샷당 {args.interval}초 재생")
    serve(eng, args.port)
    print(f"http://localhost:{args.port}/metrics\n", flush=True)

    while True:
        for key in args.captures:
            D = load(key, vocab)
            cap = D["cap"]
            for lab, counts, sets in rows_of(D, args.limit):
                v, flagged = eng.push(counts, sets,
                                      ctx=dict(capture=key, segment=lab,
                                               truth=cap["label"] if lab == "attack" else "정상"))
                mark = "" if not flagged else " -> " + ",".join(
                    sorted(r.rsplit("_", 1)[-1] for r in flagged))
                print(f"  [{eng.n:4}] {key:12} {lab:6} {v:6}{mark}", flush=True)
                time.sleep(args.interval)
        if not args.loop:
            break
        print("  --- 처음부터 다시 ---", flush=True)
    print(f"\n재생 끝. 스냅샷 {eng.n}개, 재학습 {eng.retrains}회. "
          f"Ctrl+C 로 종료 (메트릭은 계속 떠 있다)", flush=True)
    while True:
        time.sleep(3600)


# ================================================================ 데모 시나리오
# (기준선, [흘릴 캡처...]). 배포가 바뀌면 임계값을 그 배포의 정상 캡처에서 다시 뽑는다.
# 공격 3종(크립토마이닝·정찰·리버스 셸)과 실제 드리프트를 한 줄거리에 넣는다.
DEMO = [
    ("cat_baseline", ["cat_baseline", "cat_1of4", "cat_3of4", "cat_4of4",
                      "recon_4of4", "revsh_4of4"]),
    ("fe_n4_norm",   ["fe_n4_norm", "fe_n10_norm"]),
]


def rows_of(D, limit):
    """공격 투입 경계 앞뒤 limit 스냅샷만. 공격 구간이 없는 캡처는 앞에서 2*limit."""
    rows = D["rows"]
    if not limit:
        return rows
    normal = [r for r in rows if r[0] == "normal"]
    attack = [r for r in rows if r[0] == "attack"]
    if not attack:                       # 드리프트 캡처. 재학습(20칸)이 돌 만큼 길게
        return normal[:2 * limit]
    return normal[-limit:] + attack[:limit]


def run_demo(args):
    """공격 3종 + 실제 드리프트를 배속으로 한 바퀴 돌린다 (전시회용)."""
    from evaluate import load, seg, deploy_vocab
    from manifest import by_key

    groups = []
    for base, keys in DEMO:
        dep = by_key(base)["deploy"]
        vocab = deploy_vocab(dep)
        B = load(base, vocab)
        th = ctl.Thresholds.calibrate(seg(B, "normal"), B["reps"], B["feats"], vocab)
        groups.append((dep, vocab, B, th, keys))
        print(f"{dep:18} 기준선 {base:14} {th}")

    dep0, v0, B0, th0, _ = groups[0]
    eng = Engine(B0["reps"], B0["feats"], v0, th0, retrain=not args.no_retrain, deploy=dep0)
    serve(eng, args.port)
    print(f"\nhttp://localhost:{args.port}/metrics   스냅샷당 {args.interval}초\n", flush=True)

    while True:
        for dep, vocab, B, th, keys in groups:
            eng.rebase(B["reps"], B["feats"], vocab,
                       ctl.Thresholds.calibrate(seg(B, "normal"), B["reps"],
                                                B["feats"], vocab), dep)
            for key in keys:
                D = load(key, vocab)
                cap = D["cap"]
                for lab, counts, sets in rows_of(D, args.limit):
                    truth = cap["label"] if lab == "attack" else "정상"
                    if key == "fe_n10_norm":      # Node4 기준선으로 보면 드리프트다
                        truth = "정상 변화"
                    v, fl = eng.push(counts, sets,
                                     ctx=dict(capture=key, segment=lab, truth=truth))
                    mark = "" if not fl else " -> " + ",".join(
                        sorted(r.rsplit("_", 1)[-1] for r in fl))
                    print(f"  [{eng.n:4}] {dep:16}{key:14}{lab:7}{v:9}{mark}", flush=True)
                    time.sleep(args.interval)
        print(f"  --- 한 바퀴 끝 (재학습 {eng.retrains}회). 처음부터 ---", flush=True)
        if not args.loop:
            break
    while True:
        time.sleep(3600)


# ==================================================================== 라이브
def run_live(args):
    from evaluate import load, seg, deploy_vocab
    from manifest import by_key, REPLICA_PREFIX

    deploy = by_key(args.baseline)["deploy"]
    vocab = deploy_vocab(deploy)
    vidx = {k: i for i, k in enumerate(vocab)}
    B = load(args.baseline, vocab)
    reps = B["reps"]
    th = ctl.Thresholds.calibrate(seg(B, "normal"), reps, B["feats"], vocab)
    eng = Engine(reps, B["feats"], vocab, th, retrain=not args.no_retrain)
    prefix = args.prefix or REPLICA_PREFIX.get(deploy, "")
    print(f"기준선 {args.baseline}: {th}", file=sys.stderr)
    print(f"replica {reps}", file=sys.stderr)
    serve(eng, args.port)
    print(f"http://0.0.0.0:{args.port}/metrics", file=sys.stderr, flush=True)

    rset = set(reps)
    t0 = None
    cur = 0
    sets = defaultdict(lambda: defaultdict(set))
    freq = defaultdict(Counter)

    def flush(idx):
        if any(r not in freq for r in reps):
            eng.skipped += 1
        else:
            counts = {r: np.zeros(len(vocab)) for r in reps}
            for r in reps:
                for k, c in freq[r].items():
                    counts[r][vidx[k]] = c
            eng.push(counts,
                     {r: {f: sorted(sets[r][f]) for f in FEATURES} for r in reps},
                     ctx=dict(mode="live", snapshot=str(idx)))
            L = eng.last
            print(f"  [{eng.n:5}] {time.strftime('%H:%M:%S')} {L['verdict']:6} "
                  f"A={max(L['a1'].values()):.3f}/{th.a1:.3f} "
                  f"B={max(L['b'].values()):.3g}/{th.b.threshold:.3g} "
                  f"C={L['c']:.3f}/{th.c:.2f}", file=sys.stderr, flush=True)
        sets.clear()
        freq.clear()

    for line in sys.stdin:
        try:
            e = json.loads(line)
        except Exception:
            continue
        rep = e.get("container.name")
        ts = e.get("evt.rawtime")
        if ts is None or rep not in rset or (prefix and not rep.startswith(prefix)):
            continue
        if t0 is None:
            t0 = ts
            cur = 0
        idx = (ts - t0) // TAU_NS
        if idx > cur:
            flush(cur)
            cur = idx
        et = e.get("evt.type")
        S = sets[rep]
        S["syscalls"].add(et)
        if et in vidx:
            freq[rep][et] += 1
        else:
            eng.oov += 1
            freq[rep]           # 존재는 시켜 둔다 (스냅샷을 버리지 않기 위해)
        for field, name, strip in FIELD_MAP:
            v = e.get(field)
            if v:
                S[name].add(DIGITS.sub("", v) if strip else v)


# ======================================================================== CLI
def main():
    # 공통 옵션은 서브커맨드 앞뒤 어디에 써도 먹게 한다.
    # default=SUPPRESS 가 없으면 서브파서가 앞에서 받은 값을 기본값으로 덮어쓴다.
    S = argparse.SUPPRESS
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=S)
    common.add_argument("--baseline", default=S, help="임계값을 뽑을 정상 캡처")
    common.add_argument("--no-retrain", action="store_true", default=S,
                        help="정적 B (재학습 끔)")

    p = argparse.ArgumentParser(description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    r = sub.add_parser("replay", parents=[common], help="캐시된 캡처를 실시간처럼 재생")
    r.add_argument("captures", nargs="+")
    r.add_argument("--interval", type=float, default=2.0, help="스냅샷당 초 (30=실시간)")
    r.add_argument("--limit", type=int, default=0,
                   help="캡처마다 공격 투입 경계 앞뒤 N 스냅샷만 (0=전부)")
    r.add_argument("--loop", action="store_true", help="끝나면 처음부터 반복 (전시회용)")

    d = sub.add_parser("demo", parents=[common],
                       help="공격 3종 + 실제 드리프트를 배속으로 (전시회용)")
    d.add_argument("--interval", type=float, default=1.0, help="스냅샷당 초")
    d.add_argument("--limit", type=int, default=10,
                   help="캡처마다 공격 경계 앞뒤 N 스냅샷 (드리프트는 2N)")
    d.add_argument("--loop", action="store_true", help="끝나면 처음부터 반복")

    lv = sub.add_parser("live", parents=[common], help="sysdig -j 를 표준입력으로")
    lv.add_argument("--prefix", default=None, help="replica 이름 접두어")

    a = p.parse_args()
    for k, v in (("port", 9900), ("baseline", "cat_baseline"), ("no_retrain", False)):
        if not hasattr(a, k):
            setattr(a, k, v)
    {"replay": run_replay, "demo": run_demo, "live": run_live}[a.mode](a)


if __name__ == "__main__":
    main()
