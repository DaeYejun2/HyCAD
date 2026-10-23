"""Validated runtime adapter for the original HyCAD A/B(z)/C implementation.

The reference research modules remain untouched. Runtime policy (valid windows,
replica membership and unknown syscall handling) lives here, outside the model.
"""
from __future__ import annotations

import hashlib
import io
import math
import pickle
from pathlib import Path
from typing import Any

import numpy as np

import channels as ch
import controller as ctl
from preprocess import FEATURES, TAU_NS

MAX_SYSCALLS = 512
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_WINDOWS = 20_000
WINDOW_SECONDS = TAU_NS / 1_000_000_000
STATUS_NAMES = {
    "정상": "normal",
    "부분 감염": "partial_infection",
    "전원 감염": "all_infection",
    "정상 변화": "drift",
}


class _PrimitiveUnpickler(pickle.Unpickler):
    """Read aggregate dictionaries without permitting executable pickle objects."""

    def find_class(self, module: str, name: str) -> Any:
        raise pickle.UnpicklingError("Only primitive aggregate cache data is allowed")

    def persistent_load(self, pid: Any) -> Any:
        raise pickle.UnpicklingError("Persistent pickle references are not allowed")


def _read_cache(path: Path) -> tuple[dict, str]:
    path = Path(path)
    if path.stat().st_size > MAX_CACHE_BYTES:
        raise ValueError(f"Cache exceeds {MAX_CACHE_BYTES} bytes")
    raw = path.read_bytes()
    data = _PrimitiveUnpickler(io.BytesIO(raw)).load()
    if not isinstance(data, dict):
        raise ValueError("Cache must contain an aggregate dictionary")
    for field in ("freq", "sets"):
        if not isinstance(data.get(field), dict) or not data[field]:
            raise ValueError(f"Cache has no {field} data")
        for key in data[field]:
            if (not isinstance(key, tuple) or len(key) != 2
                    or type(key[0]) is not int or key[0] < 0
                    or not isinstance(key[1], str) or not key[1]):
                raise ValueError("Invalid (snapshot, replica) cache key")
    if type(data.get("t0")) is not int or not 0 <= data["t0"] <= 2**63 - 1:
        raise ValueError("Cache t0 must be an integer timestamp in nanoseconds")
    if data.get("feats") != FEATURES:
        raise ValueError("Cache feature schema differs from preprocess.FEATURES")
    cohort = data.get("replicas")
    if cohort is not None:
        if (not isinstance(cohort, list) or not 2 <= len(cohort) <= 64
                or any(not isinstance(rep, str) or not rep or len(rep) > 256 for rep in cohort)
                or len(set(cohort)) != len(cohort)):
            raise ValueError("Invalid cache replica cohort")
        observed = {rep for _, rep in data["freq"]} | {rep for _, rep in data["sets"]}
        if not observed <= set(cohort):
            raise ValueError("Observed replica is outside the cache cohort")
    quality = data.get("window_quality")
    if quality is not None:
        # New imported captures retain the live UTC window and completeness
        # policy. Legacy research caches keep their historical interpretation.
        if (not isinstance(quality, dict) or not quality or len(quality) > MAX_CACHE_WINDOWS
                or data.get("window_alignment") != "utc" or data["t0"] % TAU_NS):
            raise ValueError("Invalid cache window quality metadata")
        for index, item in quality.items():
            if (type(index) is not int or index < 0 or not isinstance(item, dict)
                    or type(item.get("complete")) is not bool
                    or not isinstance(item.get("reason"), str) or len(item["reason"]) > 128
                    or bool(item["reason"]) == item["complete"]):
                raise ValueError("Invalid cache window quality entry")
        indices = {index for index, _ in data["freq"]} | {index for index, _ in data["sets"]}
        if not indices <= quality.keys():
            raise ValueError("Missing cache window quality entry")
    indices = {index for index, _ in data["freq"]} | {index for index, _ in data["sets"]}
    if quality:
        indices |= quality.keys()
    if (len(indices) > MAX_CACHE_WINDOWS
            or data["t0"] + (max(indices) + 1) * TAU_NS > 2**63 - 1):
        raise ValueError("Cache exceeds the window count or timestamp limit")
    return data, hashlib.sha256(raw).hexdigest()


