#!/usr/bin/env python3
"""Build the Linux-provisioned dashboard from the project's metric contract.

Layout, A/B threshold ratios, log B axis, and replica colors/line styles adapt
DaeYejun2/HyCAD/Grafana/grafana_hycad_realtime.json (2026-10-23).
No Windows stack, generated backfill, or source research labels are deployed.
Detailed LIVE case views remain in a collapsed row. A separate manual playback
dashboard uses an isolated scrape job and a same-origin authenticated controller.
"""
import copy
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "monitoring/grafana/dashboards/hycad-overview.json"
MANUAL_OUTPUT = OUTPUT.with_name("hycad-replay.json")
INVESTIGATION_TEMPLATE = ROOT / "monitoring/grafana/templates/live-investigation-panels.json"
PROM = {"type": "prometheus", "uid": "hycad-prometheus"}
LOKI = {"type": "loki", "uid": "hycad-loki"}
SCOPE = 'job="hycad",deployment="$deployment",mode="$mode",capture="$capture"'
MATCH = "deployment,mode,capture,job,instance"
DETECTORS = {"A": "비교 탐지기", "B": "시스템콜 빈도 탐지기", "C": "작업량 탐지기"}
LOG_DISPLAY = ''.join(
    f' | replace "{letter}{suffix}" "{name}{suffix}"'
    for letter, name in DETECTORS.items() for suffix in (' ', '=', ':', '/', '는'))
LOG_DISPLAY += ' | replace " · " " " | replace "·" " "'


def display_text(value):
    """Expand research abbreviations in display text, never metric/schema keys."""
    value = value.replace("·", " ")
    value = re.sub(r"(?<![A-Za-z0-9_])[ABC](?![A-Za-z0-9_])",
                   lambda match: DETECTORS[match.group()], value)
    return re.sub(r" {2,}", " ", value)


