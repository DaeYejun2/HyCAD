"""Authenticated, bounded control of one saved-capture replay on Ubuntu.

The administrator-owned capture catalog is the only source of input paths.
This service never executes a shell, changes live workers, or accepts passwords.
nginx publishes only /hycad/replay/; /metrics stays on the private Docker network.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import GaugeMetricFamily

from .capture_replay import CaptureMetrics, CapturePlayback, CaptureSpec, load_catalog
from .decision_log import DecisionLog

LOG = logging.getLogger("hycad.manual")
CONTROL_PATH = "/hycad/replay/"
INTERVALS = (2, 5, 10, 30)
STATES = {"idle": 0, "playing": 1, "paused": 2, "finished": 3, "stopped": 4, "error": 5}
STATE_NAMES = {"idle": "대기", "playing": "재생 중", "paused": "일시정지",
               "finished": "재생 완료", "stopped": "중단", "error": "재생 오류"}
MAX_FORM_BYTES = 8192
CSRF_TTL = 15 * 60


class ControlConflict(ValueError):
    """A stale form must not replace or control another selected capture."""


class ManualReplay:
    """One shared, finite replay. Its worker owns only its playback state."""

    def __init__(self, specs: list[CaptureSpec], *, factory=CapturePlayback,
                 clock=time.time, monotonic=time.monotonic, loki_url=None):
        self.catalog = {spec.id: spec for spec in specs}
        if not self.catalog or len(self.catalog) != len(specs):
            raise ValueError("A nonempty catalog with unique IDs is required")
        self.factory, self.clock, self.monotonic = factory, clock, monotonic
        self.loki_url = loki_url
        self.condition = threading.Condition(threading.RLock())
        self.playback = None
        self.status = "idle"
        self.started_at = 0.0
        self.owner = None
        self.thread = None
        self.decision_log = None
        self.closed = False
        self.next_due = 0.0

    def _invalidate(self, state):
        self.status = state
        if self.playback:
            self.playback.state.error("manual_replay_" + state, invalidate=True)
        self.condition.notify_all()

    def change(self, action: str, capture: str, interval: int, user_id: int):
        if action not in ("start", "pause", "resume", "stop"):
            raise ValueError("지원하지 않는 작업입니다.")
        if capture not in self.catalog or type(interval) is not int or interval not in INTERVALS:
            raise ValueError("등록된 캡처와 허용된 재생 간격을 선택해 주세요.")
        with self.condition:
            if self.closed:
                raise ControlConflict("재생 서비스가 종료 중입니다.")
            if action == "start":
                if self.status in ("playing", "paused"):
                    raise ControlConflict("공유 재생 작업이 이미 있습니다. 먼저 중단해 주세요.")
                if self.thread and self.thread.is_alive():
                    # Prevent rapid stop/start requests from retaining many old
                    # models while their bounded Loki queues are being closed.
                    raise ControlConflict("이전 재생 작업을 정리 중입니다. 잠시 후 다시 시작해 주세요.")
                playback = self.factory(self.catalog[capture], interval)
                log = DecisionLog(self.loki_url, playback.state) if self.loki_url else None
                self.playback = playback
                self.started_at, self.owner = self.clock(), user_id
                self.status = "playing"
                self.next_due = self.monotonic()
                self.decision_log = log
                self.thread = threading.Thread(target=self._run, args=(playback, log),
                                               name="hycad-manual-replay", daemon=True)
                self.thread.start()
            else:
                if not self.playback or self.playback.spec.id != capture:
                    raise ControlConflict("선택한 캡처가 현재 공유 재생 작업과 다릅니다.")
                if action == "pause":
                    if self.status != "playing":
                        raise ControlConflict("재생 중인 작업만 일시정지할 수 있습니다.")
                    self._invalidate("paused")
                elif action == "resume":
                    if self.status != "paused":
                        raise ControlConflict("일시정지한 작업만 이어서 재생할 수 있습니다.")
                    self.playback.interval = interval
                    self.status = "playing"
                    self.next_due = self.monotonic()
                    self.condition.notify_all()
                else:
                    if self.status not in ("playing", "paused"):
                        raise ControlConflict("현재 진행 중인 재생 작업이 없습니다.")
                    self._invalidate("stopped")
            return self.snapshot()

    def _run(self, playback, log):
        try:
            while True:
                with self.condition:
                    if self.closed or self.playback is not playback or self.status not in ("playing", "paused"):
                        return
                    if self.status == "paused":
                        self.condition.wait()
                        continue
                    delay = self.next_due - self.monotonic()
                    if delay > 0:
                        self.condition.wait(delay)
                        continue
                    if playback.index >= len(playback.snapshots):
                        self._invalidate("finished")
                        return
                    try:
                        result = playback.step()
                        result.update(playback="manual", manual_run_started_at=self.started_at)
                        # Keep HTTP inspection and Loki aligned with the same
                        # source window without counting a second publication.
                        with playback.state.lock:
                            playback.state.result.update(playback="manual", manual_run_started_at=self.started_at)
                        if log:
                            log.submit(result)
                        self.next_due = self.monotonic() + playback.interval
                    except Exception:
                        self._invalidate("error")
                        LOG.error("Manual replay failed for approved capture ID %s", playback.spec.id)
                        return
        finally:
            if log:
                log.close()

    def snapshot(self):
        with self.condition:
            playback = self.playback
            metadata = playback.metadata() if playback else None
            if metadata:
                metadata["loop"] = False
            return {"state": self.status, "state_code": STATES[self.status],
                    "run_started_at": self.started_at, "shared_job": True,
                    "capture": playback.spec.id if playback else None,
                    "deployment": playback.spec.deployment if playback else None,
                    "metadata": metadata,
                    "ready": playback.state.snapshot()["ready"] if playback else False}

    def dashboard_url(self):
        current = self.snapshot()
        if not current["capture"]:
            return "/d/hycad-replay"
        return "/d/hycad-replay?" + urllib.parse.urlencode({
            "var-deployment": current["deployment"], "var-capture": current["capture"],
            "from": int(current["run_started_at"] * 1000), "to": "now", "refresh": "5s"})

    def close(self):
        with self.condition:
            self.closed = True
            self._invalidate("stopped" if self.playback else "idle")
            thread = self.thread
        if thread:
            thread.join(timeout=10)


class ManualMetrics:
    def __init__(self, controller):
        self.controller = controller

    def collect(self):
        with self.controller.condition:
            playback = self.controller.playback
            if playback:
                yield from CaptureMetrics([playback]).collect()
            labels = ["deployment", "mode", "capture"]
            values = [playback.spec.deployment if playback else "none", "replay",
                      playback.spec.id if playback else "none"]
            state = GaugeMetricFamily("hycad_manual_state",
                "Shared manual replay: 0 idle, 1 playing, 2 paused, 3 finished, 4 stopped, 5 error.", labels=labels)
            state.add_metric(values, STATES[self.controller.status])
            yield state
            started = GaugeMetricFamily("hycad_manual_run_start_timestamp_seconds",
                                        "Current manual replay start wall time, zero before selection.", labels=labels)
            started.add_metric(values, self.controller.started_at)
            yield started


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound simultaneous handlers, including sessions waiting for Grafana auth."""

    request_queue_size = 32
    daemon_threads = True

    def __init__(self, *args, max_requests=32, **kwargs):
        if type(max_requests) is not int or not 1 <= max_requests <= 128:
            raise ValueError("Invalid request concurrency limit")
        self.request_slots = threading.BoundedSemaphore(max_requests)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        # This includes clients that connect without sending complete headers.
        request.settimeout(5)
        return request, address

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Retry-After: 3\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def handle_error(self, request, client_address):
        # Do not emit request data or exception tracebacks with server paths.
        LOG.warning("Replay HTTP request failed")


