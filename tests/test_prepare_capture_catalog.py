import gzip
import json
from pathlib import Path

from scripts import prepare_capture_catalog as prepare
from hycad_monitor.capture_replay import load_catalog, CapturePlayback
from preprocess import FIELD_MAP


def test_prepared_nested_capture_layout_loads_and_replays_with_fixed_service_cohort(tmp_path, monkeypatch):
    rows = [('catalogue-normal', 'catalogue', 'normal/capture/normal.jsonl.gz', 'catalogue baseline'),
            ('frontend-node4', 'front-end', 'normal/node4/normal.jsonl.gz', 'front-end reference'),
            ('frontend-node10-drift', 'front-end', 'normal/node10/normal.jsonl.gz', 'front-end comparison')]
    monkeypatch.setattr(prepare, 'CAPTURES', rows)
    source, output = tmp_path/'source', tmp_path/'approved'
    for ident, service, relative, title in rows:
        raw = source/relative
        raw.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(raw, 'wt') as handle:
            for start in (1, 31, 61, 91):
                for i in range(1, 5):
                    event = {**{field: None for field, _, _ in FIELD_MAP},
                             'evt.rawtime': (start+i)*1_000_000_000,
                             'evt.type': 'accept4', 'container.name': f'docker-compose_{service}_{i}'}
                    handle.write(json.dumps(event)+'\n')
    first = prepare.prepare(source, output)
    specs = load_catalog(output/'catalog.json')
    assert len(specs) == 3
    for spec in specs:
        assert spec.source_name == Path(spec.source_name).name
        playback = CapturePlayback(spec)
        assert playback.step()['status'] == 'insufficient_data'
        assert playback.step()['status'] == 'normal'
        assert all(name.startswith('docker-compose_'+spec.deployment+'_') for name in playback.detector.replicas)
    assert prepare.prepare(source, output, resume=True) == first
