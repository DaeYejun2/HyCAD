import gzip
import json
import pickle
import tempfile
import unittest
from pathlib import Path

from hycad_monitor.stream import EventWindowAggregator
from hycad_monitor.engine import Detector
from preprocess import FEATURES, FIELD_MAP, aggregate


def event(seconds, replica="a", syscall="read", **fields):
    return {**{field: None for field, _, _ in FIELD_MAP},
            "evt.rawtime": int(seconds * 1_000_000_000), "container.name": replica,
            "evt.type": syscall, **fields}


def normal_detector():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "baseline.pkl"
        cache = {"t0": 0, "feats": list(FEATURES), "sets": {}, "freq": {}}
        for index in range(2):
            for rep in ("a", "b"):
                cache["sets"][(index, rep)] = {"syscalls": ["accept4", "read"]}
                cache["freq"][(index, rep)] = {"accept4": 1, "read": 1}
        path.write_bytes(pickle.dumps(cache))
        return Detector(path)


class StreamTests(unittest.TestCase):
    def test_exact_boundary_starts_next_window_and_startup_is_incomplete(self):
        stream = EventWindowAggregator(["a", "b"])
        self.assertEqual(stream.add(event(1)), [])
        first = stream.add(event(30, "b"))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["counts"], {"a": {"read": 1}})
        self.assertFalse(first[0]["complete"])
        self.assertEqual(first[0]["reason"], "startup_partial_window")
        self.assertEqual(first[0]["start"], 0)
        second = stream.add(event(60))
        self.assertTrue(second[0]["complete"])
        self.assertEqual(second[0]["counts"], {"b": {"read": 1}})

    def test_missing_and_idle_windows_remain_visible(self):
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(1))
        closed = stream.close_until(90)
        self.assertEqual([s["start"] for s in closed], [0, 30, 60])
        self.assertEqual(closed[1]["counts"], {})
        self.assertTrue(all(not snapshot["complete"] for snapshot in closed))
        self.assertTrue(all(snapshot["reason"] == "source_not_advanced" for snapshot in closed))
        self.assertEqual(stream.close_until(90), [])
        self.assertEqual(stream.close_until(89), [])

    def test_late_and_unknown_events_do_not_change_current_window(self):
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(31))
        stream.add(event(29))
        stream.add(event(32, "unknown"))
        result = stream.close_until(60)[0]
        self.assertEqual(result["counts"], {"a": {"read": 1}})
        self.assertEqual(stream.diagnostics["late_events"], 1)
        self.assertEqual(stream.diagnostics["unknown_replica_events"], 1)
        self.assertEqual(stream.last_event_timestamp, 31)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "late_event")

    def test_unknown_replica_does_not_poison_configured_cohort(self):
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(1))
        stream.add(event(31))
        stream.add(event(32, "other-service"))
        self.assertTrue(stream.add(event(60))[0]["complete"])

    def test_wrapped_kubernetes_fields_and_numeric_timestamp_strings(self):
        stream = EventWindowAggregator(["pod-a", "pod-b"])
        values = event(31, "app", **{"k8s.pod.name": "pod-a", "proc.name": "server123"})
        values["evt.rawtime"] = str(values["evt.rawtime"])
        stream.add({"output_fields": values})
        snapshot = stream.close_until(60)[0]
        self.assertEqual(snapshot["sets"]["pod-a"]["procs"], ["server"])

    def test_feature_limit_disqualifies_truncated_window_and_resets_next(self):
        stream = EventWindowAggregator(["a", "b"], max_values_per_feature=2)
        stream.add(event(1))
        for name in ("cat", "dog", "bird"):
            stream.add(event(31, **{"proc.name": name}))
        snapshot = stream.add(event(60))[0]
        self.assertEqual(len(snapshot["sets"]["a"]["procs"]), 2)
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["reason"], "feature_value_limit")
        stream.add(event(61))
        self.assertTrue(stream.add(event(90))[0]["complete"])

    def test_gap_output_is_bounded_and_skips_are_counted(self):
        stream = EventWindowAggregator(["a", "b"], max_gap_windows=4)
        stream.add(event(1))
        snapshots = stream.close_until(3000)
        self.assertEqual(len(snapshots), 4)
        self.assertEqual(snapshots[0]["start"], 0)
        self.assertEqual(snapshots[-1]["end"], 3000)
        self.assertEqual(stream.diagnostics["gap_windows_skipped"], 96)

    def test_malformed_events_cannot_advance_the_clock(self):
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(1))
        for value in (None, {}, {"output_fields": []}, event(9000, **{"proc.args": ["bad"]}),
                      {"evt.rawtime": True, "evt.type": "read", "container.name": "a"}):
            self.assertEqual(stream.add(value), [])
        self.assertEqual(stream.diagnostics["invalid_events"], 5)
        snapshot = stream.close_until(30)[0]
        self.assertEqual(snapshot["counts"], {"a": {"read": 1}})
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["reason"], "invalid_event")

    def test_missing_capture_fields_invalidate_window_but_explicit_null_is_valid(self):
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(1))
        stream.add(event(31))  # Full schema with explicit null feature values.
        self.assertEqual(stream.diagnostics["accepted_events"], 2)
        stream.add({"evt.rawtime": 32_000_000_000, "container.name": "a", "evt.type": "read"})
        snapshot = stream.close_until(60)[0]
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["reason"], "invalid_event")
        self.assertEqual(stream.diagnostics["invalid_events"], 1)
        self.assertEqual(snapshot["counts"], {"a": {"read": 1}})

    def test_each_required_feature_field_is_checked(self):
        for field, _, _ in FIELD_MAP:
            with self.subTest(field=field):
                stream = EventWindowAggregator(["a", "b"])
                stream.add(event(1))
                stream.add(event(31))
                incomplete = event(32)
                del incomplete[field]
                stream.add(incomplete)
                self.assertFalse(stream.close_until(60)[0]["complete"])
                self.assertEqual(stream.diagnostics["invalid_events"], 1)

    def test_external_invalidation_excludes_resumption_then_allows_full_window(self):
        stream = EventWindowAggregator(["a", "b"])
        stream.invalidate("before_first_event")
        stream.add(event(1))
        self.assertEqual(stream.add(event(30))[0]["reason"], "startup_partial_window")
        stream.add(event(31))
        stream.invalidate("malformed_json")
        self.assertEqual(stream.add(event(60))[0]["reason"], "malformed_json")
        self.assertEqual(stream.add(event(90))[0]["reason"], "recovery_partial_window")
        self.assertTrue(stream.add(event(120))[0]["complete"])

    def test_malformed_event_cannot_turn_partial_data_into_normal_decision(self):
        detector = normal_detector()
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(1))
        for rep in ("a", "b"):
            stream.add(event(31, rep, "accept4"))
            stream.add(event(32, rep, "read"))
        stream.add({"evt.rawtime": "bad", "container.name": "a", "evt.type": "read"})
        result = detector.evaluate(stream.close_until(60)[0])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["reason"], "invalid_event")

    def test_first_sixteen_seconds_only_cannot_become_a_normal_full_window(self):
        detector = normal_detector()
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(1))
        for rep in ("a", "b"):
            stream.add(event(31, rep, "accept4"))
            stream.add(event(46, rep, "read"))
        snapshot = stream.close_until(62)[0]
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["reason"], "source_not_advanced")
        self.assertEqual(detector.evaluate(snapshot)["status"], "insufficient_data")
        # These otherwise normal positive counts would pass the model if the
        # input-completeness metadata were missing.
        snapshot["complete"] = True
        self.assertEqual(detector.evaluate(snapshot)["status"], "normal")

    def test_gap_latch_survives_empty_windows_and_excludes_midwindow_resumption(self):
        detector = normal_detector()
        stream = EventWindowAggregator(["a", "b"])
        stream.add(event(1))
        stream.add(event(31))
        stream.close_until(62)
        empty_windows = stream.close_until(125)
        self.assertEqual([snapshot["start"] for snapshot in empty_windows], [60, 90])
        for rep in ("a", "b"):
            stream.add(event(130, rep, "accept4"))
            stream.add(event(131, rep, "read"))
        resumed = stream.add(event(150, "a", "accept4"))[0]
        self.assertEqual(resumed["start"], 120)
        self.assertEqual(resumed["reason"], "recovery_partial_window")
        self.assertEqual(detector.evaluate(resumed)["status"], "insufficient_data")
        stream.add(event(151, "a", "read"))
        stream.add(event(151, "b", "accept4"))
        stream.add(event(152, "b", "read"))
        full = stream.add(event(180, "a", "accept4"))[0]
        self.assertTrue(full["complete"])
        self.assertEqual(detector.evaluate(full)["status"], "normal")

    def test_stream_aggregation_matches_original_preprocess_values(self):
        # Begin on a boundary so the original capture-relative and live UTC
        # windows have the same origin for this parity check.
        events = [event(0, "a", "accept4", **{"evt.category": "net", "proc.name": "server123",
                                               "proc.cmdline": "server --port 8080", "proc.args": "--id 22",
                                               "fd.filename": "/tmp/x99", "fd.directory": "/tmp/42",
                                               "fd.type": "ipv4", "proc.cwd": "/app/v2", "proc.exe": "/app/run3"}),
                  event(1, "b", "read", **{"evt.category": "io", "proc.name": "server99"}),
                  event(31, "a", "write", **{"proc.name": "worker7"}), event(31, "b", "read")]
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.jsonl.gz"
            with gzip.open(capture, "wt") as handle:
                handle.write("\n".join(json.dumps(value) for value in events))
            original = aggregate(str(capture), str(Path(directory) / "capture.pkl"))
        stream = EventWindowAggregator(["a", "b"])
        snapshots = []
        for value in events:
            snapshots.extend(stream.add(value))
        snapshots.extend(stream.close_until(60))
        for index, snapshot in enumerate(snapshots):
            for replica, counts in snapshot["counts"].items():
                self.assertEqual(counts, original["freq"][(index, replica)])
                actual = {key: value for key, value in snapshot["sets"][replica].items() if value}
                self.assertEqual(actual, original["sets"][(index, replica)])


if __name__ == "__main__":
    unittest.main()

def test_investigation_fields_do_not_change_model_features_or_counts():
    a,b=EventWindowAggregator(['a','b']),EventWindowAggregator(['a','b'])
    original=[];enriched=[]
    for t,rep,kind in [(1,'a','read'),(31,'a','accept4'),(32,'b','accept4'),(33,'a','read'),(34,'b','read'),(61,'a','read')]:
        e=event(t,rep,kind,**{'proc.args':'--listen 8080','proc.name':'server'})
        original+=a.add(e)
        enriched+=b.add({**e,'proc.pid':12345,'fd.rip':'192.0.2.10','fd.rport':3306,'evt.dir':'<','evt.rawres':0})
    assert original==enriched
