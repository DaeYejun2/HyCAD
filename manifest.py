"""
캡처 목록 정의.

각 항목:
  key        내부 식별자
  path       jsonl.gz 경로 (capstone 루트 기준 상대경로)
  deploy     배포 이름. 임계값 보정이 배포 단위로 이루어진다
  attack_at  공격 시작 시각 (ISO8601, UTC). None 이면 전 구간 같은 라벨
  seg        attack_at 가 None 일 때 캡처 전체의 라벨: 'normal' | 'attack'
  infected   감염된 replica 인덱스 집합 (replica 이름 정렬 순서 기준, 0부터)
  label      정답 판정. '정상' | '부분 감염' | '전원 감염' | '정상 변화'
  note       설명
"""

ROOT = r"C:\Users\myj68\Desktop\claude\capstone"

CAPTURES = [
    # ---------------- catalogue (Go, syscall 14종) ----------------
    dict(key="cat_baseline", deploy="catalogue",
         path=r"data\capture\normal_catalogue.jsonl.gz",
         attack_at=None, seg="normal", infected=set(), label="정상",
         note="2026-09-08 정상 baseline 100분 / 200 스냅샷. 임계값 보정용"),

    dict(key="cat_1of4", deploy="catalogue",
         path=r"capture_attack\attack_1of4.jsonl.gz",
         attack_at="2026-09-13T17:40:41.864412+00:00", infected={0}, label="부분 감염",
         note="XMRig 를 catalogue_1 cgroup 에 투입"),

    dict(key="cat_2of4", deploy="catalogue",
         path=r"capture_2of4\attack_2of4.jsonl.gz",
         attack_at="2026-09-14T03:31:35.642525+00:00", infected={0, 1}, label="부분 감염",
         note="replica 1,2 감염"),

    dict(key="cat_3of4", deploy="catalogue",
         path=r"capture_3of4\attack_3of4.jsonl.gz",
         attack_at="2026-09-14T04:02:38.910950+00:00", infected={0, 1, 2}, label="부분 감염",
         note="replica 1,2,3 감염. Jaccard 역전이 나타나는 구간"),

    dict(key="cat_4of4", deploy="catalogue",
         path=r"capture_44c\attack_4of4c.jsonl.gz",
         attack_at="2026-09-14T02:48:46.089771+00:00", infected={0, 1, 2, 3}, label="전원 감염",
         note="전 replica 감염. 동료 참조 축이 전멸하는 구간"),

    dict(key="cat_external", deploy="catalogue",
         path=r"capture_44\attack_4of4.jsonl.gz",
         attack_at="2026-09-13T18:37:28.104006+00:00", infected={0, 1, 2, 3}, label="전원 감염",
         note="마이너가 컨테이너 밖(호스트)에서 실행. syscall 은 귀속되지 않고 CPU 경합만 발생"),

    dict(key="cat_drift_workload", deploy="catalogue",
         path=r"capture_drift\normal_drift.jsonl.gz",
         attack_at="2026-09-14T08:38:04.688465+00:00", infected=set(), label="정상",
         note="엔드포인트 2개→5개. 드리프트 시도 1 — 실패(구성비 TV 0.0059)"),

    dict(key="cat_drift_version", deploy="catalogue",
         path=r"capture_v030\normal_catalogue.jsonl.gz",
         attack_at=None, seg="normal", infected=set(), label="정상",
         note="이미지 0.3.5→0.3.0. 드리프트 시도 2 — 실패(구성비 TV 0.0082)"),

    # ---------------- front-end (Node.js, syscall 31종) ----------------
    dict(key="fe_n4_norm", deploy="front-end-node4",
         path=r"capture_fe_norm\normal_catalogue.jsonl.gz",
         attack_at=None, seg="normal", infected=set(), label="정상",
         note="Node 4.8.0 정상. front-end 배포 임계값 보정용"),

    dict(key="fe_n4_hidden", deploy="front-end-node4",
         path=r"capture_fe_atk\normal_catalogue.jsonl.gz",
         attack_at=None, seg="attack", infected={0}, label="부분 감염",
         note="config.js 덮어쓰기로 node 프로세스 내부에서 채굴. 신원 특성 전부 불변"),

    dict(key="fe_n10_norm", deploy="front-end-node10",
         path=r"capture_fe_v10\normal_catalogue.jsonl.gz",
         attack_at=None, seg="normal", infected=set(), label="정상",
         note="Node 10.24.1 정상. 자기 배포 기준으로는 '정상'이고, "
              "fe_n4_norm(Node 4) 기준으로 보면 '정상 변화(드리프트)'다 — "
              "신규 syscall 5종, 구성비 TV 0.2046. 드리프트 평가는 closed_loop.py 에서 한다"),

    dict(key="fe_n10_hidden", deploy="front-end-node10",
         path=r"capture_fe_v10_atk\normal_catalogue.jsonl.gz",
         attack_at=None, seg="attack", infected={0}, label="부분 감염",
         note="Node 10 에서 같은 방식으로 주입. 새 syscall 0종"),

    # ---------------- catalogue / 정찰·명령 실행 (2026-09-25) ----------------
    # 페이로드: whoami, id, uname, cat /etc/*, ls /etc /root /var/log, env, ps, netstat 를
    #           1초 간격으로 반복. docker exec 로 replica 안에서 실행 -> 새 프로세스 9종 등장.
    # XMRig 와 흔적의 성격이 다르다 (CPU/메모리 대신 프로세스 생성 + 파일 읽기).
    # 각 캡처는 정상 5분 -> 공격 25분 구조다.
    dict(key="recon_1of4", deploy="catalogue",
         path="caps/recon_1of4/normal_catalogue.jsonl.gz",
         attack_at="2026-09-25T13:13:39+00:00", infected={0}, label="부분 감염",
         note="정찰 1개 (catalogue_1)"),

    dict(key="recon_2of4", deploy="catalogue",
         path="caps/recon_2of4/normal_catalogue.jsonl.gz",
         attack_at="2026-09-25T17:33:38+00:00", infected={0, 1}, label="부분 감염",
         note="정찰 2개. 대칭 지표가 동점이 되는 구간"),

    dict(key="recon_3of4", deploy="catalogue",
         path="caps/recon_3of4/normal_catalogue.jsonl.gz",
         attack_at="2026-09-25T18:04:40+00:00", infected={0, 1, 2}, label="부분 감염",
         note="정찰 3개. 대칭 지표가 역전하는 구간"),

    dict(key="recon_4of4", deploy="catalogue",
         path="caps/recon_4of4/normal_catalogue.jsonl.gz",
         attack_at="2026-09-25T18:34:43+00:00", infected={0, 1, 2, 3}, label="전원 감염",
         note="정찰 4개. 동료 축이 원리적으로 0 이 되는 지점"),
]

# 배포별 임계값 보정에 사용할 정상 캡처
BASELINE = {
    "catalogue":        "cat_baseline",
    "front-end-node4":  "fe_n4_norm",
    "front-end-node10": "fe_n10_norm",
}

# 배포별 replica 필터 접두어 (컨테이너 이름)
REPLICA_PREFIX = {
    "catalogue":        "docker-compose_catalogue_",
    "front-end-node4":  "docker-compose_front-end_",
    "front-end-node10": "docker-compose_front-end_",
}


def by_key(k):
    for c in CAPTURES:
        if c["key"] == k:
            return c
    raise KeyError(k)
