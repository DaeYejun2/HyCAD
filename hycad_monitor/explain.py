"""Deterministic, local explanations of an already computed HyCAD decision.

Only process names, event type names, event categories and fd types may appear as
raw examples. Command lines, arguments and all path-bearing fields stay private.
"""
from __future__ import annotations

import math

import numpy as np

import channels as ch
import controller as ctl

EXAMPLE_FEATURES = frozenset({"procs", "syscalls", "category", "file_ops"})
MAX_EXAMPLES = 3
MAX_EXAMPLE_LENGTH = 160
NOTICE = "판정 근거이며 실제 침해 원인 확정 아님."
FEATURE_LABELS = {
    "syscalls": "이벤트 유형", "category": "이벤트 분류", "directories": "디렉터리",
    "filenames": "파일명", "file_ops": "파일 종류", "procs": "프로세스",
    "commands": "명령행", "cwds": "작업 디렉터리", "args": "인자", "exe": "실행파일 경로",
}
REASON_TEXT = {
    "source_not_advanced": "입력 이벤트 시각이 다음 구간으로 진행하지 않아 전체 구간의 수집 완료를 확인할 수 없음",
    "recovery_partial_window": "입력 복구 후 첫 구간이므로 구간 시작부터 수집했는지 확인할 수 없음",
    "startup_partial_window": "수집 시작 직후의 첫 구간이므로 전체 30초를 관측하지 못함",
    "source_reset": "입력 파일이 교체되거나 잘려 수집 연속성을 확인할 수 없음",
    "source_timestamp": "입력 이벤트 시각이 허용된 지연 또는 시계 범위를 벗어남",
    "missing_replicas": "일부 대상 레플리카의 데이터가 없음",
    "unexpected_replicas": "설정된 비교 대상에 없는 레플리카가 포함됨",
    "zero_events": "일부 레플리카의 이벤트 수가 0임",
    "zero_accepts": "일부 레플리카의 accept 계열 이벤트가 없어 작업량 비율을 평가할 수 없음",
    "late_event": "이미 닫힌 구간의 이벤트가 늦게 도착해 수집 연속성을 확인할 수 없음",
    "invalid_event": "필수 필드가 없거나 형식이 잘못된 이벤트가 포함됨",
    "malformed_event": "입력 이벤트를 해석할 수 없음",
    "feature_value_limit": "특성 원소 수 또는 문자열 길이 제한을 넘어 일부 값을 보존하지 못함",
    "syscall_vocabulary_limit": "이벤트 유형 차원 수 제한을 초과함",
    "capture_tail_duration_unknown": "저장된 캡처의 마지막 구간으로 실제 종료 시각을 확인할 수 없음",
    "invalid_window_timestamp": "구간의 시작 또는 종료 시각이 유효하지 않음",
    "window_duration_mismatch": "입력 구간 길이가 기준선의 30초와 다름",
    "nonfinite_model_output": "유효하지 않은 모델 수치가 발생함",
    "invalid_snapshot": "입력 스냅샷 형식이 올바르지 않음",
    "invalid_syscall_counts": "이벤트 유형별 빈도 값이 유효하지 않음",
    "invalid_feature_sets": "특성 집합 형식이 올바르지 않음",
    "incomplete_window": "전체 구간의 수집 완료를 확인할 수 없음",
    "input_error": "입력 오류로 수집 연속성을 확인할 수 없음",
    "worker_failure": "탐지 작업에 오류가 발생함",
}


def _text(value: str) -> str:
    # Control characters cannot inject extra display lines. Truncation happens
    # after whitespace normalization, and every example has the same hard cap.
    printable = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(printable.split())[:MAX_EXAMPLE_LENGTH]


def _number(value: float) -> str:
    return format(float(value), ".4g")


