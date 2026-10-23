import copy
import json
import pickle
from pathlib import Path

import pytest

from hycad_monitor.engine import Detector, load_cache_snapshots
from hycad_monitor.explain import NOTICE, explain
from preprocess import FEATURES


def fixture_detector(path, replicas=("a", "b")):
    cache = {"t0": 0, "feats": list(FEATURES), "freq": {}, "sets": {}}
    for index in range(3):
        for rep in replicas:
            cache["freq"][(index, rep)] = {"accept4": 10, "read": 90}
            cache["sets"][(index, rep)] = {"procs": ["server"], "syscalls": ["accept4", "read"]}
    path.write_bytes(pickle.dumps(cache))
    return Detector(path), load_cache_snapshots(path)[1][0]


def test_real_cat_1of4_names_xmrig_and_matches_exact_a_contributions():
    root = Path(__file__).resolve().parents[1]
    detector = Detector(root / "cache/cat_baseline.pkl")
    snapshot = load_cache_snapshots(root / "cache/cat_1of4.pkl")[1][-2]
    result = detector.evaluate(snapshot)
    details = explain(snapshot, result, detector)
    assert result["status"] == "partial_infection"
    assert result["flagged"] == ["docker-compose_catalogue_1"]
    assert "xmrig" in details["summary"]
    assert "A 우선 적용" in details["summary"]
    assert NOTICE in details["summary"]
    for rep in detector.replicas:
        evidence = details["replica_evidence"][rep]
        assert evidence["a_score_recomputed"] == pytest.approx(result["a_scores"][rep])
        assert set(evidence["features"]) == set(FEATURES)


def test_a_examples_are_union_of_pairwise_differences_not_unique_across_all_peers(tmp_path):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl", ("a", "b", "c"))
    snapshot["sets"]["a"]["procs"].append("shared-subset")
    snapshot["sets"]["b"]["procs"].append("shared-subset")
    result = detector.evaluate(snapshot)
    feature = explain(snapshot, result, detector)["replica_evidence"]["a"]["features"]["procs"]
    assert feature["mean_containment"] == pytest.approx(0.25)
    assert feature["per_peer"]["b"]["containment"] == 0
    assert feature["per_peer"]["c"]["containment"] == pytest.approx(0.5)
    assert feature["examples_absent_from_some_peers"] == [{"value": "shared-subset", "absent_from": ["c"]}]


def test_sensitive_fields_have_scores_but_never_disclose_raw_values(tmp_path):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl")
    secrets = {feature: f"TOP_SECRET_{feature}_TOKEN/path?password=hidden" for feature in
               ("commands", "args", "exe", "directories", "filenames", "cwds")}
    for feature, secret in secrets.items():
        snapshot["sets"]["a"][feature] = [secret]
    result = detector.evaluate(snapshot)
    details = explain(snapshot, result, detector)
    serialized = json.dumps(details, ensure_ascii=False)
    for feature, secret in secrets.items():
        assert secret not in serialized
        evidence = details["replica_evidence"]["a"]["features"][feature]
        assert evidence["mean_containment"] > 0
        assert evidence["examples_disclosed"] is False
        assert "examples_absent_from_some_peers" not in evidence
        assert "examples" not in evidence["per_peer"]["b"]
    assert "원문 비공개" in details["summary"]


def test_noise_threads_match_original_cleaning_and_explanations_do_not_mutate_inputs(tmp_path):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl")
    snapshot["sets"]["a"]["procs"].append("V WorkerThread")
    snapshot["sets"]["a"]["commands"] = ["V WorkerThread server.js"]
    result = detector.evaluate(snapshot)
    saved = copy.deepcopy((snapshot, result, detector.vocab))
    details = explain(snapshot, result, detector)
    assert details["replica_evidence"]["a"]["a_score_recomputed"] == 0
    assert (snapshot, result, detector.vocab) == saved
    assert details == explain(snapshot, result, detector)


def test_b_dimension_evidence_uses_actual_baseline_mu_sd_and_threshold(tmp_path):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl")
    for rep in detector.replicas:
        snapshot["counts"][rep] = {"accept4": 10, "read": 80, "new_call": 10}
        snapshot["sets"][rep]["syscalls"].append("new_call")
    result = detector.evaluate(snapshot)
    details = explain(snapshot, result, detector)
    assert result["status"] == "drift"
    top = details["b_evidence"]["a"]["top_event_types"][0]
    assert top["event_type"] == "new_call"
    assert top["observed_proportion"] == pytest.approx(0.1)
    assert top["baseline_mean"] == 0
    assert top["baseline_sd"] == pytest.approx(1e-5)
    assert top["absolute_z"] == pytest.approx(result["b_scores"]["a"])
    assert details["b_evidence"]["a"]["threshold"] == result["thresholds"]["b"]
    assert details["b_quorum"]["fired"] is True
    assert "정상 변화 판정" in details["summary"]


def test_partial_infection_a_precedes_normal_b_and_c(tmp_path):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl")
    snapshot["sets"]["a"]["procs"].append("new-process")
    result = detector.evaluate(snapshot)
    details = explain(snapshot, result, detector)
    assert details["b_quorum"]["fired"] is False
    assert details["c_evidence"]["exceeds_threshold"] is False
    assert "부분 이상 의심" in details["summary"]
    assert "필요조건이 아님" in " ".join(details["decision_path"])


@pytest.mark.parametrize("status,read_count,summary", [("normal", 90, "정상 판정"),
                                                       ("all_infection", 900, "전체 이상 의심 판정")])
def test_normal_and_all_infection_decision_paths(tmp_path, status, read_count, summary):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl")
    for counts in snapshot["counts"].values():
        counts["read"] = read_count
    result = detector.evaluate(snapshot)
    assert result["status"] == status
    details = explain(snapshot, result, detector)
    assert summary in details["summary"]
    assert details["source_window"] == {"start": snapshot["start"], "end": snapshot["end"]}
    if status == "all_infection":
        assert details["b_quorum"]["fired"]
        assert details["c_evidence"]["exceeds_threshold"]


@pytest.mark.parametrize("reason,korean", [("source_not_advanced", "수집 완료"),
                                         ("recovery_partial_window", "복구 후 첫 구간"),
                                         ("startup_partial_window", "수집 시작"),
                                         ("source_reset", "교체되거나 잘려"),
                                         ("zero_accepts", "accept"),
                                         ("missing_replicas", "일부 대상 레플리카")])
def test_insufficient_data_explains_reason_without_stale_scores(tmp_path, reason, korean):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl")
    snapshot.update(complete=False, reason=reason)
    result = detector.evaluate(snapshot)
    details = explain(snapshot, result, detector)
    assert "판정 유보" in details["summary"]
    assert korean in details["summary"]
    assert details["replica_evidence"] == {}
    assert details["b_evidence"] == {}


def test_safe_examples_are_bounded_and_single_line(tmp_path):
    detector, snapshot = fixture_detector(tmp_path / "baseline.pkl")
    snapshot["sets"]["a"]["procs"] += ["z" * 300, "y" * 300, "x\n" * 200, "w" * 300]
    details = explain(snapshot, detector.evaluate(snapshot), detector)
    feature = details["replica_evidence"]["a"]["features"]["procs"]
    assert len(feature["examples_absent_from_some_peers"]) <= 3
    for row in feature["examples_absent_from_some_peers"]:
        assert len(row["value"]) <= 160
        assert "\n" not in row["value"]
    assert all(len(value) <= 160 for value in feature["per_peer"]["b"]["examples"])
