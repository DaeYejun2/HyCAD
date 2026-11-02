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


def crossed(channel):
    threshold = metric("hycad_threshold", 'channel="' + channel + '"')
    return fresh(f'{metric(f"hycad_channel_{channel}_score")} > bool on({MATCH}) group_left() '
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
    p = panel(9, "작업량 탐지기", "timeseries", (0, 15, 24, 8),
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
    timeline = panel(1, "판정 흐름", "state-timeline", (0, 0, 24, 4),
                     "기록 시각의 판정입니다. 캡처 재생의 시간축은 이번 재생 처리 시각이며 원본 캡처 시각은 아래에 별도 표시됩니다. "
                     "회색은 불완전·오래된 입력 또는 연결 불가입니다. 정상 변화 의심은 A 침묵·B 초과·C 안정 조건이며 정상 원인이 입증됐다는 뜻은 아닙니다.")
    timeline["targets"] = [target(CURRENT_VERDICT, legend="판정")]
    timeline["fieldConfig"]["defaults"].update(
        mappings=value_mapping(VERDICT_VALUES), color={"mode": "palette-classic"},
        custom={"lineWidth": 0, "fillOpacity": 90},
        thresholds={"mode": "absolute", "steps": [{"color": "gray", "value": None}]})
    timeline["options"] = {"mergeValues": True, "showValue": "auto", "rowHeight": 0.9,
                           "legend": {"showLegend": False}, "tooltip": {"mode": "single"}, "alignValue": "left"}
    ps.append(timeline)
    ps.append(stat(2, "현재 모델 판정", (0, 4, 5, 4), CURRENT_VERDICT, VERDICT_VALUES,
                   description="전체 이상 의심은 서비스 분류입니다. 실제 임계값 초과 레플리카와 실제 공격 여부는 아래 근거로 구분합니다."))
    ps.append(stat(3, "입력 준비", (5, 4, 4, 4), f"max({READY}) or vector(-1)", {
        "-1": {"text": "연결·선택 확인", "color": "gray"},
        "0": {"text": "판정 불가", "color": "orange"},
        "1": {"text": "판정 가능", "color": "green"}},
        description="모든 레플리카·완전한 30초 창·최신 입력이 필요합니다. HTTP 연결만으로 정상 판정을 표시하지 않습니다."))
    ps.append(stat(4, "B 초과 레플리카", (9, 4, 4, 4), f"sum({crossed('b')}) or vector(-1)",
                   {"-1": {"text": "판정 불가", "color": "gray"}},
                   description="현재 창에서 자신의 B 점수가 기준선 임계값을 넘은 레플리카 수입니다. 전체 이상 의심이 곧 4개 모두 초과했다는 뜻은 아닙니다."))
    drift = fresh(metric("hycad_verdict", 'verdict="drift"'))
    ps.append(stat(5, "Normality drift", (13, 4, 5, 4),
                   f'max({drift}) or vector(-1)', {
                       "-1": {"text": "판정 불가", "color": "gray"},
                       "0": {"text": "현재 미관측", "color": "text"},
                       "1": {"text": "정상 변화 의심", "color": "blue"}},
                   description="비교 탐지기가 지목하지 않고 시스템콜 빈도 탐지기는 초과하며 작업량 탐지기는 안정 범위일 때 표시합니다. 버전이나 부하 등 정상 원인은 별도 확인이 필요합니다."))
    ps.append(stat(6, "입력 원본 구간 종료", (18, 4, 6, 4),
                   f'max({metric("hycad_window_end_timestamp_seconds")} and on(job,instance) (up{{job="hycad"}} == 1)) * 1000',
                   unit="dateTimeAsIso", description="캡처 모드에서는 과거 캡처의 시각입니다. LIVE 입력 상태와 혼동하지 않습니다."))
    ps.append(ratio_chart(7, "비교 탐지기", (0, 8, 12, 7), "a",
                          "동일한 30초 구간의 레플리카 간 특성 차이를 비교합니다. "
                          "임계값은 유효한 정상 기준선의 모든 구간과 레플리카에서 관측한 비교 점수 최댓값의 2배입니다. "
                          "세로축은 점수 / 임계값이며 1보다 크면 초과입니다. 임계값이 0이면 비율은 표시하지 않고 레플리카 표에서 실제 초과를 확인합니다."))
    ps.append(ratio_chart(8, "시스템콜 빈도 탐지기", (12, 8, 12, 7), "b",
                          "정상 기준선의 시스템콜 구성비 평균과 표준편차로 z 점수를 계산합니다. "
                          "레플리카 점수는 시스템콜별 절댓값 z 점수의 최댓값이고, 임계값은 정상 구간의 모든 레플리카 점수 중 최댓값입니다. "
                          "표준편차 하한은 0.00001입니다. 전체 레플리카의 25% 이상이 초과하면 서비스 수준의 빈도 이상 조건을 충족합니다. "
                          "세로축은 점수 / 임계값이며 1보다 크면 초과입니다. 로그축이므로 0은 표시되지 않습니다."))
    ps.append(work_chart())
    replicas = panel(10, "레플리카별 판정", "table", (0, 23, 24, 6),
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
    reasons = panel(11, "최근 판단 근거", "logs", (0, 29, 24, 6),
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
    y = 36
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
               "type": "row", "collapsed": True, "gridPos": {"x": 0, "y": 35, "w": 24, "h": 1},
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
        if p["id"] == 1:
            p["title"] = "수동 재생 흐름"
        if p["id"] == 3:
            p["title"] = "재생 제어 상태"
            p["description"] = "위 '캡처 선택 · 시작/정지 · 배속' 버튼으로 재생을 제어합니다. 선택 전에는 대기합니다. 일시정지·중단·완료 시 점수와 판정을 유효 상태로 유지하지 않습니다."
            p["targets"] = [target(
                'max(hycad_manual_state{job="hycad-manual",deployment="$deployment",mode="$mode",capture="$capture"}'
                ' and on(job,instance) (up{job="hycad-manual"} == 1)) or vector(-1)', instant=True)]
            p["fieldConfig"]["defaults"]["mappings"] = value_mapping({
                "-1": {"text": "연결·선택 확인", "color": "gray"},
                "0": {"text": "캡처 선택 대기", "color": "text"},
                "1": {"text": "재생 중", "color": "green"},
                "2": {"text": "일시정지", "color": "yellow"},
                "3": {"text": "재생 완료", "color": "blue"},
                "4": {"text": "중단됨", "color": "text"},
                "5": {"text": "재생 오류", "color": "red"}})
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