def _replica_evidence(snapshot: dict, result: dict, detector) -> dict:
    replicas, features = detector.replicas, detector.features
    cleaned = {rep: {feature: ch._clean(feature, snapshot["sets"][rep].get(feature, []), True)
                     for feature in features} for rep in replicas}
    output = {}
    for rep in replicas:
        details = {}
        peers = [peer for peer in replicas if peer != rep]
        for feature in features:
            own = cleaned[rep][feature]
            differences = {peer: own - cleaned[peer][feature] for peer in peers}
            per_peer = {}
            for peer in peers:
                missing = differences[peer]
                peer_evidence = {
                    "absent_count": len(missing),
                    "containment": 0.0 if not own else len(missing) / len(own),
                }
                if feature in EXAMPLE_FEATURES:
                    peer_evidence["examples"] = [_text(value) for value in sorted(missing)[:MAX_EXAMPLES]]
                per_peer[peer] = peer_evidence
            mean = float(np.mean([row["containment"] for row in per_peer.values()]))
            feature_evidence = {
                "label": FEATURE_LABELS.get(feature, feature),
                "own_value_count": len(own),
                "mean_containment": mean,
                "squared_contribution": mean**2,
                "per_peer": per_peer,
                "examples_disclosed": feature in EXAMPLE_FEATURES,
            }
            if feature in EXAMPLE_FEATURES:
                # Union of pairwise differences: a value can contribute even if
                # another peer also has it. Do not substitute own-minus-peer-union.
                absent_somewhere = set().union(*differences.values())
                feature_evidence["examples_absent_from_some_peers"] = [
                    {"value": _text(value), "absent_from": [peer for peer in peers if value in differences[peer]]}
                    for value in sorted(absent_somewhere)[:MAX_EXAMPLES]
                ]
            details[feature] = feature_evidence
        top = sorted(features, key=lambda feature: (-details[feature]["mean_containment"], feature))
        output[rep] = {
            "a_score": float(result["a_scores"][rep]),
            "a_score_recomputed": float(np.linalg.norm([details[feature]["mean_containment"] for feature in features])),
            "a_threshold": float(result["thresholds"]["a"]),
            "a_exceeds_threshold": bool(result["a_scores"][rep] > result["thresholds"]["a"]),
            "flagged": rep in result["flagged"],
            "features": details,
            "top_features": [feature for feature in top if details[feature]["mean_containment"] > 0][:3],
        }
    return output


def _b_evidence(snapshot: dict, result: dict, detector) -> dict:
    model = detector._thresholds.b
    if not hasattr(model, "mu") or not hasattr(model, "sd"):
        return {}
    if len(model.mu) != len(detector.vocab) or len(model.sd) != len(detector.vocab):
        return {}
    output = {}
    for rep in detector.replicas:
        counts = np.array([snapshot["counts"][rep].get(name, 0) for name in detector.vocab], dtype=float)
        proportions = counts / counts.sum()
        signed = (proportions - model.mu) / model.sd
        ranking = sorted(range(len(detector.vocab)), key=lambda index: (-abs(signed[index]), detector.vocab[index]))
        output[rep] = {
            "score": float(result["b_scores"][rep]),
            "threshold": float(result["thresholds"]["b"]),
            "exceeds_threshold": bool(result["b_scores"][rep] > result["thresholds"]["b"]),
            "top_event_types": [
                {
                    "event_type": _text(detector.vocab[index]),
                    "count": int(counts[index]),
                    "observed_proportion": float(proportions[index]),
                    "baseline_mean": float(model.mu[index]),
                    "baseline_sd": float(model.sd[index]),
                    "signed_z": float(signed[index]),
                    "absolute_z": float(abs(signed[index])),
                }
                for index in ranking[:3]
            ],
        }
    return output


def _short_differences(rep: str, evidence: dict) -> str:
    parts = []
    for feature in ("procs", "syscalls", "category", "file_ops"):
        examples = evidence[rep]["features"].get(feature, {}).get("examples_absent_from_some_peers", [])
        if examples:
            parts.append(f"{FEATURE_LABELS[feature]} {', '.join(row['value'] for row in examples)}")
        if len(parts) >= 2:
            break
    if parts:
        return "동료별 비교에서 차이가 난 " + "; ".join(parts)
    top = evidence[rep]["top_features"]
    if top:
        labels = ", ".join(FEATURE_LABELS.get(feature, feature) for feature in top)
        return f"{labels} 특성 집합에 차이가 있음(원문 비공개)"
    return "특성별 차이는 아래 수치 참조"


