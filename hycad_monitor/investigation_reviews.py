"""Validated, bounded analyst reviews of preserved evidence, separate from verdicts.

No automatic cause classification. Reviewed text must reference an unchanged
completed manifest and hash-verified evidence. Raw captures never enter Loki.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time
import urllib.request

from .decision_context import decision_context, names

LOG = logging.getLogger('hycad.reviews')
MAX_JSON = 512 * 1024
TEXT_FIELDS = ('reviewer', 'assessment', 'attack_evidence', 'benign_evidence', 'unknowns')
CASE_ID = re.compile(r'incident-\d+-[a-f0-9]{10}\Z')
KST = timezone(timedelta(hours=9))


def bounded_json(path):
    if path.is_symlink(): raise ValueError('symbolic links are not review input')
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON: raise ValueError('JSON exceeds review size limit')
    return json.loads(raw), raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): result.update(chunk)
    return result.hexdigest()


def check_text(data):
    for field in TEXT_FIELDS:
        value = data.get(field)
        limit = 160 if field == 'reviewer' else 1200
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f'invalid {field}')
        if any(not char.isprintable() and char not in '\n\t' for char in value):
            raise ValueError(f'control character in {field}')


def check_manifest(manifest):
    if not CASE_ID.fullmatch(manifest.get('incident_id', '')):
        raise ValueError('invalid incident id')
    if manifest.get('state') not in ('complete', 'limited'):
        raise ValueError('evidence preservation has not finished')
    if not isinstance(manifest.get('sha256'), dict) or not manifest['sha256']:
        raise ValueError('no evidence hashes')
    if not manifest.get('decision', {}).get('baseline_sha256') or not manifest.get('code_commit'):
        raise ValueError('missing model provenance')
    for name, value in manifest['sha256'].items():
        if Path(name).name != name or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise ValueError('invalid evidence reference')


def create_review(manifest_path, text, reviewed_at=None):
    """Verify actual files once, then save a private review plus manifest snapshot."""
    if set(text) != set(TEXT_FIELDS): raise ValueError('unexpected or missing review text fields')
    check_text(text)
    manifest_path = Path(manifest_path)
    manifest, raw = bounded_json(manifest_path)
    check_manifest(manifest)
    if manifest_path.parent.name != manifest['incident_id']:
        raise ValueError('case directory does not match manifest')
    for name, expected in manifest['sha256'].items():
        path = manifest_path.parent / name
        if path.is_symlink() or file_digest(path) != expected:
            raise ValueError(f'evidence hash mismatch: {name}')
    reviewed_at = time.time() if reviewed_at is None else reviewed_at
    if not isinstance(reviewed_at, (int, float)) or not math.isfinite(reviewed_at) or reviewed_at < manifest['window_end']:
        raise ValueError('invalid reviewed_at')
    review = dict(schema_version=1, incident_id=manifest['incident_id'], reviewed_at=reviewed_at,
                  provenance={'manifest_sha256': digest(raw),
                              'baseline_sha256': manifest['decision']['baseline_sha256'],
                              'code_commit': manifest['code_commit'], 'evidence_sha256': manifest['sha256'],
                              'files_verified_at': reviewed_at},
                  manifest_snapshot=raw.decode('utf-8'), **text)
    root = manifest_path.parent.parent / 'reviews'
    root.mkdir(mode=0o700, exist_ok=True); root.chmod(0o700)
    output = root / (manifest['incident_id'] + '.json')
    temp = output.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(review, stream, ensure_ascii=False, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, output); output.chmod(0o600)
    return output


def validate_review(review, incident_id, manifest_raw=None, now=None):
    required = {'schema_version','incident_id','reviewed_at','provenance','manifest_snapshot', *TEXT_FIELDS}
    if not isinstance(review, dict) or set(review) != required or review['schema_version'] != 1:
        raise ValueError('unsupported review schema')
    if review['incident_id'] != incident_id: raise ValueError('review incident mismatch')
    check_text(review)
    stamp = review['reviewed_at']; now = time.time() if now is None else now
    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp > now + 5:
        raise ValueError('invalid review time')
    snapshot = review['manifest_snapshot']
    if not isinstance(snapshot, str) or len(snapshot.encode()) > MAX_JSON:
        raise ValueError('invalid manifest snapshot')
    manifest = json.loads(snapshot); check_manifest(manifest)
    provenance = review['provenance']
    if manifest['incident_id'] != incident_id or stamp < manifest['window_end']:
        raise ValueError('manifest identity or time mismatch')
    if (provenance['manifest_sha256'] != digest(snapshot.encode()) or
            provenance['baseline_sha256'] != manifest['decision']['baseline_sha256'] or
            provenance['code_commit'] != manifest['code_commit'] or
            provenance['evidence_sha256'] != manifest['sha256'] or
            provenance['files_verified_at'] != stamp):
        raise ValueError('review provenance mismatch')
    if manifest_raw is not None and digest(manifest_raw) != provenance['manifest_sha256']:
        raise ValueError('current manifest differs from reviewed manifest')
    return manifest


def review_record(manifest, review=None, error=None, archive_present=True):
    decision = manifest.get('decision', {})
    context = decision_context(decision)
    row = dict(incident_id=manifest['incident_id'], window_end_ms=manifest['window_end']*1000,
               deployment=manifest['deployment'], mode=manifest['mode'], replicas=names(manifest.get('flagged', [])),
               model_verdict=context['model_status_label'], a_exceeded=context['a_exceeded_replicas'],
               b_exceeded=context['b_exceeded_replicas'], rule_summary=context['rule_summary'],
               b_quorum=context['b_quorum'], c_evidence=context['c_evidence'],
               review_state='미분석', assessment='보존 자료의 원인 검토가 아직 완료되지 않음',
               attack_evidence='미평가 · 이상 판정 자체는 공격 증거가 아님',
               benign_evidence='미평가 · 정상 동작 또는 오탐 여부를 아직 판단하지 않음',
               unknowns='실제 원인 미확인 · 연결 목적지와 서비스 로그 검토 필요',
               reviewed_at_text='미검토', updated_at=manifest.get('completed_at',manifest.get('created_at',manifest['window_end'])),
               manifest_sha256='', baseline_sha256=decision.get('baseline_sha256',''),
               code_commit=manifest.get('code_commit',''), archive_state=manifest.get('state','미확인') if archive_present else '원본 보존 기간 종료 · 검토 기록 유지')
    if error:
        row['review_state']='검토 기록 오류 · 미분석'
        row['unknowns']='검토 파일의 형식 또는 근거 연결 검증 실패 · 원인 결론을 표시하지 않음'
    elif review:
        row.update({field:' '.join(review[field].split()) for field in TEXT_FIELDS})
        row.update(review_state='검토 완료 · 원인 미확정', reviewed_at_text=datetime.fromtimestamp(review['reviewed_at'],KST).isoformat(timespec='seconds'),
                   updated_at=review['reviewed_at'], manifest_sha256=review['provenance']['manifest_sha256'])
    return row


class ReviewPublisher:
    """Small metadata-only batches, run off the capture and detector threads."""
    def __init__(self, root, url, clock=time.time):
        self.root, self.url, self.clock = Path(root), url, clock
        self.sent = {}

    def records(self):
        paths = {p.parent.name:p for p in sorted(self.root.glob('incident-*/manifest.json'))[-2000:] if CASE_ID.fullmatch(p.parent.name)}
        reviews = {p.stem:p for p in sorted((self.root/'reviews').glob('*.json'))[-2000:] if CASE_ID.fullmatch(p.stem)}
        for ident in sorted(set(paths)|set(reviews), reverse=True):
            manifest = raw = review = None; error = False
            try:
                if ident in paths: manifest, raw = bounded_json(paths[ident])
                if ident in reviews:
                    try:
                        review, _ = bounded_json(reviews[ident])
                        snapshot = validate_review(review, ident, raw, self.clock())
                        if manifest is None: manifest = snapshot
                    except (OSError,ValueError,KeyError,TypeError,AttributeError):
                        error=True; review=None
                        LOG.warning('Rejected invalid investigation review for %s',ident)
                if manifest is not None:
                    yield review_record(manifest,review,error,ident in paths)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                LOG.warning('Could not read investigation metadata for %s',ident)

    def publish_pending(self):
        if not self.url: return 0
        sent = 0
        for row in self.records():
            # State ordering follows publication time; reviewed_at_text retains
            # the actual analysis time. Fingerprints exclude this delivery clock.
            stable = {key:value for key,value in row.items() if key != 'updated_at'}
            fingerprint = digest(json.dumps(stable, ensure_ascii=False, allow_nan=False).encode()); ident = row['incident_id']; now = self.clock()
            previous = self.sent.get(ident)
            # Refresh durable reviews in the query horizon without changing source/review times.
            if previous and previous[0] == fingerprint and now-previous[1] < 3600: continue
            row['updated_at'] = now
            encoded = json.dumps(row, ensure_ascii=False, allow_nan=False)
            data = {'streams':[{'stream':{'job':'hycad-investigations','deployment':row['deployment'],'mode':row['mode']},
                                 'values':[[str(int(now*1e9)),encoded]]}]}
            try:
                request = urllib.request.Request(self.url, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request, timeout=2) as response: response.read(1024)
                self.sent[ident] = (fingerprint,now); sent += 1
            except OSError:
                LOG.warning('Could not publish investigation reviews'); break
            if sent >= 20: break
        return sent


def main():
    parser=argparse.ArgumentParser(description='Store reviewed text linked to hash-verified preserved evidence; does not classify causes automatically.')
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--text',type=Path,required=True,help='JSON with reviewer, assessment, attack_evidence, benign_evidence, unknowns')
    args=parser.parse_args()
    text,_=bounded_json(args.text)
    print(create_review(args.manifest,text))


if __name__ == '__main__': main()
