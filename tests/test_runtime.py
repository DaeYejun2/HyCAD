"""Exercise the HTTP-serving worker with malformed live input in isolation."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


def test_bad_timestamps_do_not_stop_live_http_worker(tmp_path):
    root = Path(__file__).resolve().parents[1]
    events = tmp_path / 'events.jsonl'
    events.write_bytes(b'')
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    with (tmp_path / 'worker.log').open('wb') as log:
        worker = subprocess.Popen([sys.executable, '-m', 'hycad_monitor', '--mode', 'live',
                                   '--baseline', str(root / 'cache/cat_baseline.pkl'),
                                   '--events', str(events), '--port', str(port)],
                                  cwd=root, stdout=log, stderr=log)
        url = f'http://127.0.0.1:{port}/status'
        try:
            deadline = time.monotonic() + 10
            while True:
                assert worker.poll() is None
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        json.load(response)
                    break
                except OSError:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            # The worker's file reader starts after its HTTP thread.
            time.sleep(0.3)
            with events.open('a') as stream:
                stream.write(json.dumps({'evt.rawtime': '9' * 401}) + '\n')
                stream.write('{"evt.rawtime": Infinity}\n')
                stream.write('not-json\n')
            while True:
                with urllib.request.urlopen(url, timeout=1) as response:
                    state = json.load(response)
                if state['errors'].get('malformed_event') == 3:
                    break
                assert worker.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert not state['ready']
            assert worker.poll() is None
        finally:
            worker.terminate()
            worker.wait(timeout=5)
