from prometheus_client import CollectorRegistry, generate_latest
from hycad_monitor.exporter import MonitorState, MetricsCollector
from hycad_monitor.engine import Detector, load_cache_snapshots


def test_stale_replay_disappears_instead_of_retaining_normal():
    now = [1000.0]
    detector = Detector('cache/cat_baseline.pkl')
    result = detector.evaluate(load_cache_snapshots('cache/cat_baseline.pkl')[1][0])
    state = MonitorState('test', 'replay', detector.baseline_sha256, clock=lambda: now[0])
    state.publish(result)
    registry = CollectorRegistry()
    registry.register(MetricsCollector(state))
    assert state.snapshot()['ready']
    assert 'hycad_channel_a_score{' in generate_latest(registry).decode()
    now[0] += 91
    metrics = generate_latest(registry).decode()
    assert not state.snapshot()['ready']
    assert 'hycad_channel_a_score{' not in metrics
    assert 'verdict="insufficient_data"} 1.0' in metrics


def test_historical_data_is_never_live_ready_and_errors_invalidate():
    detector = Detector('cache/cat_baseline.pkl')
    result = detector.evaluate(load_cache_snapshots('cache/cat_baseline.pkl')[1][0])
    state = MonitorState('test', 'live', detector.baseline_sha256)
    state.publish(result)
    assert not state.snapshot()['ready']
    state.mode = 'replay'
    assert state.snapshot()['ready']
    state.error('malformed_event', invalidate=True)
    assert not state.snapshot()['ready']


def test_incomplete_window_removes_scores():
    detector = Detector('cache/cat_baseline.pkl')
    snapshot = load_cache_snapshots('cache/cat_baseline.pkl')[1][0]
    state = MonitorState('test', 'replay', detector.baseline_sha256)
    state.publish(detector.evaluate(snapshot))
    snapshot['complete'] = False
    state.publish(detector.evaluate(snapshot))
    registry = CollectorRegistry()
    registry.register(MetricsCollector(state))
    assert not state.snapshot()['ready']
    assert 'hycad_channel_c_score' not in generate_latest(registry).decode()
