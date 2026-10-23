import hashlib
import json
from pathlib import Path

import pytest

from hycad_monitor.investigation_reviews import create_review, validate_review, ReviewPublisher

IDENT='incident-1000-78363c0e27'
TEXT=dict(reviewer='검토자',assessment='원인 미확정',attack_evidence='보존 범위에서 공격 증거 미발견',
          benign_evidence='정상 DB 연결 증가 가능성',unknowns='서비스 내부 실행 경로 미확인')


def case(tmp_path):
    root=tmp_path/'evidence';d=root/IDENT;d.mkdir(parents=True)
    evidence=d/'events.jsonl.gz';evidence.write_bytes(b'PRIVATE_RAW_EVENT')
    manifest=dict(incident_id=IDENT,state='complete',deployment='catalogue',mode='live',window_end=1000,
                  flagged=['docker-compose_catalogue_1'],created_at=1001,completed_at=1065,
                  code_commit='abcd',sha256={evidence.name:hashlib.sha256(evidence.read_bytes()).hexdigest()},
                  decision=dict(status='partial_infection',flagged=['docker-compose_catalogue_1'],
                                a_scores={'docker-compose_catalogue_1':0.3},b_scores={'docker-compose_catalogue_1':0},
                                thresholds={'a':0.14,'b':1,'c':0.1},c_score=0,
                                baseline_sha256='a'*64))
    p=d/'manifest.json';p.write_text(json.dumps(manifest))
    return root,p,evidence


def test_future_unreviewed_case_does_not_infer_benign_or_attack(tmp_path):
    root,manifest,evidence=case(tmp_path)
    row=list(ReviewPublisher(root,None,clock=lambda:2000).records())[0]
    assert row['review_state']=='미분석'
    assert row['attack_evidence'].startswith('미평가')
    assert row['benign_evidence'].startswith('미평가')
    assert row['model_verdict']=='부분 이상 의심' and row['a_exceeded']=='catalogue 1'
    assert 'PRIVATE_RAW_EVENT' not in json.dumps(row)


def test_review_validates_hashes_and_persists_after_raw_expiry(tmp_path):
    root,manifest,evidence=case(tmp_path)
    path=create_review(manifest,TEXT,reviewed_at=1100)
    review=json.loads(path.read_text())
    validate_review(review,IDENT,manifest.read_bytes(),now=2000)
    assert path.stat().st_mode & 0o777==0o600
    row=list(ReviewPublisher(root,None,clock=lambda:2000).records())[0]
    assert row['review_state']=='검토 완료 · 원인 미확정'
    assert row['assessment']==TEXT['assessment']
    evidence.unlink();manifest.unlink();manifest.parent.rmdir()
    persisted=list(ReviewPublisher(root,None,clock=lambda:2000).records())[0]
    assert persisted['assessment']==TEXT['assessment']
    assert persisted['archive_state']=='원본 보존 기간 종료 · 검토 기록 유지'


def test_hash_mismatch_prevents_creating_review(tmp_path):
    root,manifest,evidence=case(tmp_path)
    evidence.write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash mismatch'):create_review(manifest,TEXT,1100)
    assert not (root/'reviews').exists()


@pytest.mark.parametrize('change', ['manifest','schema','provenance','future','extra-field'])
def test_invalid_review_cannot_claim_reviewed_or_publish_its_conclusion(tmp_path,change):
    root,manifest,evidence=case(tmp_path)
    path=create_review(manifest,TEXT,1100)
    r=json.loads(path.read_text());r['assessment']='UNVERIFIED_CONCLUSION'
    if change=='manifest':manifest.write_text(manifest.read_text()+' ')
    elif change=='schema':r['schema_version']=999
    elif change=='provenance':r['provenance']['baseline_sha256']='b'*64
    elif change=='future':r['reviewed_at']=3000
    else:r['attack_confirmed']=True
    path.write_text(json.dumps(r))
    row=list(ReviewPublisher(root,None,clock=lambda:2000).records())[0]
    assert row['review_state']=='검토 기록 오류 · 미분석'
    assert 'UNVERIFIED_CONCLUSION' not in json.dumps(row)


def test_restart_republishes_review_and_separates_loki_source_time(tmp_path,monkeypatch):
    root,manifest,evidence=case(tmp_path);create_review(manifest,TEXT,1100)
    sent=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return b''
    def urlopen(request,timeout):sent.append(json.loads(request.data));return Response()
    monkeypatch.setattr('hycad_monitor.investigation_reviews.urllib.request.urlopen',urlopen)
    publisher=ReviewPublisher(root,'http://localhost/loki',clock=lambda:2000)
    assert publisher.publish_pending()==1
    assert publisher.publish_pending()==0
    assert ReviewPublisher(root,'http://localhost/loki',clock=lambda:2100).publish_pending()==1
    stream=sent[-1]['streams'][0]
    assert stream['stream']=={'job':'hycad-investigations','deployment':'catalogue','mode':'live'}
    assert stream['values'][0][0]==str(2100*10**9)
    body=json.loads(stream['values'][0][1])
    assert body['updated_at']==2100 and body['window_end_ms']==1000000
    assert body['reviewed_at_text'].startswith('1970-01-01T09:18:20')
    assert 'PRIVATE_RAW_EVENT' not in stream['values'][0][1]
    assert 'manifest_snapshot' not in body


def test_invalidated_or_removed_review_supersedes_old_valid_conclusion(tmp_path,monkeypatch):
    root,manifest,evidence=case(tmp_path)
    review=create_review(manifest,TEXT,1100)
    original=review.read_bytes();now=[2000];sent=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return b''
    def urlopen(request,timeout):
        data=json.loads(request.data)
        sent.append(json.loads(data['streams'][0]['values'][0][1]));return Response()
    monkeypatch.setattr('hycad_monitor.investigation_reviews.urllib.request.urlopen',urlopen)
    publisher=ReviewPublisher(root,'http://localhost/loki',clock=lambda:now[0])
    assert publisher.publish_pending()==1
    review.write_text('{}');now[0]=2010
    assert publisher.publish_pending()==1
    assert sent[-1]['review_state']=='검토 기록 오류 · 미분석'
    assert sent[-1]['updated_at']>sent[-2]['updated_at']
    assert sent[-1]['assessment']!=TEXT['assessment']
    review.write_bytes(original);now[0]=2020
    assert publisher.publish_pending()==1
    assert sent[-1]['reviewed_at_text']==sent[0]['reviewed_at_text']
    review.unlink();now[0]=2030
    assert publisher.publish_pending()==1
    assert sent[-1]['review_state']=='미분석'
    assert sent[-1]['updated_at']>sent[-2]['updated_at']
    assert sent[-1]['assessment']!=TEXT['assessment']
    assert publisher.publish_pending()==0