def _replica_mapping(data: dict, replicas: list[str] | None) -> tuple[list[str], dict[str, str]]:
    source = sorted(data.get("replicas") or ({r for _, r in data["freq"]} | {r for _, r in data["sets"]}))
    target = list(source if replicas is None else replicas)
    if (len(target) < 2 or len(target) != len(source)
            or len(set(target)) != len(target)
            or any(not isinstance(r, str) or not r or len(r) > 256 for r in target)):
        raise ValueError("Replicas must be unique names, at least two, matching the baseline replica count")
    return target, dict(zip(source, target))


def _cache_snapshots(data: dict, mapping: dict[str, str]) -> list[dict]:
    quality = data.get("window_quality", {})
    ids = sorted({s for s, _ in data["freq"]} | {s for s, _ in data["sets"]} | quality.keys())
    snapshots = []
    for index in ids:
        counts = {target: data["freq"][(index, source)]
                  for source, target in mapping.items() if (index, source) in data["freq"]}
        sets = {target: data["sets"][(index, source)]
                for source, target in mapping.items() if (index, source) in data["sets"]}
        complete = quality[index]["complete"] if quality else index != ids[-1]
        reason = quality[index]["reason"] if quality else (
            "capture_tail_duration_unknown" if index == ids[-1] else "")
        snapshots.append({
            "start": (data["t0"] + index * TAU_NS) / 1_000_000_000,
            "end": (data["t0"] + (index + 1) * TAU_NS) / 1_000_000_000,
            "counts": counts,
            "sets": sets,
            "complete": complete,
            "reason": reason,
        })
    return snapshots


def load_cache_snapshots(path: Path, replicas: list[str] | None = None) -> tuple[list[str], list[dict]]:
    """Read local aggregate cache as source-time windows, without manifest labels.

    Legacy aggregate pickles lack an observed capture end, so their final window
    is incomplete. Imported captures carry explicit quality for every UTC
    window, including empty gaps and the first/last fragments.
    """
    data, _ = _read_cache(Path(path))
    target, mapping = _replica_mapping(data, replicas)
    return target, _cache_snapshots(data, mapping)


def _validate_counts(values: Any) -> dict[str, int]:
    if not isinstance(values, dict) or len(values) > MAX_SYSCALLS:
        raise ValueError("invalid_syscall_counts")
    for name, count in values.items():
        if (not isinstance(name, str) or not name or len(name) > 128
                or type(count) is not int or count < 0 or count > 2**63 - 1):
            raise ValueError("invalid_syscall_counts")
    return values


def _validate_sets(values: Any) -> dict[str, list[str]]:
    if not isinstance(values, dict):
        raise ValueError("invalid_feature_sets")
    for feature, elements in values.items():
        if feature not in FEATURES or not isinstance(elements, (list, tuple, set)):
            raise ValueError("invalid_feature_sets")
        if any(not isinstance(value, str) for value in elements):
            raise ValueError("invalid_feature_sets")
    return values


