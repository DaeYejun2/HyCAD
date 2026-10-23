import copy
import hashlib
import json
import pickle
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from hycad_monitor.capture_replay import (CaptureMetrics, CapturePlayback, build_playbacks,
                                         load_catalog, make_handler, parser)
from preprocess import FEATURES


def cache(path, windows=3, anomaly=False):
    data = {"t0": 1_700_000_010_000_000_000, "feats": list(FEATURES), "freq": {}, "sets": {}}
    for index in range(windows):
        for rep in ("a", "b"):
            data["freq"][(index, rep)] = {"accept4": 1, "read": 9}
            data["sets"][(index, rep)] = {"syscalls": ["accept4", "read"], "procs": ["server"]}
    if anomaly:
        data["sets"][(0, "a")]["procs"].append("shell")
        data["freq"][(1, "a")]["new_syscall"] = 2
    path.write_bytes(pickle.dumps(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CaptureReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        baseline_hash = cache(self.root / "baseline.pkl")
        capture_hash = cache(self.root / "capture.pkl", anomaly=True)
        self.data = {"schema_version": 1, "captures": [{
            "id": "capture-one", "deployment": "catalogue", "title": "저장 캡처 1",
            "cache": "capture.pkl", "cache_sha256": capture_hash,
            "baseline": "baseline.pkl", "baseline_sha256": baseline_hash,
            "reference_description": "고정된 과거 기준선", "source_name": "capture.jsonl.gz",
            "source_sha256": "b" * 64,
        }]}
        self.manifest = self.root / "catalog.json"
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.data))

    def playback(self, **kwargs):
        return CapturePlayback(load_catalog(self.manifest)[0], **kwargs)

    def test_compose_loki_environment_is_respected_with_cli_override(self):
        with patch.dict("os.environ", {"HYCAD_LOKI_URL": "http://loki:3100/loki/api/v1/push"}):
            args = parser().parse_args(["--manifest", "catalog.json"])
            self.assertEqual(args.loki_url, "http://loki:3100/loki/api/v1/push")
            overridden = parser().parse_args(["--manifest", "catalog.json", "--loki-url", "http://other"])
            self.assertEqual(overridden.loki_url, "http://other")

    def test_source_time_decision_and_baseline_hash_remain_separate_from_wall_time(self):
        playback = self.playback(clock=lambda: 1_800_000_000)
        result = playback.step()
        self.assertEqual(result["mode"], "replay")
        self.assertEqual(result["capture"], "capture-one")
        self.assertEqual(result["status"], "partial_infection")
        self.assertEqual(result["window_start"], 1_700_000_010)
        self.assertEqual(result["processed_at"], 1_800_000_000)
        self.assertTrue(playback.state.snapshot()["ready"])
        self.assertIn("CAPTURE capture-one", result["explanation"]["summary"])
        self.assertEqual(result["baseline_sha256"], self.data["captures"][0]["baseline_sha256"])
        self.assertNotIn(str(self.root), json.dumps(playback.metadata()))

    def test_multiple_captures_keep_models_counters_and_incidents_independent(self):
        second = copy.deepcopy(self.data["captures"][0])
        second.update(id="front-end-normal", deployment="front-end", cache="baseline.pkl",
                      cache_sha256=second["baseline_sha256"])
        self.data["captures"].append(second)
        self.save()
        left, right = build_playbacks(load_catalog(self.manifest))
        left.step()
        left.step()
        self.assertIn("new_syscall", left.detector.vocab)
        self.assertNotIn("new_syscall", right.detector.vocab)
        result = right.step()
        self.assertEqual(result["status"], "normal")
        self.assertEqual(sum(right.state.snapshot()["processed"].values()), 1)
        left.step()  # Unknown capture tail remains insufficient_data.
        replayed = left.step()
        self.assertEqual(left.cycle, 2)
        self.assertEqual(replayed["replay_window_index"], 1)
        self.assertNotIn("new_syscall", left.detector.vocab)
        self.assertEqual(replayed["status"], "partial_infection")

    def test_merged_prometheus_families_are_unambiguous_and_capture_labeled(self):
        second = copy.deepcopy(self.data["captures"][0])
        second["id"] = "capture-two"
        self.data["captures"].append(second)
        self.save()
        playbacks = build_playbacks(load_catalog(self.manifest))
        for playback in playbacks:
            playback.step()
        registry = CollectorRegistry()
        registry.register(CaptureMetrics(playbacks))
        text = generate_latest(registry).decode()
        self.assertEqual(text.count("# HELP hycad_ready "), 1)
        self.assertIn('capture="capture-one"', text)
        self.assertIn('capture="capture-two"', text)
        samples = [sample for metric in text_string_to_metric_families(text) for sample in metric.samples]
        score_samples = [sample for sample in samples if sample.name == "hycad_channel_b_score"]
        self.assertEqual(len(score_samples), 4)
        self.assertTrue(all(sample.labels["mode"] == "replay" for sample in samples))
        self.assertEqual(len([sample for sample in samples if sample.name == "hycad_capture_info"]), 2)

    def test_catalog_rejects_escape_symlink_duplicate_id_hash_and_size(self):
        row = self.data["captures"][0]
        for change in ({"id": "x;touch_bad"}, {"cache": "../outside.pkl"},
                       {"cache": str(self.root / "capture.pkl")}, {"baseline_sha256": "oops"},
                       {"source_name": "/private/path.jsonl"}, {"title": "bad\nline"}):
            original = copy.deepcopy(row)
            row.update(change)
            self.save()
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_catalog(self.manifest)
            row.clear()
            row.update(original)
        self.data["captures"].append(copy.deepcopy(row))
        self.save()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_catalog(self.manifest)

    def test_changed_cache_or_baseline_cannot_be_silently_replayed(self):
        self.data["captures"][0]["cache_sha256"] = "0" * 64
        self.save()
        with self.assertRaisesRegex(ValueError, "Cache hash mismatch"):
            self.playback()
        self.data["captures"][0]["cache_sha256"] = hashlib.sha256((self.root / "capture.pkl").read_bytes()).hexdigest()
        self.data["captures"][0]["baseline_sha256"] = "0" * 64
        self.save()
        with self.assertRaisesRegex(ValueError, "Baseline hash mismatch"):
            self.playback()

    def test_catalog_symlinks_cannot_escape_and_total_resources_are_bounded(self):
        with tempfile.TemporaryDirectory() as outside:
            destination = Path(outside) / "private.pkl"
            cache(destination)
            (self.root / "link.pkl").symlink_to(destination)
            self.data["captures"][0]["cache"] = "link.pkl"
            self.save()
            with self.assertRaisesRegex(ValueError, "escapes"):
                load_catalog(self.manifest)
        self.data["captures"][0]["cache"] = "capture.pkl"
        self.save()
        with patch("hycad_monitor.capture_replay.MAX_TOTAL_CACHE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "total cache byte"):
                load_catalog(self.manifest)
        with patch("hycad_monitor.capture_replay.MAX_TOTAL_WINDOWS", 1):
            with self.assertRaisesRegex(ValueError, "window count"):
                self.playback()

    def test_http_catalog_and_status_are_read_only_and_omit_local_paths(self):
        playback = self.playback()
        playback.step()
        registry = CollectorRegistry()
        registry.register(CaptureMetrics([playback]))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler([playback], registry))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            for endpoint in ("/catalog", "/status", "/healthz", "/readyz", "/metrics"):
                with urlopen(url + endpoint) as response:
                    text = response.read().decode()
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(str(self.root), text)
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(url + "/catalog", data=b"{}", method="POST"))
            self.assertEqual(raised.exception.code, 501)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
