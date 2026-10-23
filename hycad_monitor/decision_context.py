"""Flat display context for an existing decision; never changes model results."""
from __future__ import annotations

import re

LABELS = {"normal": "정상", "partial_infection": "부분 이상 의심",
          "all_infection": "전체 이상 의심", "drift": "정상 변화", "insufficient_data": "판정 불가"}


def short_replica(value):
    value = " ".join(str(value).split())[:160]
    return re.sub(r"^docker-compose_(.+)_(\d+)$", r"\1 \2", value)


def names(values):
    return ", ".join(short_replica(value) for value in sorted(values)) or "없음"


def decision_context(result, replica=None):
    """Prefer stored quorum metadata, allowing old archived decisions to display."""
    thresholds = result.get("thresholds", {})
    a_scores, b_scores = result.get("a_scores", {}), result.get("b_scores", {})
    replicas = sorted(set(a_scores) | set(b_scores) | set(result.get("replicas", [])) | set(result.get("flagged", [])))
    a_hits = [r for r, score in a_scores.items() if score is not None and score > thresholds.get("a", float("inf"))]
    b_hits = [r for r, score in b_scores.items() if score is not None and score > thresholds.get("b", float("inf"))]
    explanation = result.get("explanation", {})
    quorum = explanation.get("b_quorum", {})
    total = quorum.get("total", len(replicas))
    # Do not guess historical quorum when archived metadata is absent.
    required = quorum.get("required_hits")
    quorum_text = f"{len(b_hits)}/{total} 초과" + (f" · 필요 {required}/{total}" if required is not None else " · 필요 수 기록 없음")
    c, threshold = result.get("c_score"), thresholds.get("c")
    c_text = "C 기록 없음"
    if c is not None and threshold is not None:
        c_text = f"C={c:.4g} {'>' if c > threshold else '≤'} {threshold:.4g} · accept 정규화 이벤트량 변화"
    status = result.get("status")
    if status == "partial_infection":
        rule = "A 초과 레플리카 우선 지목 · B/C는 참고값"
    elif status == "all_infection":
        rule = "A 지목 없음 + B 정족수 충족 + C 초과 → 전체 지목; 각 레플리카의 개별 초과를 뜻하지 않음"
    elif status == "drift":
        rule = "A 지목 없음 + B 정족수 충족 + C 이내 → 정상 변화"
    elif status == "normal":
        rule = "A 지목 없음 + B 정족수 미달 → 정상"
    else:
        rule = "입력 품질 조건 미충족 · 판정 유보"
    triggers = []
    if replica in a_hits: triggers.append("A 초과")
    if replica in b_hits: triggers.append("B 초과")
    trigger = " · ".join(triggers)
    if not trigger:
        trigger = "전체 규칙에 포함 · 개별 초과 없음" if status == "all_infection" and replica in result.get("flagged", []) else "개별 초과 없음"
    return {"model_status_label": LABELS.get(status, "미확인"),
            "a_exceeded_replicas": names(a_hits), "b_exceeded_replicas": names(b_hits),
            "current_replica_trigger": trigger, "b_quorum": quorum_text,
            "c_evidence": c_text, "rule_summary": rule}
