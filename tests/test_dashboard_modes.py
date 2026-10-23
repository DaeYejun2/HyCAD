"""Dashboard safety contracts for multiplexed capture and live exporters."""
import importlib.util
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_dashboard", ROOT / "scripts/build_dashboard.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DashboardModesTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(builder.OUTPUT.read_text())
        self.panels = {p["id"]: p for p in self.dashboard["panels"] if p["type"] != "row"}

    def test_generated_dashboard_matches_committed_source(self):
        self.assertEqual(builder.build(), self.dashboard)
        self.assertEqual("hycad-overview", self.dashboard["uid"])
        self.assertEqual(12, len(self.panels))

    def test_modes_and_captures_are_independently_selected(self):
        variables = {v["name"]: v for v in self.dashboard["templating"]["list"]}
        self.assertEqual({"deployment", "mode", "capture"}, set(variables))
        self.assertEqual(["live", "replay"], [o["value"] for o in variables["mode"]["options"]])
        self.assertIn('deployment="$deployment",mode="$mode"', variables["capture"]["definition"])
        self.assertFalse(variables["capture"]["includeAll"])

    def test_every_model_query_is_capture_isolated(self):
        for panel in self.panels.values():
            for target in panel["targets"]:
                expr = target["expr"]
                if target["datasource"]["type"] == "prometheus":
                    for selector in re.findall(r"hycad[_:a-z]+\{([^}]*)\}", expr):
                        self.assertIn('capture="$capture"', selector, (panel["id"], expr))
                        self.assertIn('deployment="$deployment"', selector)
                        self.assertIn('mode="$mode"', selector)
                    for matching in re.findall(r"on\(([^)]+)\)", expr):
                        if "deployment" in matching:
                            self.assertIn("capture", matching.split(","))
                else:
                    self.assertIn('capture="$capture"', expr)

    def test_unknown_is_never_normal_or_cached_healthy(self):
        self.assertIn("or vector(4)", self.panels[2]["targets"][0]["expr"])
        self.assertIn('up{job="hycad"} == 1', self.panels[2]["targets"][0]["expr"])
        self.assertEqual("판정 불가", builder.VERDICT_VALUES["4"]["text"])
        for pid in range(2, 7):
            self.assertTrue(self.panels[pid]["targets"][0]["instant"])
            self.assertEqual(["last"], self.panels[pid]["options"]["reduceOptions"]["calcs"])
        for pid in (7, 8, 9, 10):
            for query in self.panels[pid]["targets"]:
                self.assertIn("hycad_ready", query["expr"])

    def test_actual_exceedance_is_not_model_flagging(self):
        targets = {t["refId"]: t["expr"] for t in self.panels[10]["targets"]}
        self.assertIn("hycad_replica_anomaly", targets["A"])
        self.assertIn("hycad_channel_a_score", targets["B"])
        self.assertIn("hycad_channel_b_score", targets["C"])
        self.assertIn("> bool", targets["B"])
        self.assertIn("> bool", targets["C"])
        self.assertIn('verdict="drift"', self.panels[5]["targets"][0]["expr"])
        self.assertNotIn("hycad_retrains_total", json.dumps(self.dashboard))

    def test_vector_grouping_is_explicit_before_parenthesized_rhs(self):
        # A bare group_left followed by (threshold > 0) is parsed as a
        # grouping-label list, not as the arithmetic RHS, by Prometheus.
        for channel in ("a", "b"):
            self.assertIn("group_left() (hycad_threshold", builder.ratio(channel))
            self.assertIn("group_left() hycad_threshold", builder.crossed(channel))
        for dashboard in (self.dashboard, builder.build_manual()):
            for panel in dashboard["panels"]:
                for query in panel.get("targets", []):
                    self.assertNotRegex(query["expr"], r"group_left\s+\(")

    def test_upstream_visuals_and_ubuntu_datasources(self):
        self.assertEqual("log", self.panels[8]["fieldConfig"]["defaults"]["custom"]["scaleDistribution"]["type"])
        for pid in (7, 8):
            self.assertEqual(4, len(self.panels[pid]["fieldConfig"]["overrides"]))
            self.assertEqual(1, self.panels[pid]["fieldConfig"]["defaults"]["thresholds"]["steps"][-1]["value"])
        text = json.dumps(self.dashboard)
        self.assertNotIn("DS_PROMETHEUS", text)
        self.assertNotIn("localhost", text)
        self.assertNotIn("stack.ps1", text)
        for panel in self.panels.values():
            for query in panel["targets"]:
                self.assertIn(query["datasource"]["uid"], ("hycad-prometheus", "hycad-loki"))

    def test_legacy_investigation_is_explicitly_live_only(self):
        row = [p for p in self.dashboard["panels"] if p["type"] == "row"][0]
        self.assertIn("LIVE", row["title"])
        self.assertTrue(row["collapsed"])
        for panel in row["panels"]:
            for query in panel["targets"]:
                self.assertIn('mode="live",capture=~"live|"', query["expr"])

    def test_manual_dashboard_cannot_mix_automatic_replay(self):
        manual = json.loads(builder.MANUAL_OUTPUT.read_text())
        self.assertEqual(builder.build_manual(), manual)
        self.assertEqual("hycad-replay", manual["uid"])
        self.assertEqual(12, len(manual["panels"]))
        for panel in manual["panels"]:
            for query in panel["targets"]:
                if query["datasource"]["type"] == "prometheus":
                    self.assertNotIn('job="hycad"', query["expr"])
                    self.assertIn('job="hycad-manual"', query["expr"])
                else:
                    self.assertIn('playback="manual"', query["expr"])
        self.assertIn('playback!="manual"', self.panels[11]["targets"][0]["expr"])
        state = next(p for p in manual["panels"] if p["id"] == 3)
        self.assertIn("hycad_manual_state", state["targets"][0]["expr"])
        self.assertEqual("캡처 선택 대기", state["fieldConfig"]["defaults"]["mappings"][0]["options"]["0"]["text"])
        self.assertTrue(all(v["hide"] == 2 for v in manual["templating"]["list"]))
        self.assertTrue(any(link["url"] == "/hycad/replay/" for link in manual["links"]))

    def test_manual_hidden_selection_uses_active_controller_not_historical_series(self):
        manual = builder.build_manual()
        for variable in manual["templating"]["list"]:
            if variable["name"] == "mode":
                continue
            self.assertTrue(variable["definition"].startswith("query_result(hycad_manual_state{"))
            self.assertIn('up{job="hycad-manual"} == 1', variable["definition"])
            self.assertNotIn("label_values", variable["definition"])
            self.assertEqual(2, variable["refresh"])
            self.assertEqual('/' + variable["name"] + '="([^"]+)"/', variable["regex"])


if __name__ == "__main__":
    unittest.main()
