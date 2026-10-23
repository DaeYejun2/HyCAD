#!/usr/bin/env python3
"""Prepare the approved Grafana_caps layout on an Ubuntu monitoring server.

Raw captures stay outside the public checkout and outside the worker mount.
This command re-evaluates inputs; folder names are not verified attack labels.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hycad_monitor.capture_import import import_capture, file_sha256

CAPTURES = [
    ('catalogue-normal', 'catalogue', 'normal/capture/normal_catalogue.jsonl.gz', 'catalogue · 정상 참조 캡처'),
    ('frontend-node4', 'front-end', 'normal/capture_fe_norm/normal_catalogue.jsonl.gz', 'front-end · Node 4 참조 캡처'),
    ('frontend-node10-drift', 'front-end', 'normal/capture_fe_v10/normal_catalogue.jsonl.gz', 'front-end · Node 10 변화 캡처'),
    ('catalogue-cryptomining-1of4', 'catalogue', 'cryptomining/capture_attack/attack_1of4.jsonl.gz', 'catalogue · 채굴 시나리오 1/4'),
    ('catalogue-cryptomining-2of4', 'catalogue', 'cryptomining/capture_2of4/attack_2of4.jsonl.gz', 'catalogue · 채굴 시나리오 2/4'),
    ('catalogue-cryptomining-3of4', 'catalogue', 'cryptomining/capture_3of4/attack_3of4.jsonl.gz', 'catalogue · 채굴 시나리오 3/4'),
    ('catalogue-cryptomining-4of4', 'catalogue', 'cryptomining/capture_44c/attack_4of4c.jsonl.gz', 'catalogue · 채굴 시나리오 4/4'),
] + [
    (f'catalogue-{kind}-{n}of4', 'catalogue', f'{kind}/{kind}_{n}of4/normal_catalogue.jsonl.gz', f'catalogue · {title} 시나리오 {n}/4')
    for kind, title in [('recon', '정찰'), ('revsh', '리버스 셸')] for n in range(1, 5)
]


def prepare(source, output, resume=False):
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    reports = {}
    for ident, service, relative, title in CAPTURES:
        raw = source / relative
        if not raw.is_file():
            raise ValueError(f'Missing approved capture: {relative}')
        cache = output / 'cache' / (ident + '.pkl')
        meta = cache.with_suffix('.metadata.json')
        if resume and cache.is_file() and meta.is_file():
            report = json.loads(meta.read_text())
            if report['cache_sha256'] != file_sha256(cache) or report['source_sha256'] != file_sha256(raw):
                raise ValueError(f'Existing cache provenance mismatch: {ident}')
        else:
            report = import_capture(raw, cache, [f'docker-compose_{service}_{i}' for i in range(1, 5)])
        reports[ident] = report
        print(json.dumps({'capture': ident, 'windows': report['window_count'],
                          'complete': report['complete_window_count'], 'quality': report['quality_reasons']}, ensure_ascii=False), flush=True)
    entries = []
    for ident, service, relative, title in CAPTURES:
        base = 'catalogue-normal' if service == 'catalogue' else 'frontend-node4'
        report = reports[ident]
        entries.append(dict(id=ident, deployment=service, title=title,
                            cache=f'cache/{ident}.pkl', cache_sha256=report['cache_sha256'],
                            baseline=f'cache/{base}.pkl', baseline_sha256=reports[base]['cache_sha256'],
                            source_sha256=report['source_sha256'], source_name=Path(relative).name,
                            reference_description='Historical catalogue normal reference' if service == 'catalogue' else 'Historical Node 4 normal reference; Node 10 is compared for drift'))
    catalog = {'schema_version': 1, 'captures': entries}
    target = output/'catalog.json'
    temporary = output/'.catalog.json.tmp'
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(target)
    return catalog


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, required=True)
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--resume', action='store_true')
    a=p.parse_args()
    prepare(a.source_root, a.output_root, a.resume)