def public_origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("HYCAD_PUBLIC_URL must be an HTTP(S) origin without a path")
    # Browser Origin values omit a trailing slash. Require the configured casing
    # and port, so a different origin cannot silently pass validation.
    return parsed.scheme + "://" + parsed.netloc


class CsrfTokens:
    def __init__(self, *, secret=None, clock=time.time):
        self.secret = secrets.token_bytes(32) if secret is None else secret
        if len(self.secret) < 32:
            raise ValueError("CSRF signing secret must have at least 32 bytes")
        self.clock = clock

    def issue(self, user_id: int):
        payload = f"{user_id}:{int(self.clock()) + CSRF_TTL}:{secrets.token_hex(16)}".encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + "." + signature

    def verify(self, token: str, user_id: int):
        if not isinstance(token, str) or len(token) > 256:
            return False
        try:
            encoded, signature = token.split(".")
            expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return False
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            identity, expiry, nonce = payload.split(":")
            return identity == str(user_id) and self.clock() < int(expiry) <= self.clock() + CSRF_TTL + 1 and len(nonce) == 32
        except (ValueError, TypeError, UnicodeError):
            return False


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # A deployment misconfiguration must not forward a session cookie to a
        # redirected host or turn a login page into a successful identity check.
        return None


class GrafanaSession:
    """Verify the incoming session with Grafana; never trust client identity headers."""

    def __init__(self, grafana_url="http://grafana:3000", *, opener=None):
        parsed = urllib.parse.urlsplit(grafana_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Invalid internal Grafana origin")
        self.url = grafana_url.rstrip("/") + "/api/user"
        self.opener = opener or urllib.request.build_opener(_NoAuthRedirect()).open

    def __call__(self, cookie_header):
        if not cookie_header or len(cookie_header) > MAX_FORM_BYTES or "\r" in cookie_header or "\n" in cookie_header:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            session = cookies.get("grafana_session")
            if not session or not session.value or len(session.value) > 4096:
                return None
            request = urllib.request.Request(self.url, headers={
                "Cookie": session.OutputString(), "Accept": "application/json"})
            with self.opener(request, timeout=3) as response:
                if response.status != 200:
                    return None
                body = response.read(16385)
                if len(body) > 16384:
                    return None
                user = json.loads(body)
            identifier = user.get("id")
            if (type(identifier) is not int or identifier <= 0 or user.get("isAnonymous")
                    or not isinstance(user.get("login"), str) or not user["login"]
                    or user["login"].lower() == "anonymous"):
                return None
            return identifier
        except (OSError, ValueError, TypeError, AttributeError, CookieError, urllib.error.URLError):
            return None


def render_form(controller, csrf, selected=None, message=""):
    state = controller.snapshot()
    selected = selected if selected in controller.catalog else state["capture"]
    options = "".join(
        '<option value="' + html.escape(spec.id, quote=True) + '"' + (' selected' if spec.id == selected else '') + '>'
        + html.escape(spec.deployment + " · " + spec.title) + '</option>'
        for spec in controller.catalog.values())
    interval = state["metadata"]["interval_seconds"] if state["metadata"] else 5
    speeds = "".join(f'<option value="{seconds}"' + (' selected' if interval == seconds else '')
                      + f'>{seconds}초 / 원본 30초 구간 ({30 / seconds:g}배속)</option>' for seconds in INTERVALS)
    summary = ""
    if state["metadata"]:
        row = state["metadata"]
        summary = f'<p>{html.escape(row["title"])} · {row["window_index"]}/{row["window_count"]} 구간</p>'
    return ('''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HyCAD 캡처 수동 재생</title><style>
body{font:16px system-ui,sans-serif;background:#111217;color:#e4e7ed;margin:0;padding:32px;line-height:1.6}
main{max-width:760px;margin:auto}h1{font-size:26px}a{color:#8ab8ff}label{display:block;margin-top:20px}
select,button{font:inherit;padding:10px;border:1px solid #687080;border-radius:5px;background:#20242e;color:inherit}
select{width:100%}button{margin:20px 8px 0 0;cursor:pointer}button[value=start]{background:#2454a4}
.notice{padding:12px;border-left:3px solid #8ab8ff;background:#1b202b}.status{font-size:20px}
</style><main><h1>캡처 수동 재생</h1><p>저장된 Sysdig 캡처를 HyCAD로 다시 판정합니다. LIVE 수집과 자동 순환 화면은 계속 실행됩니다.</p>'''
        + '<p class="status">공유 작업 상태: <strong>' + STATE_NAMES[state["state"]] + '</strong></p>'
        + summary + ('<p class="notice">' + html.escape(message) + '</p>' if message else '')
        + '<form method="post" action="' + CONTROL_PATH + '"><input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True) + '">'
        + '<label for="capture">캡처</label><select id="capture" name="capture">' + options + '</select>'
        + '<label for="interval">재생 속도</label><select id="interval" name="interval">' + speeds + '</select>'
        + '<button name="action" value="start">처음부터 시작</button><button name="action" value="pause">일시정지</button>'
        + '<button name="action" value="resume">선택한 속도로 계속</button><button name="action" value="stop">중단</button></form>'
        + '<p><a href="' + html.escape(controller.dashboard_url(), quote=True) + '">수동 재생 대시보드 열기</a> · <a href="/d/hycad-overview">LIVE / 자동 순환 대시보드</a></p>'
        + '<p>동시에 수동 작업 하나만 실행합니다. 다른 조회 사용자와 작업을 공유하며, 재생 완료·일시정지·중단 상태는 현재 정상 판정으로 표시하지 않습니다. 속도 변경은 일시정지 후 계속을 누르세요.</p>'
        + '<p>캡처의 이상 판정은 당시 관측 자료의 분석이며 현재 서비스에서 공격이 실행 중이라는 뜻이 아닙니다. 재학습은 수행하지 않습니다.</p></main></html>').encode()


def make_handler(controller, registry, *, origin, authenticate=None, csrf=None):
    origin = public_origin(origin)
    authenticate = authenticate or GrafanaSession()
    csrf = csrf or CsrfTokens()

    class Handler(BaseHTTPRequestHandler):
        server_version = "HyCADReplay"

        def _send(self, code, body=b"", content_type="text/plain; charset=utf-8", location=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(body)

        def _user(self):
            identity = authenticate(self.headers.get("Cookie", ""))
            if type(identity) is not int or identity <= 0:
                self._send(401, '<!doctype html><meta charset="utf-8"><p>Grafana 로그인이 필요합니다. <a href="/login">로그인</a> 후 이 페이지를 다시 열어 주세요.</p>'.encode(), "text/html; charset=utf-8")
                return None
            return identity

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/metrics" and not parsed.query:
                self._send(200, generate_latest(registry), CONTENT_TYPE_LATEST)
            elif parsed.path == "/healthz" and not parsed.query:
                self._send(200, b'{"alive":true,"mode":"replay","playback":"manual"}', "application/json")
            elif parsed.path in (CONTROL_PATH, CONTROL_PATH + "status"):
                user = self._user()
                if user is None:
                    return
                if parsed.path.endswith("/status"):
                    self._send(200, json.dumps(controller.snapshot(), ensure_ascii=False, allow_nan=False).encode(), "application/json")
                    return
                try:
                    params = urllib.parse.parse_qs(parsed.query, max_num_fields=8)
                    selected = params.get("capture", [None])[0]
                except ValueError:
                    self._send(400, b"Invalid query")
                    return
                self._send(200, render_form(controller, csrf.issue(user), selected), "text/html; charset=utf-8")
            else:
                self._send(404, b"Not found")

        def do_POST(self):
            if self.path != CONTROL_PATH:
                self._send(404, b"Not found")
                return
            if self.headers.get("Origin") != origin:
                self._send(403, b"Origin rejected")
                return
            user = self._user()
            if user is None:
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Transfer-Encoding is not supported")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_FORM_BYTES:
                    self._send(413, b"Form size limit")
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
                    self._send(415, b"Expected form encoding")
                    return
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete form")
                fields = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True,
                                              strict_parsing=True, max_num_fields=4)
                if set(fields) != {"action", "capture", "interval", "csrf"} or any(len(v) != 1 for v in fields.values()):
                    raise ValueError("Unexpected or repeated form field")
                if not csrf.verify(fields["csrf"][0], user):
                    self._send(403, b"CSRF rejected; refresh the control page")
                    return
                interval = int(fields["interval"][0])
                controller.change(fields["action"][0], fields["capture"][0], interval, user)
            except ControlConflict as exc:
                self._send(409, render_form(controller, csrf.issue(user), message=str(exc)), "text/html; charset=utf-8")
                return
            except (ValueError, UnicodeError):
                self._send(400, b"Invalid form or unapproved capture")
                return
            except OSError:
                self._send(503, b"Capture unavailable; administrator review required")
                return
            self._send(303, location=controller.dashboard_url())

        def log_message(self, *args):
            # Request paths, cookies and CSRF values are deliberately not logged.
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9111)
    parser.add_argument("--public-url", default=os.environ.get("HYCAD_PUBLIC_URL"))
    parser.add_argument("--grafana-url", default=os.environ.get("HYCAD_GRAFANA_URL", "http://grafana:3000"))
    parser.add_argument("--loki-url", default=os.environ.get("HYCAD_LOKI_URL"))
    args = parser.parse_args()
    if not args.public_url:
        parser.error("--public-url or HYCAD_PUBLIC_URL is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    controller = ManualReplay(load_catalog(args.manifest), loki_url=args.loki_url)
    registry = CollectorRegistry()
    registry.register(ManualMetrics(controller))
    handler = make_handler(controller, registry, origin=args.public_url, authenticate=GrafanaSession(args.grafana_url))
    server = BoundedHTTPServer((args.bind, args.port), handler)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("Manual replay control ready for %d approved captures", len(controller.catalog))
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


if __name__ == "__main__":
    main()