def clean_display(value):
    """Keep imported investigation panels consistent without changing queries."""
    if isinstance(value, list):
        return [clean_display(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if "·" in key and re.search(r"[가-힣]", key):
                key = display_text(key)
            if key == "renameByName":
                result[key] = {field: display_text(label) for field, label in item.items()}
            elif key in {"title", "description", "noValue", "text", "displayName", "axisLabel"} and isinstance(item, str):
                result[key] = display_text(item)
            elif key == "matcher" and item.get("id") == "byName":
                result[key] = dict(item, options=display_text(item["options"]))
            elif key == "value" and isinstance(item, str) and "·" in item:
                result[key] = display_text(item)
            else:
                result[key] = clean_display(item)
        return result
    return value


def metric(name, extra=""):
    return f"{name}{{{SCOPE}{',' + extra if extra else ''}}}"


READY = f'({metric("hycad_ready")} and on(job,instance) (up{{job="hycad"}} == 1))'


def fresh(expr):
    return f"({expr}) and on({MATCH}) ({READY} == 1)"


def ratio(channel):
    score = metric(f"hycad_channel_{channel}_score")
    threshold = metric("hycad_threshold", f'channel="{channel}"')
    join = " group_left()" if channel in "ab" else ""
    # A zero threshold is undefined as a ratio; never silently replace it by 1.
    return fresh(f"{score} / on({MATCH}){join} ({threshold} > 0)")


def crossed(channel, replica=None):
    threshold = metric("hycad_threshold", 'channel="' + channel + '"')
    extra = f'replica="{replica}"' if replica else ""
    return fresh(f'{metric(f"hycad_channel_{channel}_score", extra)} > bool on({MATCH}) group_left() '
                 f'{threshold}')


VERDICT = "sum without(verdict) (" + " or ".join(
    '(' + metric("hycad_verdict", 'verdict="' + v + '"') + f' * {i})'
    for i, v in enumerate(("normal", "partial_infection", "all_infection", "drift", "insufficient_data"))
) + ")"
CURRENT_VERDICT = f"max({fresh(VERDICT)}) or vector(4)"
VERDICT_VALUES = {
    "0": {"text": "정상 범위", "color": "green"},
    "1": {"text": "부분 이상 의심", "color": "orange"},
    "2": {"text": "전체 이상 의심", "color": "red"},
    "3": {"text": "정상 변화 의심", "color": "blue"},
    "4": {"text": "판정 불가", "color": "gray"},
}


def value_mapping(values):
    return [{"type": "value", "options": values}]


def target(expr, ref="A", *, instant=False, table=False, legend=""):
    return {"datasource": PROM, "editorMode": "code", "expr": expr, "refId": ref,
            "range": not instant, "instant": instant,
            "format": "table" if table else "time_series", "legendFormat": legend}


def panel(pid, title, kind, pos, description=""):
    return {"id": pid, "title": title, "type": kind, "datasource": PROM,
            "gridPos": dict(zip(("x", "y", "w", "h"), pos)), "description": description,
            "fieldConfig": {"defaults": {"noValue": "판정 불가 · 입력 확인"}, "overrides": []},
            "options": {}, "targets": []}


def stat(pid, title, pos, expr, mapping=None, unit="none", description=""):
    p = panel(pid, title, "stat", pos, description)
    p["fieldConfig"]["defaults"].update(
        unit=unit, decimals=0, color={"mode": "thresholds"},
        mappings=value_mapping(mapping) if mapping else [],
        thresholds={"mode": "absolute", "steps": [{"color": "text", "value": None}]})
    p["options"] = {"reduceOptions": {"calcs": ["last"], "fields": "", "values": False},
                    "orientation": "auto", "colorMode": "value", "graphMode": "none",
                    "justifyMode": "auto", "textMode": "auto", "wideLayout": True}
    p["targets"] = [target(expr, instant=True)]
    return p


def replica_styles():
    result = []
    for i, (color, dash) in enumerate(zip(
            ("#0072B2", "#D55E00", "#009E73", "#CC79A7"),
            (None, [12, 6], [3, 4], [14, 5, 3, 5])), 1):
        props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]
        if dash:
            props.append({"id": "custom.lineStyle", "value": {"fill": "dash", "dash": dash}})
        result.append({"matcher": {"id": "byRegexp", "options": f".*{i}$"}, "properties": props})
    return result


REPLICA_VALUES = {
    "-1": {"text": "판정 불가", "color": "#8E8E8E"},
    # Unflagged is deliberately not labelled "normal": a service-level drift
    # or a partially suspicious service can still have unflagged replicas.
    "0": {"text": "지목 없음", "color": "#5794F2"},
    "1": {"text": "이상 의심", "color": "#FF9830"},
}
EXCEEDED_VALUES = {
    "-1": {"text": "판정 불가", "color": "#8E8E8E"},
    "0": {"text": "초과 없음", "color": "text"},
    "1": {"text": "실제 초과", "color": "#FF9830"},
}
MANUAL_VALUES = {
    "-1": {"text": "재생 연결 확인", "color": "gray"},
    "0": {"text": "캡처 선택 대기", "color": "text"},
    "1": {"text": "재생 중", "color": "green"},
    "2": {"text": "일시정지", "color": "yellow"},
    "3": {"text": "재생 완료", "color": "blue"},
    "4": {"text": "중단됨", "color": "text"},
    "5": {"text": "재생 오류", "color": "red"},
}


def canvas_element(name, kind, left, top, width, height, config):
    # Canvas's horizontal scale constraint uses percentage left/right offsets.
    # Vertical pixels keep labels readable while the four columns fit the panel.
    return {"name": name, "type": kind, "config": config,
            "placement": {"left": left, "right": 100 - left - width,
                          "top": top, "height": height},
            "constraint": {"horizontal": "scale", "vertical": "top"},
            "background": {"color": {"fixed": "transparent"}},
            "border": {"width": 0, "color": {"fixed": "transparent"}}}


def canvas_text(name, text, left, top, width, height, *, field=None, size=14, align="center"):
    # Native text supports field dimensions with a fixed fallback. Unlike the
    # metric-value element, it does not expose "Field not found" to viewers.
    return canvas_element(name, "text", left, top, width, height, {
        "text": {"mode": "field", "field": field, "fixed": text} if field else {"mode": "fixed", "fixed": text},
        "align": align, "valign": "middle", "size": size,
        "color": {"field": field, "fixed": "#8E8E8E"} if field else {"fixed": "text"}})


def canvas_field(p, ref, field, expr, mappings):
    p["targets"].append(target(expr, ref, instant=True, legend=field))
    # Binding by a unique display name avoids ambiguous Value/Value #A fields
    # when a canvas consumes several Prometheus instant-query frames.
    p["fieldConfig"]["overrides"].append({
        "matcher": {"id": "byFrameRefID", "options": ref},
        "properties": [{"id": "displayName", "value": field},
                       {"id": "mappings", "value": value_mapping(mappings)}]})


def canvas_server(i, left, field):
    # The native server renders a square SVG whose intrinsic height follows
    # its width. Do not stretch its width with a percentage constraint: wide
    # panels would grow the SVG past the status text below it. A responsive
    # column frame centers a fixed square, retaining the same four-column grid.
    frame = canvas_element(f"replica-{i}-icon-frame", "frame", left, 59, 22, 90, {})
    frame["elements"] = [{
        "name": f"replica-{i}-server", "type": "server",
        "placement": {"left": 0, "top": 0, "width": 84, "height": 84},
        "constraint": {"horizontal": "center", "vertical": "top"},
        "background": {"color": {"fixed": "transparent"}},
        "border": {"width": 0, "color": {"fixed": "transparent"}},
        "config": {"type": "Single", "statusColor": {"field": field, "fixed": "#8E8E8E"},
                   "bulbColor": {"field": field, "fixed": "#8E8E8E"}, "blinkRate": {"fixed": 0}},
    }]
    return frame


def replica_canvas():
    """Native Grafana Canvas: server, text and field-color dimensions only.

    Schema reference: Grafana's own canvas-examples.json and native elements:
    https://github.com/grafana/grafana/tree/v13.2.4/public/app/features/canvas/elements
    https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/canvas/
    No external images, HTML, plugins, or unauthenticated action buttons.
    """
    p = panel(13, "레플리카 상태", "canvas", (0, 0, 24, 10),
              "네 개의 그림은 선택한 서비스의 레플리카입니다. 그림 색과 큰 문구는 모델의 지목이며 공격 확정이 아닙니다. "
              "아래 두 줄은 비교 탐지기와 시스템콜 빈도 탐지기가 각 레플리카에서 실제 임계값을 초과했는지 표시합니다. "
              "전체 이상 의심에서도 실제 초과가 없는 레플리카가 모델에 지목될 수 있습니다. "
              "판정 가능한 최신 30초 입력이 없으면 회색 판정 불가로 표시합니다. 지목 없음은 정상 원인 확정을 뜻하지 않습니다.")
    p["fieldConfig"]["defaults"].update(
        unit="none", decimals=0, color={"mode": "thresholds"}, noValue="판정 불가",
        mappings=value_mapping(REPLICA_VALUES),
        thresholds={"mode": "absolute", "steps": [{"color": "#8E8E8E", "value": None}]})
    # A byFrameRefID display-name override also renames the Time field. Canvas
    # searches the first matching display name, so remove time-typed fields
    # before binding. Match by type, not "Time", to remain safe irrespective of
    # whether display names were already applied by the panel query pipeline.
    p["transformations"] = [{"id": "filterFields", "options": {
        "include": {"id": "byType", "options": "number"}}}]
    elements = [
        canvas_text("service-classification-label", "서비스 분류", 2, 4, 10, 22, size=12, align="left"),
        canvas_text("service-classification", "판정 불가", 12, 4, 26, 22,
                    field="service_classification", size=14, align="left"),
    ]
    canvas_field(p, "SERVICE", "service_classification", CURRENT_VERDICT, VERDICT_VALUES)
    for i in range(1, 5):
        left = 2 + (i - 1) * 24
        replica = f"docker-compose_${{deployment}}_{i}"
        state_field = f"replica_{i}_model"
        compare_field = f"replica_{i}_comparison"
        frequency_field = f"replica_{i}_frequency"
        # Select the exact known container identity; a renamed/missing replica
        # must become unknown rather than being assigned another replica's data.
        flagged = fresh(metric("hycad_replica_anomaly", f'replica="{replica}"'))
        canvas_field(p, f"MODEL{i}", state_field, f"max({flagged}) or vector(-1)", REPLICA_VALUES)
        canvas_field(p, f"COMPARE{i}", compare_field, f"max({crossed('a', replica)}) or vector(-1)", EXCEEDED_VALUES)
        canvas_field(p, f"FREQUENCY{i}", frequency_field, f"max({crossed('b', replica)}) or vector(-1)", EXCEEDED_VALUES)
        elements.extend([
            canvas_text(f"replica-{i}-label", f"레플리카 {i}", left, 33, 22, 24, size=18),
            canvas_server(i, left, state_field),
            canvas_text(f"replica-{i}-model", "판정 불가", left, 151, 22, 28,
                        field=state_field, size=20),
            canvas_text(f"replica-{i}-comparison-label", "비교 탐지기", left, 188, 14, 20, size=12, align="left"),
            canvas_text(f"replica-{i}-comparison", "판정 불가", left + 14, 188, 8, 20,
                        field=compare_field, size=12, align="right"),
            canvas_text(f"replica-{i}-frequency-label", "시스템콜 빈도 탐지기", left, 211, 14, 20, size=12, align="left"),
            canvas_text(f"replica-{i}-frequency", "판정 불가", left + 14, 211, 8, 20,
                        field=frequency_field, size=12, align="right"),
        ])
    elements.append(canvas_text("meaning-legend", "그림은 모델 지목, 아래 두 줄은 실제 임계값 초과", 2, 244, 94, 20,
                                size=11, align="left"))
    p["options"] = {"inlineEditing": False, "showAdvancedTypes": False,
                    "panZoom": False, "zoomToContent": False,
                    "tooltip": {"mode": "single", "disableForOneClick": False},
                    "root": {"name": "hycad-replicas", "type": "frame", "elements": elements,
                             "background": {"color": {"fixed": "transparent"}},
                             "border": {"width": 0, "color": {"fixed": "transparent"}}}}
    return p


def ratio_chart(pid, title, pos, channel, description):
    p = panel(pid, title, "timeseries", pos, description)
    custom = {"drawStyle": "line", "lineInterpolation": "stepAfter", "lineWidth": 2,
              "fillOpacity": 0, "showPoints": "never", "spanNulls": False,
              "axisPlacement": "auto", "axisLabel": "점수 / 임계값",
              "scaleDistribution": {"type": "log", "log": 10} if channel == "b" else {"type": "linear"},
              "thresholdsStyle": {"mode": "dashed+area"}}
    p["fieldConfig"]["defaults"].update(
        unit="none", decimals=2, color={"mode": "palette-classic"}, custom=custom,
        thresholds={"mode": "absolute", "steps": [
            {"color": "transparent", "value": None}, {"color": "#C2353D", "value": 1}]})
    if channel != "b":
        p["fieldConfig"]["defaults"]["min"] = 0
    p["fieldConfig"]["overrides"] = replica_styles() if channel in "ab" else []
    p["targets"] = [target(ratio(channel), legend="{{replica}}" if channel in "ab" else "C / 임계값")]
    if channel in "ab":
        p["transformations"] = [{"id": "renameByRegex", "options": {
            "regex": "^docker-compose_(catalogue|front-end)_([1-4])$", "renamePattern": "$1 $2"}}]
    p["options"] = {"tooltip": {"mode": "multi", "sort": "desc"},
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}}
    return p


