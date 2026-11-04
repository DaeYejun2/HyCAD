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


def canvas_elements(elements):
    for element in elements:
        yield element
        yield from canvas_elements(element.get("elements", []))


class DashboardModesTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(builder.OUTPUT.read_text())
        self.panels = {p["id"]: p for p in self.dashboard["panels"] if p["type"] != "row"}

    def test_generated_dashboard_matches_committed_source(self):
        self.assertEqual(builder.build(), self.dashboard)
        self.assertEqual("hycad-overview", self.dashboard["uid"])
        self.assertEqual(6, len(self.panels))
        self.assertNotIn(12, self.panels)
        self.assertFalse(set(range(1, 7)) & set(self.panels))

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
        canvas = self.panels[13]
        queries = {q["refId"]: q for q in canvas["targets"]}
        self.assertIn("or vector(4)", queries["SERVICE"]["expr"])
        self.assertIn('up{job="hycad"} == 1', queries["SERVICE"]["expr"])
        self.assertEqual("판정 불가", builder.VERDICT_VALUES["4"]["text"])
        for q in canvas["targets"]:
            self.assertTrue(q["instant"])
            self.assertFalse(q["range"])
            self.assertNotIn("or vector(0)", q["expr"])
            if q["refId"] != "SERVICE":
                self.assertIn("or vector(-1)", q["expr"])
                self.assertIn("hycad_ready", q["expr"])
        self.assertEqual("판정 불가", builder.REPLICA_VALUES["-1"]["text"])
        self.assertEqual("지목 없음", builder.REPLICA_VALUES["0"]["text"])
        self.assertNotEqual("green", builder.REPLICA_VALUES["0"]["color"])
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
        service = next(t for t in self.panels[13]["targets"] if t["refId"] == "SERVICE")
        self.assertIn('verdict="drift"', service["expr"])
        self.assertNotIn("hycad_retrains_total", json.dumps(self.dashboard))

    def test_canvas_has_four_native_servers_and_unambiguous_field_bindings(self):
        canvas = self.panels[13]
        self.assertEqual("canvas", canvas["type"])
        self.assertFalse(canvas["options"]["inlineEditing"])
        elements = list(canvas_elements(canvas["options"]["root"]["elements"]))
        servers = [element for element in elements if element["type"] == "server"]
        self.assertEqual(4, len(servers))
        self.assertEqual({f"replica-{i}-server" for i in range(1, 5)}, {s["name"] for s in servers})
        self.assertEqual(len(elements), len({e["name"] for e in elements}))
        bound_fields = set()
        for element in elements:
            for dimension in element["config"].values():
                if isinstance(dimension, dict) and dimension.get("field"):
                    bound_fields.add(dimension["field"])
            if element["type"] == "server":
                self.assertEqual("Single", element["config"]["type"])
                self.assertEqual("#8E8E8E", element["config"]["statusColor"]["fixed"])
        display_fields = set()
        for override in canvas["fieldConfig"]["overrides"]:
            self.assertEqual("byFrameRefID", override["matcher"]["id"])
            display_fields.update(p["value"] for p in override["properties"] if p["id"] == "displayName")
        self.assertEqual(bound_fields, display_fields)
        self.assertEqual(len(display_fields), len(canvas["targets"]))
        for i in range(1, 5):
            own_queries = [q for q in canvas["targets"] if q["refId"] in (f"MODEL{i}", f"COMPARE{i}", f"FREQUENCY{i}")]
            self.assertEqual(3, len(own_queries))
            for query in own_queries:
                self.assertIn(f'replica="docker-compose_${{deployment}}_{i}"', query["expr"])
                self.assertIn("hycad_ready", query["expr"])
        # Threshold-zero data still uses score>threshold; it is never inferred
        # from the undefined score/threshold ratio or the model's flagged set.
        for query in canvas["targets"]:
            if query["refId"].startswith(("COMPARE", "FREQUENCY")):
                self.assertIn("> bool", query["expr"])
                self.assertNotIn("hycad_replica_anomaly", query["expr"])
                self.assertNotIn(" / ", query["expr"])

    def test_native_server_svg_stays_square_and_clear_of_status_at_all_widths(self):
        for dashboard in (self.dashboard, builder.build_manual()):
            canvas = next(p for p in dashboard["panels"] if p["id"] == 13)
            self.assertEqual(10, canvas["gridPos"]["h"])
            elements = {e["name"]: e for e in canvas["options"]["root"]["elements"]}
            for i in range(1, 5):
                frame = elements[f"replica-{i}-icon-frame"]
                server = frame["elements"][0]
                self.assertEqual("frame", frame["type"])
                self.assertEqual("scale", frame["constraint"]["horizontal"])
                self.assertEqual("center", server["constraint"]["horizontal"])
                self.assertEqual(84, server["placement"]["width"])
                self.assertEqual(84, server["placement"]["height"])
                self.assertNotIn("right", server["placement"])
                status_top = elements[f"replica-{i}-model"]["placement"]["top"]
                svg_bottom = frame["placement"]["top"] + server["placement"]["top"] + server["placement"]["width"]
                self.assertLessEqual(svg_bottom + 8, status_top)
                for panel_width in (480, 640, 900, 1200, 1920):
                    column_width = panel_width * (100 - frame["placement"]["left"] - frame["placement"]["right"]) / 100
                    self.assertGreaterEqual(column_width, server["placement"]["width"])

    def test_canvas_removes_time_typed_fields_before_display_name_binding(self):
        for dashboard in (self.dashboard, builder.build_manual()):
            canvas = next(p for p in dashboard["panels"] if p["id"] == 13)
            include = canvas["transformations"][0]["options"]["include"]
            self.assertEqual("filterFields", canvas["transformations"][0]["id"])
            self.assertEqual({"id": "byType", "options": "number"}, include)
            # A Prometheus instant frame has Time followed by Value. Both can
            # receive the same frame-level display name; type matching must
            # leave only Value even after those names become identical.
            for value in (-1, 0, 1):
                fields = [
                    {"name": "Time", "type": "time", "displayName": "replica_1_model", "value": 1793790206000},
                    {"name": "Value", "type": "number", "displayName": "replica_1_model", "value": value},
                ]
                retained = [f for f in fields if f["type"] == include["options"]]
                self.assertEqual([value], [f["value"] for f in retained])
                self.assertIn(str(retained[0]["value"]), builder.REPLICA_VALUES)

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

    def test_workload_keeps_each_replica_and_filters_invalid_denominators(self):
        for dashboard in (self.dashboard, builder.build_manual()):
            work = next(p for p in dashboard["panels"] if p["id"] == 9)
            self.assertEqual(1, len(work["targets"]))
            query = work["targets"][0]
            expr = query["expr"]
            self.assertEqual("{{replica}}", query["legendFormat"])
            self.assertTrue(query["range"])
            self.assertFalse(query["instant"])
            self.assertIn("hycad_window_events{", expr)
            self.assertRegex(expr, r"hycad_window_accepts\{[^}]+\}\s*>\s*0")
            matching = re.search(r"/\s*on\(([^)]+)\)", expr)
            self.assertIsNotNone(matching)
            self.assertEqual(
                {"deployment", "mode", "capture", "job", "instance", "replica"},
                set(matching.group(1).split(",")),
            )
            # A summed service value or one selected replica would hide the
            # different per-replica workload changes this chart is for.
            self.assertNotRegex(expr, r"\b(?:sum|avg|max|min|topk|bottomk)\s*(?:by|without|\()")
            self.assertNotRegex(expr, r"\breplica\s*(?:=|!=)")
            self.assertNotIn("hycad_channel_c_score", expr)
            self.assertNotIn("hycad_threshold", expr)
            # A zero denominator is missing data, never an artificial count of
            # one, infinity, or a false healthy zero. Freshness gating remains.
            self.assertNotIn("clamp_min", expr)
            self.assertNotIn("> bool", expr)
            self.assertNotIn("or vector", expr)
            self.assertIn("hycad_ready", expr)
            self.assertIn("up{", expr)

    def test_raw_workload_does_not_reuse_score_threshold_scale(self):
        for dashboard in (self.dashboard, builder.build_manual()):
            work = next(p for p in dashboard["panels"] if p["id"] == 9)
            defaults = work["fieldConfig"]["defaults"]
            self.assertEqual("이벤트 / 연결 이벤트", defaults["custom"]["axisLabel"])
            self.assertEqual("linear", defaults["custom"]["scaleDistribution"]["type"])
            self.assertFalse(defaults["custom"]["spanNulls"])
            self.assertEqual("off", defaults["custom"].get("thresholdsStyle", {"mode": "off"})["mode"])
            self.assertTrue(all(step["value"] is None
                                for step in defaults.get("thresholds", {}).get("steps", [])))
            self.assertEqual(4, len(work["fieldConfig"]["overrides"]))

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
        self.assertEqual(6, len(manual["panels"]))
        self.assertNotIn(12, {p["id"] for p in manual["panels"]})
        for panel in manual["panels"]:
            for query in panel["targets"]:
                if query["datasource"]["type"] == "prometheus":
                    self.assertNotIn('job="hycad"', query["expr"])
                    self.assertIn('job="hycad-manual"', query["expr"])
                else:
                    self.assertIn('playback="manual"', query["expr"])
        self.assertIn('playback!="manual"', self.panels[11]["targets"][0]["expr"])
        canvas = next(p for p in manual["panels"] if p["id"] == 13)
        state = next(q for q in canvas["targets"] if q["refId"] == "CONTROL")
        self.assertIn("hycad_manual_state", state["expr"])
        self.assertEqual("캡처 선택 대기", builder.MANUAL_VALUES["0"]["text"])
        self.assertEqual("일시정지", builder.MANUAL_VALUES["2"]["text"])
        self.assertTrue(any(e["name"] == "manual-state" for e in canvas["options"]["root"]["elements"]))
        self.assertFalse(any(p["type"] == "stat" for p in manual["panels"]))
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