def explain(snapshot: dict, result: dict, detector) -> dict:
    """Explain the supplied result without making network calls or changing it."""
    source = snapshot if isinstance(snapshot, dict) else {}
    output = {
        "summary": "", "decision_path": [], "replica_evidence": {}, "b_evidence": {},
        "source_window": {"start": result.get("window_start", source.get("start")),
                          "end": result.get("window_end", source.get("end"))},
        "notice": NOTICE,
    }
    status = result.get("status")
    if status not in ("normal", "partial_infection", "all_infection", "drift"):
        reason = result.get("reason", "")
        description = REASON_TEXT.get(reason, "입력의 완전성 또는 유효성을 확인할 수 없음")
        output["summary"] = f"판정 유보: {description}. {NOTICE}"
        output["decision_path"] = [f"입력 품질 검사: {description}.", "A/B/C 판정을 유보함."]
        return output

    replica_evidence = _replica_evidence(snapshot, result, detector)
    output["replica_evidence"] = replica_evidence
    output["b_evidence"] = _b_evidence(snapshot, result, detector)
    n = len(detector.replicas)
    a_threshold = result["thresholds"]["a"]
    a_hits = sum(row["a_exceeds_threshold"] for row in replica_evidence.values())
    b_hits = sum(result["b_scores"][rep] > result["thresholds"]["b"] for rep in detector.replicas)
    b_fired = b_hits / n >= ctl.QUORUM_B - 1e-9
    c, c_threshold = float(result["c_score"]), float(result["thresholds"]["c"])
    c_changed = c > c_threshold
    output["b_quorum"] = {
        "hits": b_hits, "total": n, "required_fraction": ctl.QUORUM_B,
        "required_hits": math.ceil(n * (ctl.QUORUM_B - 1e-9)), "fired": b_fired,
    }
    output["c_evidence"] = {
        "score": c, "threshold": c_threshold, "exceeds_threshold": c_changed,
        "work_rate": result["work_rate"], "baseline_work_rate": float(detector._thresholds.c_ref),
        "definition": "accept 계열 이벤트 수로 정규화한 커널 이벤트량의 기준선 대비 변화; HTTP 요청 수를 직접 측정하지 않음",
    }
    b_text = (f"B: {b_hits}/{n} 레플리카가 임계값 {_number(result['thresholds']['b'])} 초과"
              f"(정족수 {ctl.QUORUM_B:.0%} {'충족' if b_fired else '미달'})")
    c_text = f"C={_number(c)} {'>' if c_changed else '≤'} {_number(c_threshold)}"
    if status == "partial_infection":
        flagged = sorted(result["flagged"])
        details = [f"{_text(rep)} A={_number(result['a_scores'][rep])} > {_number(a_threshold)}; "
                   + _short_differences(rep, replica_evidence) for rep in flagged[:3]]
        if len(flagged) > 3:
            details.append(f"외 {len(flagged) - 3}개 레플리카")
        output["summary"] = ("부분 이상 의심 판정: " + " / ".join(details)
                             + f". A 우선 적용; {b_text}, {c_text}는 참고값. {NOTICE}")
        output["decision_path"] = [
            f"A: {a_hits}/{n} 레플리카가 임계값 {_number(a_threshold)}를 초과함.",
            "부분 이상 의심 규칙을 우선 적용함. B/C 이상은 이 판정의 필요조건이 아님.",
            f"참고값 — {b_text}; {c_text}.",
        ]
    else:
        a_text = f"A: 모든 레플리카가 임계값 {_number(a_threshold)} 이내임"
        if status == "normal":
            output["summary"] = f"정상 판정: A 지목 없음, B 정족수 미달({b_hits}/{n}). {c_text}는 참고값. {NOTICE}"
            output["decision_path"] = [a_text + ".", b_text + ".", "A 지목 없음과 B 정족수 미달로 정상 규칙에 해당함.",
                                       c_text + "는 이 분기의 필요조건이 아님."]
        elif status == "all_infection":
            output["summary"] = f"전체 이상 의심 판정: A 지목 없음; {b_text}; {c_text}. {NOTICE}"
            output["decision_path"] = [a_text + ".", b_text + ".", c_text + ".", "B 정족수 충족과 C 임계값 초과로 전체 이상 의심 규칙에 해당함."]
        else:
            output["summary"] = f"정상 변화 판정: A 지목 없음; {b_text}; {c_text}. {NOTICE}"
            output["decision_path"] = [a_text + ".", b_text + ".", c_text + ".", "B 정족수 충족과 C 임계값 이내 조건으로 정상 변화 규칙에 해당함."]
    return output
