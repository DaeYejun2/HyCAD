import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from hycad_monitor.decision_log import DecisionLog, loki_payload
from hycad_monitor.exporter import MonitorState


def test_logs_keep_source_time_in_body_and_only_bounded_labels():
    result = {'processed_at': 1792669200.0, 'window_end': 1789321200,
              'explanation': {'summary': '부분 감염 판정: 프로세스 xmrig 차이'}}
    body = loki_payload(result, 'catalogue', 'replay')
    stream = body['streams'][0]
    assert stream['stream'] == {'job': 'hycad-decisions', 'deployment': 'catalogue', 'mode': 'replay', 'capture': 'legacy-replay', 'playback': 'loop'}
    assert stream['values'][0][0] == '1792669200000000000'
    assert json.loads(stream['values'][0][1]) == result


def test_loki_failure_does_not_change_detector_readiness():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(503)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = MonitorState('test', 'replay', 'sha')
    state.publish({'status': 'normal', 'window_end': 123})
    sink = DecisionLog(f'http://127.0.0.1:{server.server_port}/push', state)
    try:
        sink.submit({'processed_at': 123, 'status': 'normal'})
        sink.queue.join()
        assert state.snapshot()['ready']
        assert state.snapshot()['errors']['decision_log_delivery'] == 1
    finally:
        sink.close()
        server.shutdown()
        server.server_close()


def test_deployed_loki_limit_accepts_capture_and_playback_labels():
    from pathlib import Path
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'monitoring/loki/config.yml').read_text())
    payload = loki_payload({'processed_at': 1, 'capture': 'approved-capture', 'playback': 'manual',
                            'incidents': [{'incident_id': 'example'}]}, 'front-end', 'replay')
    for stream in payload['streams']:
        assert len(stream['stream']) <= config['limits_config']['max_label_names_per_series']
        assert set(stream['stream']) == {'job', 'deployment', 'mode', 'capture', 'playback'}
