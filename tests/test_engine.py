import copy
import pickle
import tempfile
import unittest
from pathlib import Path

import numpy as np

import channels as ch
import controller as ctl
from hycad_monitor.engine import Detector, STATUS_NAMES, load_cache_snapshots
from preprocess import FEATURES, TAU_NS


def make_cache(path, windows=4):
    data = {"t0": 1_700_000_000_000_000_000, "feats": list(FEATURES), "n": 0,
            "freq": {}, "sets": {}}
    for index in range(windows):
        for rep in ("replica-a", "replica-b"):
            data["freq"][(index, rep)] = {"accept4": 10, "read": 90}
            data["sets"][(index, rep)] = {"syscalls": ["accept4", "read"], "procs": ["server"]}
    Path(path).write_bytes(pickle.dumps(data))
    return data


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "baseline.pkl"
        make_cache(self.path)
        self.detector = Detector(self.path)
        _, snapshots = load_cache_snapshots(self.path)
        self.snapshot = snapshots[0]

    def test_cache_preserves_epoch_and_excludes_uncertain_tail(self):
        reps, snapshots = load_cache_snapshots(self.path)
        self.assertEqual(reps, ["replica-a", "replica-b"])
        self.assertEqual(snapshots[0]["start"], 1_700_000_000)
        self.assertEqual(snapshots[0]["end"] - snapshots[0]["start"], 30)
        self.assertFalse(snapshots[-1]["complete"])
        self.assertEqual(self.detector.baseline_windows, 3)
        self.assertEqual(self.detector.evaluate(snapshots[-1])["status"], "insufficient_data")

    def test_normal_partial_all_and_drift_follow_original_rules(self):
        self.assertEqual(self.detector.evaluate(self.snapshot)["status"], "normal")
        partial = copy.deepcopy(self.snapshot)
        partial["sets"]["replica-a"]["procs"].append("shell")
        result = self.detector.evaluate(partial)
        self.assertEqual(result["status"], "partial_infection")
        self.assertEqual(result["flagged"], ["replica-a"])
        attack = copy.deepcopy(self.snapshot)
        for counts in attack["counts"].values():
            counts["read"] = 900
        self.assertEqual(self.detector.evaluate(attack)["status"], "all_infection")
        drift = copy.deepcopy(self.snapshot)
        for rep in self.detector.replicas:
            drift["counts"][rep] = {"accept4": 10, "read": 80, "write": 10}
            drift["sets"][rep]["syscalls"].append("write")
        self.assertEqual(self.detector.evaluate(drift)["status"], "drift")

    def test_unknown_dimensions_use_zero_baseline_without_learning_attack(self):
        snapshot = copy.deepcopy(self.snapshot)
        for rep in self.detector.replicas:
            snapshot["counts"][rep] = {"accept4": 10, "read": 80, "new_syscall": 10}
            snapshot["sets"][rep]["syscalls"].append("new_syscall")
        original_hash = self.detector.baseline_sha256
        first = self.detector.evaluate(snapshot)
        second = self.detector.evaluate(snapshot)
        self.assertIn("new_syscall", self.detector.vocab)
        self.assertEqual(first["b_scores"], second["b_scores"])
        self.assertGreater(first["b_scores"]["replica-a"], 1000)
        self.assertAlmostEqual(first["thresholds"]["b"], 0)
        self.assertEqual(self.detector.baseline_sha256, original_hash)
        self.assertEqual(self.detector.evaluate(self.snapshot)["status"], "normal")

    def test_invalid_windows_are_never_labeled_normal(self):
        cases = []
        missing = copy.deepcopy(self.snapshot)
        del missing["counts"]["replica-a"]
        cases.append((missing, "missing_replicas"))
        empty = copy.deepcopy(self.snapshot)
        empty["counts"]["replica-a"] = {}
        cases.append((empty, "zero_events"))
        no_accept = copy.deepcopy(self.snapshot)
        no_accept["counts"]["replica-a"] = {"read": 100}
        cases.append((no_accept, "zero_accepts"))
        negative = copy.deepcopy(self.snapshot)
        negative["counts"]["replica-a"]["read"] = -1
        cases.append((negative, "invalid_syscall_counts"))
        truncated = copy.deepcopy(self.snapshot)
        truncated.update(complete=False, reason="feature_value_limit")
        cases.append((truncated, "feature_value_limit"))
        short = copy.deepcopy(self.snapshot)
        short["end"] -= 1
        cases.append((short, "window_duration_mismatch"))
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                result = self.detector.evaluate(snapshot)
                self.assertEqual(result["status"], "insufficient_data")
                self.assertEqual(result["reason"], reason)
                self.assertIsNone(result["c_score"])
                self.assertEqual(result["flagged"], [])

    def test_vocabulary_limit_rejects_without_growing_model(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["counts"]["replica-a"].update({f"extra_{i}": 1 for i in range(300)})
        snapshot["counts"]["replica-b"].update({f"other_{i}": 1 for i in range(300)})
        result = self.detector.evaluate(snapshot)
        self.assertEqual(result["reason"], "syscall_vocabulary_limit")
        self.assertEqual(self.detector.vocab, ["accept4", "read"])

    def test_replica_mapping_is_explicit_and_same_size(self):
        detector = Detector(self.path, ["pod-z", "pod-a"])
        reps, snapshots = load_cache_snapshots(self.path, ["pod-z", "pod-a"])
        self.assertEqual(reps, ["pod-z", "pod-a"])
        self.assertEqual(detector.evaluate(snapshots[0])["status"], "normal")
        with self.assertRaises(ValueError):
            Detector(self.path, ["one"])
        with self.assertRaises(ValueError):
            Detector(self.path, ["same", "same"])

    def test_unsafe_pickle_objects_are_rejected(self):
        class Unsafe:
            def __reduce__(self):
                return eval, ("1 + 1",)
        path = Path(self.temp.name) / "unsafe.pkl"
        path.write_bytes(pickle.dumps(Unsafe()))
        with self.assertRaises(pickle.UnpicklingError):
            Detector(path)

    def test_invalid_baseline_cannot_create_a_detector(self):
        data = make_cache(self.path)
        for counts in data["freq"].values():
            counts.pop("accept4")
        self.path.write_bytes(pickle.dumps(data))
        with self.assertRaisesRegex(ValueError, "no complete valid windows"):
            Detector(self.path)

    def test_tail_anomaly_does_not_raise_calibration_threshold(self):
        data = make_cache(self.path)
        data["sets"][(3, "replica-a")]["procs"].append("shell")
        data["freq"][(3, "replica-a")]["malicious"] = 10000
        self.path.write_bytes(pickle.dumps(data))
        detector = Detector(self.path)
        self.assertEqual(detector.thresholds["a"], 0)
        self.assertNotIn("malicious", detector.vocab)

    def test_bundled_data_scores_match_reference_controller(self):
        root = Path(__file__).resolve().parents[1]
        baseline = root / "cache" / "cat_baseline.pkl"
        if not baseline.exists():
            self.skipTest("Bundled cache is unavailable")
        detector = Detector(baseline)
        reps, original = load_cache_snapshots(baseline)
        for name in ("cat_baseline", "cat_1of4", "cat_3of4", "cat_4of4"):
            _, snapshots = load_cache_snapshots(root / "cache" / f"{name}.pkl")
            snapshot = snapshots[-2]
            result = detector.evaluate(snapshot)
            vocab = detector.vocab
            vectorize = lambda snap: {r: np.array([snap["counts"][r].get(k, 0) for k in vocab], float)
                                      for r in reps}
            rows = [(vectorize(s), s["sets"]) for s in original if s["complete"]]
            ref = ctl.Thresholds(
                ctl.FACTOR_A1 * max(max(ch.a1_scores(s, reps, FEATURES).values()) for _, s in rows),
                float(np.median([ch.work_rate(c, reps, vocab) for c, _ in rows])),
                ch.ChannelB([c for c, _ in rows], reps, kind="z"),
            )
            label, flagged, diag = ctl.verdict(vectorize(snapshot), snapshot["sets"], reps, FEATURES, vocab, ref)
            with self.subTest(capture=name):
                self.assertEqual(result["status"], STATUS_NAMES[label])
                self.assertEqual(result["flagged"], sorted(flagged))
                self.assertAlmostEqual(result["c_score"], diag["c"])
                self.assertEqual(result["a_scores"], ch.a1_scores(snapshot["sets"], reps, FEATURES))
                self.assertEqual(result["b_scores"], ref.b.scores(vectorize(snapshot), reps))


if __name__ == "__main__":
    unittest.main()
