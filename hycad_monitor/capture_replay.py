"""Continuously re-evaluate a bounded, hash-pinned catalog of saved captures.

Each catalog entry has its own frozen baseline, detector, state and episode
tracker. Grafana selects already running capture results; it cannot choose a
filesystem path or execute a server command. Live collectors are never touched.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import GaugeMetricFamily

from .decision_log import DecisionLog
from .engine import Detector, MAX_CACHE_BYTES, _cache_snapshots, _read_cache, _replica_mapping
from .explain import explain
from .exporter import MetricsCollector, MonitorState
from .incidents import IncidentTracker

LOG = logging.getLogger("hycad.capture")
MAX_MANIFEST_BYTES = 256 * 1024
MAX_CAPTURES = 64
MAX_TOTAL_CACHE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_WINDOWS = 20_000
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _text(value, name: str, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(not char.isprintable() for char in value)):
        raise ValueError(f"Invalid {name}")
    return value


def _path(root: Path, value, name: str) -> Path:
    value = _text(value, name, 512)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"{name} must be a relative path inside the catalog directory")
    resolved = (root / relative).resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"{name} escapes the catalog directory or is not a file")
    if resolved.stat().st_size > MAX_CACHE_BYTES:
        raise ValueError(f"{name} exceeds the per-file byte limit")
    return resolved


@dataclass(frozen=True)
class CaptureSpec:
    id: str
    deployment: str
    title: str
    cache: Path
    cache_sha256: str
    baseline: Path
    baseline_sha256: str
    reference_description: str
    replicas: list[str] | None = None
    source_name: str = ""
    source_sha256: str = ""


def load_catalog(path: Path) -> list[CaptureSpec]:
    """Validate the administrator-owned allowlist before loading any pickle."""
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Catalog exceeds the byte limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1):
        raise ValueError("Catalog schema_version must be 1")
    rows = data.get("captures")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CAPTURES:
        raise ValueError(f"Catalog must contain 1 to {MAX_CAPTURES} captures")
    entries, identifiers = [], set()
    total_bytes = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Capture must be an object")
        identifier, deployment = row.get("id"), row.get("deployment")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError("Invalid capture ID")
        if identifier in identifiers:
            raise ValueError("Duplicate capture ID")
        identifiers.add(identifier)
        if not isinstance(deployment, str) or not IDENTIFIER.fullmatch(deployment):
            raise ValueError("Invalid deployment ID")
        title = _text(row.get("title"), "capture title", 128)
        reference = _text(row.get("reference_description"), "reference description", 200)
        cache = _path(path.parent, row.get("cache"), "cache")
        baseline = _path(path.parent, row.get("baseline"), "baseline")
        # Count repeated baselines too: every entry owns a separate detector.
        total_bytes += cache.stat().st_size + baseline.stat().st_size
        if total_bytes > MAX_TOTAL_CACHE_BYTES:
            raise ValueError("Catalog exceeds the total cache byte limit")
        for field in ("cache_sha256", "baseline_sha256"):
            if not isinstance(row.get(field), str) or not SHA256.fullmatch(row[field]):
                raise ValueError(f"Invalid {field}")
        source_hash = row.get("source_sha256", "")
        if not isinstance(source_hash, str) or source_hash and not SHA256.fullmatch(source_hash):
            raise ValueError("Invalid source_sha256")
        source_name = row.get("source_name", "")
        if not isinstance(source_name, str):
            raise ValueError("Invalid source_name")
        if source_name:
            _text(source_name, "source name", 256)
            if source_name != Path(source_name).name or "/" in source_name or "\\" in source_name:
                raise ValueError("source_name must be a filename, not a path")
        replicas = row.get("replicas")
        if replicas is not None and (not isinstance(replicas, list) or not 2 <= len(replicas) <= 64
                or any(not isinstance(rep, str) or not rep or len(rep) > 256 for rep in replicas)
                or len(set(replicas)) != len(replicas)):
            raise ValueError("Invalid replica mapping")
        entries.append(CaptureSpec(identifier, deployment, title, cache, row["cache_sha256"],
                                   baseline, row["baseline_sha256"], reference, replicas,
                                   source_name, source_hash))
    return entries


class CapturePlayback:
    """One capture's replay state; cycle boundaries reset model vocabulary/state."""

    def __init__(self, spec: CaptureSpec, interval: float = 5, *, clock=time.time):
        if not math.isfinite(interval) or not 1 <= interval <= 300:
            raise ValueError("Replay interval must be between 1 and 300 seconds")
        self.spec, self.interval, self.clock = spec, interval, clock
        data, actual_hash = _read_cache(spec.cache)
        if actual_hash != spec.cache_sha256:
            raise ValueError(f"Cache hash mismatch for capture {spec.id}")
        self.detector = self._new_detector()
        _, mapping = _replica_mapping(data, self.detector.replicas)
        self.snapshots = _cache_snapshots(data, mapping)
        if not self.snapshots or len(self.snapshots) > MAX_TOTAL_WINDOWS:
            raise ValueError("Capture exceeds the window count limit")
        self.state = MonitorState(spec.deployment, "replay", spec.baseline_sha256,
                                  stale_seconds=max(90, interval * 3), clock=clock,
                                  capture=spec.id, reference_description=spec.reference_description)
        self.tracker = IncidentTracker()
        self.index, self.cycle = 0, 1
        self.lock = threading.Lock()

    def _new_detector(self) -> Detector:
        detector = Detector(self.spec.baseline, self.spec.replicas)
        if detector.baseline_sha256 != self.spec.baseline_sha256:
            raise ValueError(f"Baseline hash mismatch for capture {self.spec.id}")
        return detector

    def step(self) -> dict:
        """Publish one historical source window, never a live incident."""
        if self.index == len(self.snapshots):
            self.detector = self._new_detector()
            self.tracker = IncidentTracker()
            self.index = 0
            self.cycle += 1
        snapshot = self.snapshots[self.index]
        result = self.detector.evaluate(snapshot)
        result.update(mode="replay", deployment=self.spec.deployment, capture=self.spec.id,
                      source=self.spec.source_name or self.spec.id, processed_at=self.clock(),
                      capture_title=self.spec.title, reference_description=self.spec.reference_description,
                      cache_sha256=self.spec.cache_sha256, source_sha256=self.spec.source_sha256,
                      replay_cycle=self.cycle, replay_window_index=self.index + 1,
                      replay_window_count=len(self.snapshots), replay_interval_seconds=self.interval)
        result["explanation"] = explain(snapshot, result, self.detector)
        start = datetime.fromtimestamp(result["window_start"], timezone.utc).isoformat()
        end = datetime.fromtimestamp(result["window_end"], timezone.utc).isoformat()
        result["explanation"]["source_window"].update(start_iso=start, end_iso=end)
        result["explanation"]["summary"] = (
            f"[CAPTURE {self.spec.id} | 원본 {start} ~ {end}] " + result["explanation"]["summary"])
        result["incidents"] = self.tracker.update(result)
        for incident in result["incidents"]:
            incident["incident_id"] = f"{self.spec.id}-cycle{self.cycle}-" + incident["incident_id"]
            incident.update(capture=self.spec.id, replay_cycle=self.cycle)
        with self.lock:
            self.state.publish(result)
            self.index += 1
        return result

    def metadata(self) -> dict:
        with self.lock:
            return {"id": self.spec.id, "deployment": self.spec.deployment, "mode": "replay",
                    "title": self.spec.title, "source_name": self.spec.source_name,
                    "cache_sha256": self.spec.cache_sha256,
                    "source_sha256": self.spec.source_sha256,
                    "baseline_sha256": self.spec.baseline_sha256,
                    "reference_description": self.spec.reference_description,
                    "source_start": self.snapshots[0]["start"], "source_end": self.snapshots[-1]["end"],
                    "window_count": len(self.snapshots), "window_index": self.index,
                    "cycle": self.cycle, "interval_seconds": self.interval,
                    "loop": True, "normality_verified": False}