def work_chart():
    p = panel(9, "작업량 탐지기", "timeseries", (0, 17, 24, 8),
              "각 선은 한 레플리카의 연결 이벤트당 작업량 변화를 나타내며 모든 레플리카를 함께 표시합니다. "
              "30초 구간의 레플리카별 전체 이벤트 수를 accept와 accept4 이벤트 수로 나눕니다.\n\n"
              "실제 판정은 전체 레플리카의 이벤트 합을 연결 이벤트 합으로 나눈 작업량을 사용합니다. "
              "정상 기준 작업량은 유효한 정상 구간에서 얻은 전체 작업량의 중앙값입니다. "
              "|log2(전체 작업량 / 정상 기준 작업량)| > 0.20이면 임계값 초과입니다. "
              "0.20은 연구 실험 결과를 검토해 정한 고정값이며 현재 정상 기준선에서 자동 산출한 값은 아닙니다. "
              "이는 정상 기준의 약 0.871배 미만 또는 1.149배 초과에 해당합니다.\n\n"
              "그래프의 세로축은 레플리카별 실제 작업량이므로 판정 점수 임계값 0.20을 선으로 표시하지 않습니다. "
              "연결 이벤트는 HTTP 요청 수와 다를 수 있으며, 분모가 0이거나 입력이 불완전한 구간은 표시하지 않습니다.")
    p["fieldConfig"]["defaults"].update(
        unit="none", decimals=2, min=0, color={"mode": "palette-classic"},
        custom={"drawStyle": "line", "lineInterpolation": "stepAfter", "lineWidth": 2,
                "fillOpacity": 0, "showPoints": "never", "spanNulls": False,
                "axisPlacement": "auto", "axisLabel": "이벤트 / 연결 이벤트",
                "scaleDistribution": {"type": "linear"}, "thresholdsStyle": {"mode": "off"}})
    p["fieldConfig"]["overrides"] = replica_styles()
    p["targets"] = [target(fresh(
        f'{metric("hycad_window_events")} / on({MATCH},replica) '
        f'({metric("hycad_window_accepts")} > 0)'), legend="{{replica}}")]
    p["transformations"] = [{"id": "renameByRegex", "options": {
        "regex": "^docker-compose_(catalogue|front-end)_([0-9]+)$", "renamePattern": "$1 $2"}}]
    p["options"] = {"tooltip": {"mode": "multi", "sort": "desc"},
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}}
    return p


