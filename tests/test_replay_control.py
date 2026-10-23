import contextlib
from http.client import HTTPConnection
import io
import re
import socket
import threading
import time
from types import SimpleNamespace
import urllib.parse

from prometheus_client import CollectorRegistry, generate_latest
import pytest

from hycad_monitor.exporter import MonitorState
from hycad_monitor.replay_control import (
    BoundedHTTPServer, CONTROL_PATH, CSRF_TTL, ControlConflict, CsrfTokens, GrafanaSession,
    ManualMetrics, ManualReplay, make_handler, public_origin,
)


class FakePlayback:
    def __init__(self, spec, interval):
        self.spec, self.interval = spec, interval
        self.index, self.cycle = 0, 1
        self.snapshots = [None] * 4
        self.state = MonitorState(spec.deployment, 'replay', 'a' * 64, capture=spec.id)

    def step(self):
        self.index += 1
        result = dict(status='normal', window_start=1000 + self.index * 30,
            window_end=1030 + self.index * 30, processed_at=time.time(),
            a_scores={'replica1':0.1}, b_scores={'replica1':0.1},
            c_score=0.1, work_rate=3, thresholds={'A':1, 'B':1, 'C':1},
            event_counts={'replica1':30}, accept_counts={'replica1':10}, flagged=[])
        self.state.publish(result)
        return result

    def metadata(self):
        return dict(id=self.spec.id, deployment=self.spec.deployment, title=self.spec.title,
            source_name='saved.jsonl.gz', reference_description='historical reference',
            source_sha256='a' * 64, cache_sha256='b' * 64, source_start=1000,
            source_end=1150, window_index=self.index, window_count=len(self.snapshots),
            cycle=self.cycle, interval_seconds=self.interval, loop=True)


@pytest.fixture
def controller():
    specs = [SimpleNamespace(id='capture-a', deployment='catalogue', title='Saved <A>'),
             SimpleNamespace(id='capture-b', deployment='front-end', title='Saved B')]
    value = ManualReplay(specs, factory=FakePlayback)
    yield value
    value.close()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def metrics(controller):
    registry = CollectorRegistry()
    registry.register(ManualMetrics(controller))
    return generate_latest(registry).decode()


def test_one_shared_job_pause_resume_speed_stop_and_safe_metrics(controller):
    initial = metrics(controller)
    assert 'hycad_manual_state{capture="none",deployment="none",mode="replay"} 0.0' in initial
    controller.change('start', 'capture-a', 5, 1)
    wait_for(lambda: controller.playback.index == 1)
    assert controller.snapshot()['ready'] is True
    assert controller.playback.state.snapshot()['result']['playback'] == 'manual'
    with pytest.raises(ControlConflict):
        controller.change('start', 'capture-b', 2, 2)
    with pytest.raises(ControlConflict):
        controller.change('stop', 'capture-b', 2, 2)
    controller.change('pause', 'capture-a', 5, 1)
    assert controller.snapshot()['ready'] is False
    assert 'hycad_channel_a_score{' not in metrics(controller)
    assert 'hycad_manual_state{capture="capture-a",deployment="catalogue",mode="replay"} 2.0' in metrics(controller)
    controller.change('resume', 'capture-a', 30, 1)
    wait_for(lambda: controller.playback.index == 2)
    assert controller.playback.interval == 30
    assert controller.snapshot()['metadata']['loop'] is False
    controller.change('stop', 'capture-a', 30, 1)
    wait_for(lambda: not controller.thread.is_alive())
    assert controller.snapshot()['state'] == 'stopped'
    assert controller.snapshot()['ready'] is False
    controller.change('start', 'capture-b', 2, 2)
    wait_for(lambda: controller.playback.index == 1)
    assert 'var-capture=capture-b' in controller.dashboard_url()
    assert 'var-deployment=front-end' in controller.dashboard_url()


