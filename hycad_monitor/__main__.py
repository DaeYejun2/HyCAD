"""Run cache replay or tail live Sysdig JSONL and expose Prometheus metrics."""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from .engine import Detector, load_cache_snapshots
from .stream import EventWindowAggregator
from .exporter import MonitorState, MetricsCollector
from .explain import explain
from .decision_log import DecisionLog
from .incidents import IncidentTracker

LOG = logging.getLogger('hycad')
MAX_LINE = 1024 * 1024


def follow_file(path, stop):
    """Start at EOF, follow append/rotation/truncation, retain partial JSON lines."""
    handle, identity, pending, discard = None, None, b'', False
    first_open = True
    try:
        while not stop.is_set():
            try:
                stat = path.stat()
                changed = identity != (stat.st_dev, stat.st_ino)
                if handle is None or changed or stat.st_size < handle.tell():
                    reset = not first_open
                    if handle:
                        handle.close()
                    handle = path.open('rb')
                    identity = stat.st_dev, stat.st_ino
                    if first_open:
                        handle.seek(0, os.SEEK_END)
                        first_open = False
                    pending, discard = b'', False
                    if reset:
                        yield b'!source_reset'
                chunk = handle.read(65536)
            except FileNotFoundError:
                chunk = b''
            if not chunk:
                yield None
                stop.wait(0.2)
                continue
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if discard or len(line) > MAX_LINE:
                    yield b'!oversized'
                    discard = False
                elif line.strip():
                    yield line
            if len(pending) > MAX_LINE:
                pending = b''
                discard = True
    finally:
        if handle:
            handle.close()


def make_handler(state, registry):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/metrics':
                body, content_type, code = generate_latest(registry), CONTENT_TYPE_LATEST, 200
            elif self.path in ('/healthz', '/readyz', '/status'):
                snapshot = state.snapshot()
                code = 503 if self.path == '/readyz' and not snapshot['ready'] else 200
                body = json.dumps(snapshot if self.path == '/status' else
                                  {'alive': True, 'ready': snapshot['ready'], 'mode': state.mode},
                                  ensure_ascii=False, allow_nan=False).encode()
                content_type = 'application/json; charset=utf-8'
            else:
                code, body, content_type = 404, b'Not found', 'text/plain'
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['replay', 'live'], default='replay')
    p.add_argument('--baseline', type=Path, default=Path('cache/cat_baseline.pkl'))
    p.add_argument('--replay', nargs='+', type=Path,
                   default=[Path('cache/cat_baseline.pkl'), Path('cache/cat_1of4.pkl'), Path('cache/cat_4of4.pkl')])
    p.add_argument('--interval', type=float, default=5)
    p.add_argument('--loop', action='store_true')
    p.add_argument('--events', type=Path)
    p.add_argument('--replicas', help='Comma-separated replica IDs, mapping sorted baseline replicas in this order.')
    p.add_argument('--deployment', default='catalogue')
    p.add_argument('--capture-id', default=None, help='Stable capture identity; live defaults to live.')
    p.add_argument('--reference-description', default='', help='Short baseline provenance shown in the dashboard.')
    p.add_argument('--bind', default='127.0.0.1')
    p.add_argument('--port', type=int, default=9108)
    p.add_argument('--stale-seconds', type=float, default=90)
    p.add_argument('--journal', type=Path, help='Optional append-only decision JSONL file; rotate externally.')
    p.add_argument('--incident-state', type=Path, help='Optional persistent episode state for restart continuity.')
    p.add_argument('--evidence-health', type=Path, help='Optional read-only preservation worker health file.')
    p.add_argument('--loki-url', default=os.environ.get('HYCAD_LOKI_URL'), help='Optional Loki push endpoint for decision explanations.')
    return p


