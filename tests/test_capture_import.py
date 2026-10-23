import gzip
import json
import pickle
import tempfile
import unittest
from pathlib import Path

from hycad_monitor.capture_import import import_capture
from hycad_monitor.engine import Detector, load_cache_snapshots
from preprocess import FEATURES, FIELD_MAP, TAU_NS


def event(second, replica="a", syscall="read"):
    return {**{field: None for field, _, _ in FIELD_MAP}, "evt.rawtime": int(second * 1_000_000_000),
            "container.name": replica, "evt.type": syscall}


def cohort_events(start):
    return [event(start, "a", "accept4"), event(start + 1, "b", "accept4"),
            event(start + 2, "a"), event(start + 3, "b")]


class CaptureImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "capture.pkl"

    def write(self, rows, compressed=False):
        path = self.root / ("capture.jsonl.gz" if compressed else "capture.jsonl")
        text = "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows) + "\n"
        if compressed:
            with gzip.open(path, "wt") as handle:
                handle.write(text)
        else:
            path.write_text(text)
        return path

    def test_utc_alignment_quality_and_hash_provenance_survive_reload(self):
        source = self.write(cohort_events(7) + [{"output_fields": row} for row in cohort_events(31)] + cohort_events(61), True)
        report = import_capture(source, self.output, ["a", "b"])
        reps, snapshots = load_cache_snapshots(self.output)
        self.assertEqual(reps, ["a", "b"])
        self.assertEqual([row["start"] for row in snapshots], [0, 30, 60])
        self.assertEqual([row["complete"] for row in snapshots], [False, True, False])
        self.assertEqual(snapshots[0]["reason"], "startup_partial_window")
        self.assertEqual(snapshots[-1]["reason"], "capture_tail_duration_unknown")
        self.assertEqual(report["source_first_event_ns"], 7_000_000_000)
        self.assertEqual(report["window_alignment"], "utc")
        self.assertFalse(report["normality_verified"])
        self.assertEqual(len(report["source_sha256"]), 64)
        self.assertEqual(Detector(self.output).baseline_windows, 1)
        self.assertEqual(Detector(self.output).evaluate(snapshots[1])["status"], "normal")

    def test_zero_denominator_and_missing_replica_are_not_normal(self):
        source = self.write(cohort_events(1) + cohort_events(31) +
                            [event(61, "a"), event(62, "b")] + [event(91, "a", "accept4")] + cohort_events(121))
        import_capture(source, self.output, ["a", "b"])
        detector = Detector(self.output)
        _, snapshots = load_cache_snapshots(self.output)
        self.assertEqual(detector.evaluate(snapshots[2])["reason"], "zero_accepts")
        self.assertEqual(detector.evaluate(snapshots[3])["reason"], "missing_replicas")
        self.assertEqual(detector.baseline_windows, 1)

    def test_malformed_line_and_source_reset_are_preserved_as_quality(self):
        source = self.write(cohort_events(1) + cohort_events(31) + ["not-json"] +
                            cohort_events(61) + ["!bridge_source_start"] + cohort_events(91) + cohort_events(121))
        report = import_capture(source, self.output, ["a", "b"])
        _, snapshots = load_cache_snapshots(self.output)
        self.assertFalse(snapshots[1]["complete"])
        self.assertEqual(snapshots[1]["reason"], "malformed_event")
        self.assertTrue(all(not row["complete"] for row in snapshots))
        self.assertEqual(report["diagnostics"]["source_resets"], 1)
        with self.assertRaisesRegex(ValueError, "no complete valid windows"):
            Detector(self.output)

    def test_empty_gap_is_visible_and_late_event_invalidates_original_bucket(self):
        source = self.write(cohort_events(1) + cohort_events(31) + cohort_events(91) +
                            [event(32, "a")] + cohort_events(121))
        report = import_capture(source, self.output, ["a", "b"])
        _, snapshots = load_cache_snapshots(self.output)
        self.assertEqual([row["start"] for row in snapshots], [0, 30, 60, 90, 120])
        self.assertEqual(snapshots[1]["reason"], "late_event")
        self.assertEqual(snapshots[2]["counts"], {})
        self.assertEqual(report["diagnostics"]["late_events"], 1)

    def test_same_bucket_reordering_keeps_counts_but_is_reported(self):
        rows = cohort_events(1) + [event(31, "a", "accept4"), event(33, "b", "accept4"),
                                  event(32, "a"), event(34, "b")] + cohort_events(61)
        report = import_capture(self.write(rows), self.output, ["a", "b"])
        _, snapshots = load_cache_snapshots(self.output)
        self.assertEqual(report["diagnostics"]["out_of_order_events"], 1)
        self.assertTrue(snapshots[1]["complete"])
        self.assertEqual(Detector(self.output).evaluate(snapshots[1])["status"], "normal")

    def test_foreign_service_does_not_change_membership_or_completeness(self):
        source = self.write(cohort_events(1) + cohort_events(31) + [event(9000, "other")] + cohort_events(61))
        report = import_capture(source, self.output, ["a", "b"])
        self.assertEqual(report["window_count"], 3)
        self.assertEqual(report["diagnostics"]["other_replica_events"], 1)
        self.assertEqual(Detector(self.output).replicas, ["a", "b"])

    def test_replica_missing_throughout_capture_does_not_shrink_comparison_cohort(self):
        source = self.write([event(1, "a", "accept4"), event(31, "a", "accept4"), event(61, "a", "accept4")])
        import_capture(source, self.output, ["a", "b"])
        replicas, snapshots = load_cache_snapshots(self.output)
        self.assertEqual(replicas, ["a", "b"])
        self.assertNotIn("b", snapshots[1]["counts"])
        # Missing the entire second replica cannot silently calibrate a smaller
        # cohort or become a normal baseline.
        with self.assertRaisesRegex(ValueError, "no complete valid windows"):
            Detector(self.output)

    def test_missing_required_field_invalidates_instead_of_filling_null(self):
        bad = event(35)
        del bad["proc.args"]
        source = self.write(cohort_events(1) + cohort_events(31) + [bad] + cohort_events(61))
        import_capture(source, self.output, ["a", "b"])
        _, snapshots = load_cache_snapshots(self.output)
        self.assertEqual(snapshots[1]["reason"], "invalid_event")

    def test_limits_unsupported_formats_and_overwrite_are_explicit(self):
        source = self.write(cohort_events(1) + cohort_events(31) + cohort_events(61))
        with self.assertRaisesRegex(ValueError, "source size limit"):
            import_capture(source, self.output, ["a", "b"], max_source_bytes=1)
        with self.assertRaisesRegex(ValueError, "expanded byte limit"):
            import_capture(source, self.output, ["a", "b"], max_expanded_bytes=1)
        with self.assertRaisesRegex(ValueError, "event count limit"):
            import_capture(source, self.output, ["a", "b"], max_events=1)
        with self.assertRaisesRegex(ValueError, "Unsupported capture format"):
            import_capture(self.root / "capture.scap", self.output, ["a", "b"])
        import_capture(source, self.output, ["a", "b"])
        with self.assertRaises(FileExistsError):
            import_capture(source, self.output, ["a", "b"])
        import_capture(source, self.output, ["a", "b"], overwrite=True)

    def test_invalid_quality_metadata_is_rejected_before_baseline_calibration(self):
        source = self.write(cohort_events(1) + cohort_events(31) + cohort_events(61))
        import_capture(source, self.output, ["a", "b"])
        data = pickle.loads(self.output.read_bytes())
        data["window_quality"][1] = {"complete": True, "reason": "truncated"}
        self.output.write_bytes(pickle.dumps(data))
        with self.assertRaisesRegex(ValueError, "quality entry"):
            Detector(self.output)
        data["window_quality"][1] = {"complete": True, "reason": ""}
        data["t0"] += 1
        self.output.write_bytes(pickle.dumps(data))
        with self.assertRaisesRegex(ValueError, "quality metadata"):
            Detector(self.output)


if __name__ == "__main__":
    unittest.main()
