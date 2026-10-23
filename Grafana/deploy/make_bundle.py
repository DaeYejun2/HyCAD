"""marine-normal 로 올릴 배포 꾸러미를 만든다.

  python deploy/make_bundle.py

윈도우 바이너리(1.4GB)는 넣지 않는다. 서버가 리눅스판을 직접 받는다.
tar 는 파이썬으로 만든다 — 윈도우 tar 가 권한·줄바꿈에서 말썽을 부린다.
"""
import io, os, sys, tarfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.dirname(HERE)
ROOT = os.path.dirname(LIVE)
UCB = os.path.join(ROOT, "ucbadx")
OUT = os.path.join(HERE, "hycad-deploy.tar.gz")
TOP = "hycad-deploy"

# (꾸러미 안 경로, 원본 경로).  app/ 아래는 설치 시 $BASE 로 통째로 복사된다.
FILES = [("install.sh", os.path.join(HERE, "install.sh")),
         ("prometheus.yml", os.path.join(HERE, "prometheus.yml")),
         ("README.md", os.path.join(HERE, "README.md")),
         ("app/hycad_live.py", os.path.join(LIVE, "hycad_live.py")),
         ("app/backfill.py", os.path.join(LIVE, "backfill.py")),
         ("app/run_live.sh", os.path.join(LIVE, "run_live.sh")),
         ("app/openmetrics.txt", os.path.join(LIVE, "stack", "openmetrics.txt"))]

# 실시간 대시보드는 서버 전용이라 deploy/dashboards 에 따로 있다.
for n in ("hycad_live.json", "hycad_demo.json"):
    FILES.append((f"app/dashboards/{n}", os.path.join(LIVE, "stack", "dashboards", n)))
FILES.append(("app/dashboards/hycad_realtime.json",
              os.path.join(HERE, "dashboards", "hycad_realtime.json")))

for n in sorted(os.listdir(UCB)):
    if n.endswith(".py"):
        FILES.append((f"app/ucbadx/{n}", os.path.join(UCB, n)))

CACHE = os.path.join(UCB, "cache")
for n in sorted(os.listdir(CACHE)):
    if n.endswith(".pkl"):
        FILES.append((f"app/ucbadx/cache/{n}", os.path.join(CACHE, n)))

missing = [p for _, p in FILES if not os.path.exists(p)]
if missing:
    sys.exit("없는 파일:\n  " + "\n  ".join(missing))

EXEC = {".sh"}
now = int(time.time())
total = 0
with tarfile.open(OUT, "w:gz") as t:
    for name, path in FILES:
        data = open(path, "rb").read()
        # 셸 스크립트는 LF 로. 윈도우에서 CRLF 가 섞이면 서버에서 bad interpreter 가 난다
        if os.path.splitext(path)[1] in (".sh", ".yml", ".yaml"):
            data = data.replace(b"\r\n", b"\n")
        info = tarfile.TarInfo(f"{TOP}/{name}")
        info.size = len(data)
        info.mtime = now
        info.mode = 0o755 if os.path.splitext(name)[1] in EXEC else 0o644
        info.uname = info.gname = "marine"
        t.addfile(info, io.BytesIO(data))
        total += len(data)

print(f"{OUT}")
print(f"  파일 {len(FILES)}개 / 원본 {total/1048576:.1f} MB "
      f"-> 압축 {os.path.getsize(OUT)/1048576:.1f} MB")
print()
print("다음:")
print(f"  scp {OUT} cap-normal:~/")
print("  ssh cap-normal")
print("  tar xzf hycad-deploy.tar.gz && \\")
print("  GRAFANA_ADMIN_PASSWORD='긴비밀번호' bash hycad-deploy/install.sh")