class CaptureMetrics:
    """Merge equal metric families so each HELP/TYPE is emitted exactly once."""

    def __init__(self, playbacks: list[CapturePlayback]):
        self.playbacks = playbacks

    def collect(self):
        families = {}
        metadata = []
        for playback in self.playbacks:
            for metric in MetricsCollector(playback.state).collect():
                if metric.name in families:
                    families[metric.name].samples.extend(metric.samples)
                else:
                    families[metric.name] = metric
            metadata.append(playback.metadata())
        yield from families.values()
        labels = ["deployment", "mode", "capture"]
        info = GaugeMetricFamily("hycad_capture_info", "Hash-pinned saved capture metadata; not a truth label.",
                                 labels=labels + ["title", "reference_description", "cache_sha256", "source_sha256"])
        for row in metadata:
            info.add_metric([row["deployment"], "replay", row["id"], row["title"],
                             row["reference_description"], row["cache_sha256"], row["source_sha256"]], 1)
        yield info
        fields = (
            ("source_start_timestamp_seconds", "source_start", "Original capture UTC window start."),
            ("source_end_timestamp_seconds", "source_end", "Original capture UTC window end, including incomplete tail."),
            ("window_index", "window_index", "Last published window index, starting at one; zero before playback."),
            ("window_count", "window_count", "Number of source windows, including incomplete windows."),
            ("cycle", "cycle", "Current replay loop; episode/model state resets between loops."),
            ("interval_seconds", "interval_seconds", "Wall seconds per original 30-second source window."),
        )
        for suffix, key, help_text in fields:
            metric = GaugeMetricFamily("hycad_capture_" + suffix, help_text, labels=labels)
            for row in metadata:
                metric.add_metric([row["deployment"], "replay", row["id"]], row[key])
            yield metric
        progress = GaugeMetricFamily("hycad_capture_progress_ratio", "Progress through the current replay loop.", labels=labels)
        for row in metadata:
            progress.add_metric([row["deployment"], "replay", row["id"]], row["window_index"] / row["window_count"])
        yield progress