def test_finite_replay_does_not_roll_into_another_cycle(controller):
    controller.change('start', 'capture-a', 2, 1)
    wait_for(lambda: controller.playback.index == 1)
    for expected in (2, 3, 4):
        with controller.condition:
            controller.next_due = 0
            controller.condition.notify_all()
        wait_for(lambda: controller.playback.index == expected)
    with controller.condition:
        controller.next_due = 0
        controller.condition.notify_all()
    wait_for(lambda: controller.snapshot()['state'] == 'finished')
    assert controller.playback.index == 4
    assert controller.playback.cycle == 1
    assert controller.snapshot()['ready'] is False


def test_concurrent_starts_cannot_replace_active_playback(controller):
    barrier = threading.Barrier(3)
    results = []

    def start(capture):
        barrier.wait()
        try:
            controller.change('start', capture, 30, 1)
            results.append('started')
        except ControlConflict:
            results.append('conflict')

    threads = [threading.Thread(target=start, args=(key,)) for key in controller.catalog]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
    assert sorted(results) == ['conflict', 'started']


def test_start_waits_for_old_worker_cleanup(controller):
    controller.thread = SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(ControlConflict):
        controller.change('start', 'capture-a', 5, 1)
    controller.thread = None
    assert controller.snapshot()['state'] == 'idle'


@pytest.mark.parametrize('capture,interval', [('../file.pkl',5), ('capture-a',1), ('capture-a',True), ('capture-a',0)])
def test_catalog_paths_and_unbounded_speeds_are_rejected(controller, capture, interval):
    with pytest.raises(ValueError):
        controller.change('start', capture, interval, 1)
    assert controller.snapshot()['state'] == 'idle'


def test_csrf_is_signed_user_bound_and_expires():
    clock = [1000.0]
    tokens = CsrfTokens(secret=b'x' * 32, clock=lambda: clock[0])
    token = tokens.issue(7)
    assert tokens.verify(token, 7)
    assert not tokens.verify(token, 8)
    assert not tokens.verify(token[:-1] + ('0' if token[-1] != '0' else '1'), 7)
    assert not tokens.verify('invalid', 7)
    assert not tokens.verify(token.split('.')[0] + '.한글', 7)
    clock[0] += CSRF_TTL
    assert not tokens.verify(token, 7)


def test_grafana_auth_forwards_only_the_session_cookie():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        response = io.BytesIO(b'{"id":7,"login":"viewer"}')
        response.status = 200
        return contextlib.closing(response)

    auth = GrafanaSession(opener=opener)
    assert auth('other=private; grafana_session=valid-session; bearer=do-not-forward') == 7
    request, timeout = calls[0]
    assert request.full_url == 'http://grafana:3000/api/user'
    assert request.get_header('Cookie') == 'grafana_session=valid-session'
    assert request.get_header('Authorization') is None
    assert timeout == 3
    assert auth('other=value') is None
    assert auth('grafana_session=x\r\nInjected: yes') is None


@pytest.mark.parametrize('body', [
    b'{"id":0,"login":"Anonymous"}', b'{"id":7,"login":"viewer","isAnonymous":true}',
    b'{"id":true,"login":"viewer"}', b'{"id":7}', b'[]', b'<html>Login</html>',
])
def test_grafana_anonymous_or_invalid_identity_rejected(body):
    def opener(request, timeout):
        response = io.BytesIO(body)
        response.status = 200
        return contextlib.closing(response)
    assert GrafanaSession(opener=opener)('grafana_session=test') is None


@pytest.fixture
def http_control(controller):
    registry = CollectorRegistry()
    registry.register(ManualMetrics(controller))
    handler = make_handler(controller, registry, origin='https://dashboard.example/',
                           authenticate=lambda cookie: {'valid':1, 'other':2}.get(cookie))
    server = BoundedHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method='GET', path=CONTROL_PATH, fields=None, cookie='valid', origin='https://dashboard.example', raw=None, extra_headers=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        headers = {'Cookie':cookie, 'Origin':origin}
        body = urllib.parse.urlencode(fields).encode() if fields is not None else raw
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        headers.update(extra_headers or {})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    yield request
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def form_token(http_control, cookie='valid'):
    status, headers, body = http_control(cookie=cookie)
    assert status == 200
    assert headers['Cache-Control'] == 'no-store'
    assert b'Saved &lt;A&gt;' in body
    return re.search(rb'name="csrf" value="([^"]+)"', body).group(1).decode()