class Detector:
    """A frozen normal baseline with bounded, zero-initialized syscall expansion."""

    def __init__(self, baseline_path: Path, replicas: list[str] | None = None):
        data, self.baseline_sha256 = _read_cache(Path(baseline_path))
        self.replicas, mapping = _replica_mapping(data, replicas)
        self.features = list(FEATURES)
        self._baseline = []
        for snapshot in _cache_snapshots(data, mapping):
            if self._invalid_reason(snapshot):
                continue
            self._baseline.append(snapshot)
        if not self._baseline:
            raise ValueError("Baseline has no complete valid windows with events and accepts for every replica")
        self.baseline_windows = len(self._baseline)
        self.vocab = sorted({name for snap in self._baseline
                             for counts in snap["counts"].values()
                             for name, count in counts.items() if count > 0})
        if len(self.vocab) > MAX_SYSCALLS:
            raise ValueError(f"Baseline has more than {MAX_SYSCALLS} syscall dimensions")
        self._calibrate()

    @property
    def thresholds(self) -> dict[str, float]:
        return {"a": float(self._thresholds.a1), "b": float(self._thresholds.b.threshold),
                "c": float(self._thresholds.c)}

    def _vectorize(self, snapshot: dict) -> dict[str, np.ndarray]:
        return {rep: np.array([snapshot["counts"][rep].get(name, 0) for name in self.vocab], dtype=float)
                for rep in self.replicas}

    def _calibrate(self) -> None:
        rows = [(self._vectorize(snap), snap["sets"]) for snap in self._baseline]
        # Use the published default z-score model explicitly; an unrelated process
        # environment variable must not silently select the experimental AE.
        a = ctl.FACTOR_A1 * max(max(ch.a1_scores(sets, self.replicas, self.features).values())
                               for _, sets in rows)
        reference = float(np.median([ch.work_rate(counts, self.replicas, self.vocab)
                                    for counts, _ in rows]))
        b = ch.ChannelB([counts for counts, _ in rows], self.replicas, kind="z")
        self._thresholds = ctl.Thresholds(a, reference, b)

    def _invalid_reason(self, snapshot: Any) -> str:
        if not isinstance(snapshot, dict):
            return "invalid_snapshot"
        start, end = snapshot.get("start"), snapshot.get("end")
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)):
            return "invalid_window_timestamp"
        if not math.isclose(end - start, WINDOW_SECONDS, rel_tol=0, abs_tol=0.00001):
            return "window_duration_mismatch"
        if snapshot.get("complete") is False:
            return str(snapshot.get("reason") or "incomplete_window")
        counts, sets = snapshot.get("counts"), snapshot.get("sets")
        if not isinstance(counts, dict) or not isinstance(sets, dict):
            return "invalid_snapshot"
        if set(counts) - set(self.replicas) or set(sets) - set(self.replicas):
            return "unexpected_replicas"
        if any(rep not in counts or rep not in sets for rep in self.replicas):
            return "missing_replicas"
        try:
            for rep in self.replicas:
                _validate_counts(counts[rep])
                _validate_sets(sets[rep])
                if sum(counts[rep].values()) <= 0:
                    return "zero_events"
                if sum(counts[rep].get(name, 0) for name in ch.ACCEPT_SYSCALLS) <= 0:
                    return "zero_accepts"
        except ValueError as exc:
            return str(exc)
        return ""

    def evaluate(self, snapshot: dict) -> dict:
        reason = self._invalid_reason(snapshot)
        source = snapshot if isinstance(snapshot, dict) else {}
        raw_counts = source.get("counts", {})
        event_counts, accept_counts = {}, {}
        for rep in self.replicas:
            try:
                values = _validate_counts(raw_counts.get(rep, {}))
                event_counts[rep] = sum(values.values())
                accept_counts[rep] = sum(values.get(name, 0) for name in ch.ACCEPT_SYSCALLS)
            except (ValueError, AttributeError):
                event_counts[rep] = accept_counts[rep] = 0
        result = {
            "status": "insufficient_data", "flagged": [], "a_scores": {}, "b_scores": {},
            "c_score": None, "thresholds": self.thresholds, "work_rate": None,
            "event_counts": event_counts, "accept_counts": accept_counts,
            "reason": reason, "window_start": source.get("start"), "window_end": source.get("end"),
            "baseline_sha256": self.baseline_sha256,
        }
        if reason:
            return result
        names = {name for values in raw_counts.values() for name, count in values.items() if count > 0}
        expanded = set(self.vocab) | names
        if len(expanded) > MAX_SYSCALLS:
            result["reason"] = "syscall_vocabulary_limit"
            return result
        if names - set(self.vocab):
            self.vocab = sorted(expanded)
            self._calibrate()  # Rebuild using the original normal windows only.
        counts = self._vectorize(snapshot)
        label, flagged, diag = ctl.verdict(counts, snapshot["sets"], self.replicas,
                                           self.features, self.vocab, self._thresholds)
        a_scores = ch.a1_scores(snapshot["sets"], self.replicas, self.features)
        b_scores = self._thresholds.b.scores(counts, self.replicas)
        work_rate = float(ch.work_rate(counts, self.replicas, self.vocab))
        if not all(math.isfinite(float(value)) for value in [*a_scores.values(), *b_scores.values(),
                                                            diag["c"], work_rate]):
            result["reason"] = "nonfinite_model_output"
            return result
        result.update(status=STATUS_NAMES[label], flagged=sorted(flagged), a_scores=a_scores,
                      b_scores=b_scores, c_score=float(diag["c"]), thresholds=self.thresholds,
                      work_rate=work_rate, reason="")
        return result
