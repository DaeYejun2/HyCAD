"""
sysdig jsonl.gz  ->  (스냅샷, replica) 단위 집계 pickle

캡처 형식은 rw/tools/capture2.sh 가 만드는 것과 같다:
  sysdig -j -p'*%evt.rawtime %container.name %evt.type %evt.category
               %fd.directory %fd.filename %fd.type
               %proc.name %proc.cmdline %proc.cwd %proc.exe %proc.args'

출력 pickle 구조:
  sets  : {(snapshot, replica): {feature: [원소, ...]}}   RW 10개 집합 특성
  freq  : {(snapshot, replica): {syscall: 횟수}}          빈도 벡터용
  t0    : 첫 이벤트의 rawtime (ns).  스냅샷 경계 계산 기준
  feats : 특성 이름 10개
  n     : 파싱한 이벤트 수

사용:
  python preprocess.py <입력.jsonl.gz> <출력.pkl>
  python preprocess.py --all            manifest 의 캡처 전부를 cache/ 에 생성
"""
import gzip, json, pickle, re, sys, os
from collections import defaultdict, Counter

TAU_NS = 30_000_000_000          # 스냅샷 길이 30초 (논문 값)
DIGITS = re.compile(r"\d")       # RW 전처리: 문자열에서 숫자 제거

FEATURES = ["syscalls", "category", "directories", "filenames", "file_ops",
            "procs", "commands", "cwds", "args", "exe"]

# sysdig 필드 -> 특성 이름, 숫자 제거 여부
FIELD_MAP = [
    ("evt.category", "category",    False),
    ("fd.directory", "directories", True),
    ("fd.filename",  "filenames",   True),
    ("fd.type",      "file_ops",    False),
    ("proc.name",    "procs",       True),
    ("proc.cmdline", "commands",    True),
    ("proc.cwd",     "cwds",        True),
    ("proc.args",    "args",        True),
    ("proc.exe",     "exe",         True),
]


# replica 식별자로 쓸 필드.
#   Docker/Compose : container.name  (docker-compose_catalogue_1 처럼 replica 마다 다름)
#   Kubernetes     : k8s.pod.name    (container.name 은 pod 안 컨테이너 이름이라 4개가 모두 같다)
REPLICA_FIELD_CANDIDATES = ["k8s.pod.name", "container.name"]


def pick_replica_field(sample):
    """첫 줄을 보고 replica 식별자로 쓸 필드를 고른다."""
    for f in REPLICA_FIELD_CANDIDATES:
        if sample.get(f):
            return f
    return "container.name"


def aggregate(src, out, replica_prefix=None, replica_field=None):
    sets = defaultdict(lambda: defaultdict(set))
    freq = defaultdict(Counter)
    t0 = None
    n = 0
    with gzip.open(src, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if replica_field is None:
                replica_field = pick_replica_field(e)
            rep = e.get(replica_field)
            if replica_prefix and not (rep or "").startswith(replica_prefix):
                continue
            ts = e.get("evt.rawtime")
            if ts is None:
                continue
            n += 1
            if t0 is None:
                t0 = ts
            key = ((ts - t0) // TAU_NS, rep)
            S = sets[key]
            et = e.get("evt.type")
            S["syscalls"].add(et)
            freq[key][et] += 1
            for field, name, strip in FIELD_MAP:
                v = e.get(field)
                if v:
                    S[name].add(DIGITS.sub("", v) if strip else v)
    data = dict(
        sets={k: {a: sorted(b) for a, b in v.items()} for k, v in sets.items()},
        freq={k: dict(v) for k, v in freq.items()},
        t0=t0, feats=FEATURES, n=n, replica_field=replica_field,
    )
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    pickle.dump(data, open(out, "wb"))
    snaps = max(k[0] for k in sets) + 1
    reps = len({k[1] for k in sets})
    print(f"  {os.path.basename(src)}: {n:,} events / {snaps} snapshots / "
          f"{reps} replicas (식별자={replica_field}) -> {out}")
    return data


def cache_path(key):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", key + ".pkl")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--all":
        from manifest import CAPTURES, ROOT, REPLICA_PREFIX
        for c in CAPTURES:
            out = cache_path(c["key"])
            if os.path.exists(out):
                print(f"  skip {c['key']} (이미 있음)")
                continue
            src = os.path.join(ROOT, c["path"])
            if not os.path.exists(src):
                print(f"  !! 없음: {src}")
                continue
            aggregate(src, out, REPLICA_PREFIX.get(c["deploy"]))
    elif len(sys.argv) == 3:
        aggregate(sys.argv[1], sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