def test_http_login_origin_csrf_catalog_and_post_only_controls(http_control, controller):
    assert http_control(cookie='')[0] == 401
    token = form_token(http_control)
    fields = dict(csrf=token, action='start', capture='capture-a', interval='5')
    assert http_control('GET', CONTROL_PATH + '?action=start&capture=capture-a')[0] == 200
    assert controller.snapshot()['state'] == 'idle'
    assert http_control('POST', fields=fields, cookie='')[0] == 401
    assert http_control('POST', fields=fields, origin='https://attacker.example')[0] == 403
    assert http_control('POST', fields=fields, cookie='other')[0] == 403
    assert http_control('POST', fields={**fields, 'csrf':'bad'})[0] == 403
    assert http_control('POST', fields={**fields, 'capture':'../../secret'})[0] == 400
    assert http_control('POST', fields={**fields, 'command':'rm'})[0] == 400
    status, headers, _ = http_control('POST', fields=fields)
    assert status == 303
    assert headers['Location'].startswith('/d/hycad-replay?')
    assert 'var-capture=capture-a' in headers['Location']
    assert http_control('POST', fields=fields)[0] == 409
    assert http_control('GET', CONTROL_PATH + 'status', cookie='')[0] == 401
    assert http_control('GET', '/healthz', cookie='')[0] == 200


def test_http_form_size_encoding_and_duplicate_fields_are_rejected(http_control, controller):
    token = form_token(http_control)
    encoded = urllib.parse.urlencode(dict(csrf=token, action='start', capture='capture-a', interval=5)).encode()
    assert http_control('POST', raw=encoded + b'&capture=capture-b')[0] == 400
    assert http_control('POST', raw=b'x' * 8193)[0] == 413
    assert http_control('POST', raw=encoded, extra_headers={'Content-Type':'application/json'})[0] == 415
    assert http_control('POST', raw=encoded, extra_headers={'Transfer-Encoding':'chunked'})[0] == 400
    assert controller.snapshot()['state'] == 'idle'


def test_worker_failure_invalidates_previous_result(controller):
    controller.change('start', 'capture-a', 5, 1)
    wait_for(lambda: controller.playback.index == 1)

    def failure():
        raise ValueError('private cache detail must not become an HTTP response')

    with controller.condition:
        controller.playback.step = failure
        controller.next_due = 0
        controller.condition.notify_all()
    wait_for(lambda: controller.snapshot()['state'] == 'error')
    assert controller.snapshot()['ready'] is False
    assert 'hycad_channel_a_score{' not in metrics(controller)


@pytest.mark.parametrize('url', ['https://u:p@example.com', 'https://example.com/path', 'https://example.com?x=1', 'file:///tmp/file'])
def test_public_origin_configuration_has_no_credentials_or_path(url):
    with pytest.raises(ValueError):
        public_origin(url)


def test_http_server_caps_idle_connections_and_releases_slots():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass

    server = BoundedHTTPServer(('127.0.0.1', 0), Handler, max_requests=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first = socket.create_connection(server.server_address, timeout=2)
    try:
        wait_for(lambda: server.request_slots._value == 0)
        second = socket.create_connection(server.server_address, timeout=2)
        try:
            assert b'503 Service Unavailable' in second.recv(1024)
        finally:
            second.close()
        first.close()
        wait_for(lambda: server.request_slots._value == 1)
        connection = HTTPConnection(*server.server_address, timeout=2)
        try:
            connection.request('GET', '/')
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b'ok'
        finally:
            connection.close()
    finally:
        first.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
