"""Thread-safe current-window exposition; expired results never look healthy."""
import copy
import json
from pathlib import Path
import math
import threading
import time
from collections import Counter
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

VERDICTS = ('normal', 'partial_infection', 'all_infection', 'drift', 'insufficient_data')


class MonitorState:
    def __init__(self, deployment, mode, baseline_sha256, stale_seconds=90, clock=time.time, evidence_health=None, capture=None, reference_description=""):
        self.deployment, self.mode = deployment, mode
        self.capture = capture or ("live" if mode == "live" else "legacy-replay")
        if (not isinstance(self.capture, str) or len(self.capture) > 96
                or not self.capture or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in self.capture)):
            raise ValueError("capture must be a bounded identifier")
        self.reference_description = reference_description[:200]
        self.baseline_sha256 = baseline_sha256
        self.stale_seconds, self.clock = stale_seconds, clock
        self.lock = threading.Lock()
        self.result = None
        self.updated = 0
        self.errors, self.processed = Counter(), Counter()
        self.evidence_health = evidence_health
        self.input_fault = None

    def publish(self, result):
        if result['status'] not in VERDICTS:
            raise ValueError('Unknown verdict')
        with self.lock:
            self.result = copy.deepcopy(result)
            self.updated = self.clock()
            self.processed[result['status']] += 1
            self.input_fault = None

    def error(self, reason, invalidate=False):
        with self.lock:
            self.errors[reason] += 1
            if invalidate:
                self.input_fault = reason

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.result)
            now = self.clock()
            recent = result is not None and now - self.updated <= self.stale_seconds
            source_recent = (self.mode == 'replay' or result is not None and
                             -5 <= now - result['window_end'] <= self.stale_seconds)
            ready = bool(recent and source_recent and not self.input_fault and
                         result['status'] != 'insufficient_data')
            return dict(deployment=self.deployment, mode=self.mode, capture=self.capture, ready=ready,
                        result=result, updated=self.updated,
                        reason=self.input_fault or (result.get('reason') if recent and source_recent else 'waiting_or_stale'),
                        errors=dict(self.errors), processed=dict(self.processed))


class MetricsCollector:
    def __init__(self, state):
        self.state = state

    def collect(self):
        s = self.state.snapshot()
        labels = ['deployment', 'mode', 'capture']
        values = [s['deployment'], s['mode'], s['capture']]

        def gauge(name, help_text, rows, extra=()):
            metric = GaugeMetricFamily(name, help_text, labels=labels + list(extra))
            for suffix, value in rows:
                if value is not None and math.isfinite(value):
                    metric.add_metric(values + list(suffix), value)
            return metric

        if self.state.evidence_health:
            try:
                h = json.loads(Path(self.state.evidence_health).read_text())
                ok = (0 <= self.state.clock() - h['last_poll_ok'] < 10
                      and 0 <= self.state.clock() - h['updated_at'] < 10 and not h['error'])
                pending = h['pending']
            except (OSError, ValueError, KeyError, TypeError):
                ok, pending = False, 0
            yield gauge('hycad_evidence_ready', 'Evidence worker is polling and reporting fresh health.', [((), int(ok))])
            yield gauge('hycad_evidence_pending', 'Pending evidence preservation tasks.', [((), pending)])
        yield gauge('hycad_ready', 'Fresh complete usable window available (1=yes).', [((), int(s['ready']))])
        yield gauge('hycad_last_result_timestamp_seconds', 'Wall time when a window was processed.',
                    [((), s['updated'])] if s['updated'] else [])
        yield gauge('hycad_window_seconds', 'Aggregation duration in seconds.', [((), 30)])
        yield gauge('hycad_baseline_info', 'Baseline digest, B implementation and reference description.', [((self.state.baseline_sha256, 'z', self.state.reference_description), 1)], ['sha256', 'b_kind', 'reference'])
        active = s['result']['status'] if s['ready'] else 'insufficient_data'
        yield gauge('hycad_verdict', 'Current verdict; stale data becomes insufficient_data.',
                    [((v,), int(v == active)) for v in VERDICTS], ['verdict'])
        for name, help_text, counts, extra in (
            ('hycad_processed_windows', 'Processed windows by verdict.', s['processed'], 'verdict'),
            ('hycad_input_errors', 'Input failures by bounded reason.', s['errors'], 'reason')):
            metric = CounterMetricFamily(name, help_text, labels=labels + [extra])
            for key, value in counts.items():
                metric.add_metric(values + [key], value)
            yield metric
        result = s['result']
        if result is None:
            return
        yield gauge('hycad_window_end_timestamp_seconds', 'Original source window end (historical during replay).', [((), result['window_end'])])
        # No old scores remain exposed while input is incomplete or stale.
        if not s['ready']:
            return
        for name, key in [('hycad_channel_a_score', 'a_scores'), ('hycad_channel_b_score', 'b_scores'),
                          ('hycad_window_events', 'event_counts'), ('hycad_window_accepts', 'accept_counts')]:
            yield gauge(name, 'Latest complete window value per replica.', [((k,), v) for k, v in result[key].items()], ['replica'])
        yield gauge('hycad_replica_anomaly', 'Replica flagged by the HyCAD decision.',
                    [((r,), int(r in result['flagged'])) for r in result['event_counts']], ['replica'])
        yield gauge('hycad_channel_c_score', 'Absolute log2 events-per-accept ratio change.', [((), result['c_score'])])
        yield gauge('hycad_work_rate', 'Kernel events per accept-family event, not HTTP requests.', [((), result['work_rate'])])
        yield gauge('hycad_threshold', 'Baseline calibrated A/B and fixed C thresholds.',
                    [((k,), v) for k, v in result['thresholds'].items()], ['channel'])
