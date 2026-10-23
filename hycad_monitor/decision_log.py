"""Optional bounded asynchronous Loki delivery of decision evidence."""
import json
import queue
import threading
import urllib.error
import urllib.request


def loki_payload(result, deployment, mode):
    stream_labels = {'deployment': deployment, 'mode': mode,
                     'capture': result.get('capture', 'live' if mode == 'live' else 'legacy-replay'),
                     'playback': result.get('playback', 'live' if mode == 'live' else 'loop')}
    payload = {'streams': [{'stream': {'job': 'hycad-decisions', **stream_labels},
                         'values': [[str(int(result['processed_at'] * 1_000_000_000)),
                                     json.dumps(result, ensure_ascii=False, allow_nan=False)]]}]}
    for item in result.get('incidents', []):
        payload['streams'].append({'stream': {'job': 'hycad-incidents', **stream_labels},
                                   'values': [[str(int(result['processed_at'] * 1_000_000_000)),
                                               json.dumps(item, ensure_ascii=False, allow_nan=False)]]})
    return payload


class DecisionLog:
    """A failed log backend cannot stop or change the anomaly verdict."""
    def __init__(self, url, state):
        if not url.startswith(('http://', 'https://')):
            raise ValueError('Loki URL must use http or https')
        self.url, self.state = url, state
        self.queue = queue.Queue(maxsize=128)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, result):
        data = json.dumps(loki_payload(result, self.state.deployment, self.state.mode),
                          ensure_ascii=False, allow_nan=False).encode()
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            self.state.error('decision_log_queue_full')

    def _run(self):
        while not self.stop.is_set():
            try:
                data = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                request = urllib.request.Request(self.url, data=data, method='POST',
                                                 headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=2) as response:
                    response.read(1024)
            except (OSError, urllib.error.URLError):
                self.state.error('decision_log_delivery')
            finally:
                self.queue.task_done()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