def main():
    p = parser()
    args = p.parse_args()
    if args.interval <= 0 or args.stale_seconds < 30:
        p.error('interval must be positive and stale-seconds must be at least 30')
    if args.mode == 'live' and args.events is None:
        p.error('--events is required in live mode')
    if len(args.deployment) > 128:
        p.error('deployment must be at most 128 characters')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    detector = Detector(args.baseline, args.replicas.split(',') if args.replicas else None)
    state = MonitorState(args.deployment, args.mode, detector.baseline_sha256, args.stale_seconds, evidence_health=args.evidence_health, capture=args.capture_id, reference_description=args.reference_description)
    decision_log = DecisionLog(args.loki_url, state) if args.loki_url else None
    registry = CollectorRegistry()
    registry.register(MetricsCollector(state))
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(state, registry))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    incident_tracker = IncidentTracker(args.incident_state)
    journal = None
    if args.journal:
        args.journal.parent.mkdir(parents=True, exist_ok=True)
        journal = args.journal.open('a', encoding='utf-8')

    def publish(snapshot, source):
        result = detector.evaluate(snapshot)
        result.update(mode=args.mode, deployment=args.deployment, capture=state.capture, source=source, processed_at=time.time())
        result['explanation'] = explain(snapshot, result, detector)
        source_start = datetime.fromtimestamp(result['window_start'], timezone.utc).isoformat()
        source_end = datetime.fromtimestamp(result['window_end'], timezone.utc).isoformat()
        result['explanation']['source_window'].update(start_iso=source_start, end_iso=source_end)
        result['explanation']['summary'] = (f"[{args.mode.upper()} | 원본 {source_start} ~ {source_end}] "
                                            + result['explanation']['summary'])
        result['incidents'] = incident_tracker.update(result)
        if incident_tracker.persistence_error:
            state.error('incident_state_persistence')
        if journal:
            journal.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
            journal.flush()
        state.publish(result)
        if decision_log:
            decision_log.submit(result)
        LOG.info('mode=%s verdict=%s flagged=%s reason=%s', args.mode, result['status'],
                 ','.join(result['flagged']), result.get('reason', ''))

    LOG.info('mode=%s metrics=http://%s:%d/metrics baseline=%s',
             args.mode, args.bind, args.port, detector.baseline_sha256)
    try:
        if args.mode == 'replay':
            # Replay all source windows, including partial ones marked insufficient_data.
            sources = [(str(path), load_cache_snapshots(path, detector.replicas)[1]) for path in args.replay]
            while not stop.is_set():
                for source, snapshots in sources:
                    for snapshot in snapshots:
                        if stop.is_set():
                            break
                        publish(snapshot, source)
                        stop.wait(args.interval)
                if not args.loop:
                    break
            # Keep the endpoint inspectable after finite replay; readiness expires.
            while not stop.wait(1):
                pass
        else:
            aggregator = EventWindowAggregator(detector.replicas)
            diagnostics = dict(aggregator.diagnostics)
            for line in follow_file(args.events, stop):
                if stop.is_set():
                    break
                if line is None:
                    # Allow a short transport delay before closing by wall clock.
                    for snapshot in aggregator.close_until(time.time() - 2):
                        publish(snapshot, str(args.events))
                    continue
                if line == b'!source_reset':
                    state.error('source_reset', invalidate=True)
                    aggregator.invalidate('source_reset')
                    continue
                try:
                    event = json.loads(line)
                    fields = event.get('output_fields', event)
                    timestamp = int(fields['evt.rawtime']) / 1e9
                    age = time.time() - timestamp
                    if not -5 <= age <= args.stale_seconds:
                        state.error('source_timestamp', invalidate=True)
                        aggregator.invalidate('source_timestamp')
                        continue
                    for snapshot in aggregator.add(event):
                        publish(snapshot, str(args.events))
                    for key, value in aggregator.diagnostics.items():
                        if key in ('invalid_events', 'late_events', 'unknown_replica_events', 'dropped_values'):
                            for _ in range(value - diagnostics[key]):
                                state.error(key, invalidate=key != 'unknown_replica_events')
                    diagnostics = dict(aggregator.diagnostics)
                except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError, OverflowError):
                    state.error('malformed_event', invalidate=True)
                    aggregator.invalidate('malformed_event')
    except Exception:
        state.error('worker_failure', invalidate=True)
        LOG.exception('Monitor stopped')
        raise
    finally:
        server.shutdown()
        server.server_close()
        if journal:
            journal.close()
        if decision_log:
            decision_log.close()


if __name__ == '__main__':
    main()