def build_playbacks(specs: list[CaptureSpec], interval: float = 5) -> list[CapturePlayback]:
    playbacks = []
    total_windows = 0
    for spec in specs:
        playback = CapturePlayback(spec, interval)
        total_windows += len(playback.snapshots) + playback.detector.baseline_windows
        if total_windows > MAX_TOTAL_WINDOWS:
            raise ValueError("Catalog exceeds the total source/baseline window limit")
        playbacks.append(playback)
    return playbacks


def make_handler(playbacks: list[CapturePlayback], registry: CollectorRegistry):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                body, content_type, code = generate_latest(registry), CONTENT_TYPE_LATEST, 200
            elif self.path in ("/catalog", "/status", "/healthz", "/readyz"):
                states = [playback.state.snapshot() for playback in playbacks]
                alive = bool(states)
                ready = alive and all(state["updated"] and state["reason"] != "waiting_or_stale"
                                      and not state["errors"].get("capture_worker_failure") for state in states)
                if self.path == "/catalog":
                    output = {"schema_version": 1, "mode": "replay", "captures": [p.metadata() for p in playbacks]}
                elif self.path == "/status":
                    output = {"mode": "replay", "ready": ready, "captures": states}
                else:
                    output = {"alive": alive, "ready": ready, "mode": "replay", "capture_count": len(playbacks)}
                code = 503 if self.path == "/readyz" and not ready else 200
                body = json.dumps(output, ensure_ascii=False, allow_nan=False).encode()
                content_type = "application/json; charset=utf-8"
            else:
                body, content_type, code = b"Not found", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9110)
    parser.add_argument("--loki-url", default=os.environ.get("HYCAD_LOKI_URL"))
    return parser


def main() -> None:
    argument_parser = parser()
    args = argument_parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        playbacks = build_playbacks(load_catalog(args.manifest), args.interval)
    except (OSError, ValueError, TypeError) as exc:
        argument_parser.exit(1, f"Capture catalog validation failed: {exc}\n")
    registry = CollectorRegistry()
    registry.register(CaptureMetrics(playbacks))
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(playbacks, registry))
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    decision_logs = {playback.spec.id: DecisionLog(args.loki_url, playback.state)
                     for playback in playbacks} if args.loki_url else {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("Capture playback started: %d approved captures, interval=%s seconds", len(playbacks), args.interval)
    try:
        while not stop.is_set():
            started = time.monotonic()
            for playback in playbacks:
                if stop.is_set():
                    break
                try:
                    result = playback.step()
                    if playback.spec.id in decision_logs:
                        decision_logs[playback.spec.id].submit(result)
                except Exception:
                    playback.state.error("capture_worker_failure", invalidate=True)
                    # No traceback, raw event, private filesystem path or command
                    # line enters logs; a failed worker causes service restart.
                    LOG.error("Capture worker failed for approved ID %s", playback.spec.id)
                    raise SystemExit(1)
            stop.wait(max(0.01, args.interval - (time.monotonic() - started)))
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        for log in decision_logs.values():
            log.close()


if __name__ == "__main__":
    main()