def query_variable(name, label, query):
    return {"name": name, "label": label, "type": "query", "datasource": PROM,
            "definition": query, "query": {"query": query, "refId": f"VariableQuery-{name}"},
            "refresh": 1, "sort": 1, "multi": False, "includeAll": False, "hide": 0,
            "skipUrlSync": False, "options": [], "current": {}, "regex": ""}


def build():
    d = {"id": None, "uid": "hycad-overview", "title": "HyCAD · LIVE / 캡처 자동 재생",
         "description": "LIVE는 현재 Sysdig 입력, 캡처 재생은 저장 자료의 반복 재판정입니다. "
                        "30초 창을 판정하고 화면은 5초마다 조회합니다. 이상·normality drift는 모델의 의심이며 원인 확정이 아닙니다.",
         "tags": ["HyCAD", "live", "replay"], "timezone": "browser", "schemaVersion": 39,
         "version": 1, "refresh": "5s", "editable": False, "graphTooltip": 1,
         "time": {"from": "now-30m", "to": "now"}, "timepicker": {}, "annotations": {"list": []},
         "links": [{"type": "link", "title": "캡처 수동 재생 화면", "targetBlank": False,
                    "url": "/d/hycad-replay?from=now-30m&to=now"},
                   {"type": "link", "title": "캡처 선택 · 시작/정지 · 배속", "targetBlank": False,
                    "url": "/hycad/replay/"}],
         "panels": []}
    d["templating"] = {"list": [
        query_variable("deployment", "서비스", 'label_values(hycad_window_seconds{job="hycad"}, deployment)'),
        {"name": "mode", "label": "모드", "type": "custom", "query": "LIVE : live, 캡처 재생 : replay",
         "options": [{"text": "LIVE", "value": "live", "selected": True},
                     {"text": "캡처 재생", "value": "replay", "selected": False}],
         "current": {"text": "LIVE", "value": "live"}, "multi": False, "includeAll": False,
         "hide": 0, "skipUrlSync": False},
        query_variable("capture", "입력 캡처", 'label_values(hycad_window_seconds{job="hycad",deployment="$deployment",mode="$mode"}, capture)')
    ]}
    ps = d["panels"]
    ps.append(replica_canvas())
    ps.append(ratio_chart(7, "비교 탐지기", (0, 10, 12, 7), "a",
                          "동일한 30초 구간의 레플리카 간 특성 차이를 비교합니다. "
                          "임계값은 유효한 정상 기준선의 모든 구간과 레플리카에서 관측한 비교 점수 최댓값의 2배입니다. "
                          "세로축은 점수 / 임계값이며 1보다 크면 초과입니다. 임계값이 0이면 비율은 표시하지 않고 레플리카 표에서 실제 초과를 확인합니다."))
    ps.append(ratio_chart(8, "시스템콜 빈도 탐지기", (12, 10, 12, 7), "b",
                          "정상 기준선의 시스템콜 구성비 평균과 표준편차로 z 점수를 계산합니다. "
                          "레플리카 점수는 시스템콜별 절댓값 z 점수의 최댓값이고, 임계값은 정상 구간의 모든 레플리카 점수 중 최댓값입니다. "
                          "표준편차 하한은 0.00001입니다. 전체 레플리카의 25% 이상이 초과하면 서비스 수준의 빈도 이상 조건을 충족합니다. "
                          "세로축은 점수 / 임계값이며 1보다 크면 초과입니다. 로그축이므로 0은 표시되지 않습니다."))
    ps.append(work_chart())
    replicas = panel(10, "레플리카별 판정", "table", (0, 25, 24, 6),
                     "모델 지목과 각 레플리카의 A/B 실제 임계값 초과를 따로 표시합니다. 데이터가 불완전하거나 오래되면 이전 점수를 유지하지 않습니다.")
    replicas["targets"] = [target(f"max by(replica) ({expr})", ref, instant=True, table=True)
                            for ref, expr in (("A", fresh(metric("hycad_replica_anomaly"))),
                                              ("B", crossed("a")), ("C", crossed("b")),
                                              ("D", ratio("a")), ("E", ratio("b")))]
    rename = {"replica": "레플리카", "Value #A": "모델 지목", "Value #B": "A 실제 초과",
              "Value #C": "B 실제 초과", "Value #D": "A 배수", "Value #E": "B 배수"}
    replicas["transformations"] = [
        {"id": "joinByField", "options": {"byField": "replica", "mode": "outerTabular"}},
        {"id": "filterFieldsByName", "options": {"include": {"pattern": "^(replica|Value #[A-E])$"}}},
        {"id": "organize", "options": {"renameByName": rename, "indexByName": {k: i for i, k in enumerate(rename)}}}]
    replicas["fieldConfig"]["defaults"].update(decimals=2, custom={"align": "auto", "cellOptions": {"type": "auto"}})
    replicas["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": "모델 지목"}, "properties": [
            {"id": "mappings", "value": value_mapping({"0": {"text": "없음"}, "1": {"text": "지목", "color": "orange"}})},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
        {"matcher": {"id": "byRegexp", "options": "(비교 탐지기|시스템콜 빈도 탐지기) 실제 초과"}, "properties": [
            {"id": "mappings", "value": value_mapping({"0": {"text": "없음"}, "1": {"text": "초과", "color": "orange"}})},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
        {"matcher": {"id": "byName", "options": "레플리카"}, "properties": [
            {"id": "mappings", "value": value_mapping({f"docker-compose_{service}_{i}": {"text": f"{service} {i}"}
                                                        for service in ("catalogue", "front-end") for i in range(1, 5)})},
            {"id": "custom.width", "value": 160}]}]
    replicas["options"] = {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "레플리카", "desc": False}]}
    ps.append(replicas)
    reasons = panel(11, "최근 판단 근거", "logs", (0, 31, 24, 6),
                    "완료된 창의 짧은 모델 설명입니다. 처리 시각과 원본 구간을 함께 확인하세요. 공격·정상 원인 검토와 전후 자료는 LIVE 사건 조사에서 확인합니다.")
    reasons["datasource"] = LOKI
    reasons["targets"] = [{"refId": "A", "datasource": LOKI, "editorMode": "code", "queryType": "range",
                           "maxLines": 3, "direction": "backward",
                           "expr": '{job="hycad-decisions",deployment="$deployment",mode="$mode",capture="$capture",playback!="manual"}'
                                   ' | json summary="explanation.summary" | line_format `{{ .summary'
                                   ' | replace "부분 감염" "부분 이상 의심" | replace "전원 감염" "전체 이상 의심"'
                                   ' | replace "docker-compose_" "" | replace "catalogue_" "catalogue "'
                                   ' | replace "front-end_" "front-end "' + LOG_DISPLAY + ' }}`'}]
    reasons["fieldConfig"]["defaults"]["noValue"] = "해당 입력의 판단 설명 없음"
    reasons["options"] = {"showTime": True, "showLabels": False, "showCommonLabels": False,
                          "wrapLogMessage": True, "prettifyLogMessage": False,
                          "enableLogDetails": True, "dedupStrategy": "none", "sortOrder": "Descending"}
    ps.append(reasons)
    details = json.loads(INVESTIGATION_TEMPLATE.read_text(encoding="utf-8"))
    y = 38
    detail_titles = {25: "사건 판정", 26: "검토 결과", 27: "원인 검토", 22: "레플리카별 지목", 23: "조사 자료", 21: "판정 근거 상세"}
    for item in details:
        item["title"] = detail_titles.get(item["id"], item["title"])
        for query in item.get("targets", []):
            query["expr"] = query["expr"].replace(' }}`', LOG_DISPLAY + ' }}`')
        for override in item.get("fieldConfig", {}).get("overrides", []):
            if override["matcher"].get("options") in ("A 실제 초과", "B 실제 초과"):
                for prop in override["properties"]:
                    if prop["id"] == "custom.width":
                        prop["value"] = 220
        item["gridPos"].update(x=0, y=y, w=24)
        y += item["gridPos"]["h"]
    ps.append({"id": 90, "title": "LIVE 사건 상세",
               "type": "row", "collapsed": True, "gridPos": {"x": 0, "y": 37, "w": 24, "h": 1},
               "panels": details})
    return clean_display(d)


