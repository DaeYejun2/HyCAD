"""Bounded event-time aggregation of flat Sysdig JSON or output_fields events."""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from preprocess import DIGITS, FEATURES, FIELD_MAP, pick_replica_field

MAX_SYSCALLS = 512
MAX_FIELD_LENGTH = 4096


class EventWindowAggregator:
    """Close UTC-aligned windows and report incomplete data instead of hiding it.

    Events older than the currently open bucket are dropped. The first observed
    bucket is never considered complete. No timers or file readers are started;
    the caller drives close_until() even when the input stream is idle.
    """

    def __init__(self, replicas: list[str], window_seconds: int = 30,
                 max_values_per_feature: int = 2048, max_gap_windows: int = 120):
        if (len(replicas) < 2 or len(set(replicas)) != len(replicas)
                or any(not isinstance(rep, str) or not rep for rep in replicas)):
            raise ValueError("At least two unique replica names are required")
        if (type(window_seconds) is not int or window_seconds <= 0
                or type(max_values_per_feature) is not int or max_values_per_feature <= 0
                or type(max_gap_windows) is not int or max_gap_windows < 2):
            raise ValueError("Window and value limits must be positive integers; gap limit must be >= 2")
        self.replicas = list(replicas)
        self.window_seconds = window_seconds
        self.max_values_per_feature = max_values_per_feature
        self.max_gap_windows = max_gap_windows
        self._replicas = set(replicas)
        self._window_ns = window_seconds * 1_000_000_000
        self._bucket: int | None = None
        self._initial_bucket: int | None = None
        self._counts: dict[str, Counter] = {}
        self._sets: dict[str, dict[str, set]] = {}
        self._invalid_reason = ""
        self._continuity_lost = False
        self.diagnostics = {
            "accepted_events": 0, "invalid_events": 0, "late_events": 0,
            "unknown_replica_events": 0, "dropped_values": 0,
            "windows_emitted": 0, "gap_windows_skipped": 0,
        }
        self.last_event_timestamp: float | None = None

    def add(self, event: dict) -> list[dict]:
        if not isinstance(event, dict):
            self.diagnostics["invalid_events"] += 1
            self.invalidate("invalid_event")
            return []
        event = event.get("output_fields", event)
        if not isinstance(event, dict):
            self.diagnostics["invalid_events"] += 1
            self.invalidate("invalid_event")
            return []
        rawtime = event.get("evt.rawtime")
        if isinstance(rawtime, str) and rawtime.isascii() and rawtime.isdigit():
            try:
                rawtime = int(rawtime)
            except ValueError:
                rawtime = None
        syscall = event.get("evt.type")
        if (type(rawtime) is not int or rawtime < 0 or rawtime > 2**63 - 1
                or not isinstance(syscall, str) or not syscall or len(syscall) > 128
                or any(field not in event for field, _, _ in FIELD_MAP)
                or any(event.get(field) is not None and not isinstance(event[field], str)
                       for field, _, _ in FIELD_MAP)):
            self.diagnostics["invalid_events"] += 1
            self.invalidate("invalid_event")
            return []
        replica = event.get(pick_replica_field(event))
        if not isinstance(replica, str) or replica not in self._replicas:
            self.diagnostics["unknown_replica_events"] += 1
            return []
        bucket = rawtime // self._window_ns
        if self._bucket is None:
            self._bucket = self._initial_bucket = bucket
        if bucket < self._bucket:
            self.diagnostics["late_events"] += 1
            self.invalidate("late_event")
            return []
        completed = self._flush_until(bucket)
        if self._continuity_lost:
            # After a transport gap, the first observed bucket can have started
            # before input resumed. Never treat that fragment as a full window.
            if not self._invalid_reason:
                self._invalid_reason = "recovery_partial_window"
            self._continuity_lost = False
        counts = self._counts.setdefault(replica, Counter())
        if syscall not in counts and len(counts) >= MAX_SYSCALLS:
            self._invalid_reason = "syscall_vocabulary_limit"
            self.diagnostics["dropped_values"] += 1
            return completed
        counts[syscall] += 1
        features = self._sets.setdefault(replica, {feature: set() for feature in FEATURES})
        self._add_value(features["syscalls"], syscall)
        for field, feature, strip in FIELD_MAP:
            value = event.get(field)
            if value:
                if len(value) > MAX_FIELD_LENGTH:
                    self.diagnostics["dropped_values"] += 1
                    self._invalid_reason = "feature_value_limit"
                    continue
                self._add_value(features[feature], DIGITS.sub("", value) if strip else value)
        self.diagnostics["accepted_events"] += 1
        self.last_event_timestamp = max(self.last_event_timestamp or 0, rawtime / 1_000_000_000)
        return completed

    def invalidate(self, reason: str = "input_error") -> None:
        """Disqualify the open window after upstream loss or parse failure.

        Before the first accepted event there is no bucket to invalidate. The
        initial bucket is already marked incomplete when it is eventually opened.
        """
        if self._bucket is not None:
            if not self._invalid_reason:
                self._invalid_reason = reason
            self._continuity_lost = True

    def _add_value(self, values: set, value: str) -> None:
        if value in values:
            return
        if len(values) >= self.max_values_per_feature:
            self.diagnostics["dropped_values"] += 1
            self._invalid_reason = "feature_value_limit"
            return
        values.add(value)

    def _flush_one(self, incomplete_reason: str = "") -> dict:
        assert self._bucket is not None
        reason = self._invalid_reason or incomplete_reason
        if not reason and self._bucket == self._initial_bucket:
            reason = "startup_partial_window"
        snapshot = {
            "start": self._bucket * self.window_seconds,
            "end": (self._bucket + 1) * self.window_seconds,
            "counts": {replica: dict(counts) for replica, counts in self._counts.items()},
            "sets": {replica: {feature: sorted(values) for feature, values in features.items()}
                     for replica, features in self._sets.items()},
            "complete": not bool(reason), "reason": reason,
        }
        self._counts = {}
        self._sets = {}
        self._invalid_reason = ""
        self._bucket += 1
        self.diagnostics["windows_emitted"] += 1
        return snapshot

    def _flush_until(self, target: int, incomplete_reason: str = "") -> list[dict]:
        if self._bucket is None or target <= self._bucket:
            return []
        snapshots = []
        if target - self._bucket > self.max_gap_windows:
            snapshots.append(self._flush_one(incomplete_reason))
            resume = target - (self.max_gap_windows - 1)
            self.diagnostics["gap_windows_skipped"] += resume - self._bucket
            self._bucket = resume
        while self._bucket < target:
            snapshots.append(self._flush_one(incomplete_reason))
        return snapshots

    def close_until(self, now_seconds: float) -> list[dict]:
        """Expire elapsed windows without assuming an idle source was complete.

        A later source event, through add(), is the completeness watermark. A
        wall-clock timeout cannot distinguish real inactivity from stalled input.
        """
        if (isinstance(now_seconds, bool) or not isinstance(now_seconds, (int, float))
                or not math.isfinite(now_seconds) or now_seconds < 0):
            raise ValueError("now_seconds must be a finite nonnegative timestamp")
        target = math.floor(now_seconds / self.window_seconds)
        if self._bucket is not None and target > self._bucket:
            self._continuity_lost = True
        return self._flush_until(target, "source_not_advanced")