def build_manual():
    d = copy.deepcopy(build())
    d.update(uid="hycad-replay", title="HyCAD · 캡처 수동 재생", tags=["HyCAD", "manual-replay"],
             description="선택한 저장 캡처를 시작·일시정지·중단하고 배속을 조절합니다. "
                         "이 화면은 별도 수동 재생 프로세스만 조회하며 LIVE와 자동 순환 재생을 변경하지 않습니다.")
    d["links"] = [
        {"type": "link", "title": "캡처 선택 · 시작/정지 · 배속", "targetBlank": False, "url": "/hycad/replay/"},
        {"type": "link", "title": "LIVE / 캡처 자동 재생 화면", "targetBlank": False,
         "url": "/d/hycad-overview?var-mode=live&var-capture=live&from=now-30m&to=now"}]
    d["panels"] = [p for p in d["panels"] if p["type"] != "row"]
    for p in d["panels"]:
        for t in p.get("targets", []):
            t["expr"] = t["expr"].replace('job="hycad"', 'job="hycad-manual"')
            t["expr"] = t["expr"].replace('playback!="manual"', 'playback="manual"')
        if p["id"] == 13:
            p["options"]["root"]["elements"].extend([
                canvas_text("manual-state-label", "수동 재생", 65, 4, 12, 22, size=12, align="right"),
                canvas_text("manual-state", "재생 연결 확인", 78, 4, 18, 22,
                            field="manual_control_state", size=13, align="right"),
            ])
            canvas_field(p, "CONTROL", "manual_control_state",
                         'max(hycad_manual_state{job="hycad-manual",deployment="$deployment",mode="$mode",capture="$capture"}'
                         ' and on(job,instance) (up{job="hycad-manual"} == 1)) or vector(-1)', MANUAL_VALUES)
        if p["id"] == 11:
            p["description"] = "현재 선택 캡처의 수동 재생 판단 근거입니다. 표시 시각은 이번 처리 시각이며 설명 안의 원본 구간과 구분합니다."
    for v in d["templating"]["list"]:
        if v["name"] == "mode":
            v.update(type="constant", query="replay", hide=2,
                     current={"text": "캡처 수동 재생", "value": "replay"},
                     options=[{"text": "캡처 수동 재생", "value": "replay", "selected": True}])
        else:
            labels = 'job="hycad-manual",mode="replay"'
            if v["name"] == "capture":
                labels += ',deployment="$deployment"'
            # label_values is a historical series lookup: an old idle/capture
            # would otherwise become the hidden default on a direct revisit.
            # Query the single active controller state at the current end time.
            v["definition"] = (
                f'query_result(hycad_manual_state{{{labels}}} '
                'and on(job,instance) (up{job="hycad-manual"} == 1))')
            v["query"]["query"] = v["definition"]
            v["regex"] = '/' + v["name"] + '="([^"]+)"/'
            v["refresh"] = 2
            v["hide"] = 2
    return clean_display(d)


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MANUAL_OUTPUT.write_text(json.dumps(build_manual(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(OUTPUT.relative_to(ROOT))
    print(MANUAL_OUTPUT.relative_to(ROOT))
