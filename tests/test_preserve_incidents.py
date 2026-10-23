import gzip,json,os,sys,time
from pathlib import Path
import pytest
from scripts.preserve_incidents import snapshot_events, Preserver, atomic_json
from deployment.capture.serve_context import validate_request, bounded, NAMES

def event(t,n=1):
    return (json.dumps({'evt.rawtime':int(t*1e9),'evt.num':n,'evt.type':'connect',
                        'container.name':'docker-compose_catalogue_2','fd.rip':'10.0.0.2',
                        'fd.rport':3306,'proc.pid':123,'evt.dir':'<','evt.rawres':0})+'\n').encode()

def test_snapshot_interval_rotation_and_destinations(tmp_path):
    src=tmp_path/'events.jsonl'
    src.with_name(src.name+'.1').write_bytes(event(99)+event(100)+event(110))
    src.write_bytes(event(120)+event(129.5)+event(130))
    out=tmp_path/'out.gz'
    r=snapshot_events(src,out,100,130)
    rows=[json.loads(x) for x in gzip.open(out,'rb')]
    assert len(rows)==4 and r['boundary_coverage'] and not r['truncated']
    assert r['connections'][0]['fd.rip']=='10.0.0.2'
    assert r['connections'][0]['fd.rport']==3306
    assert os.stat(out).st_mode & 0o777==0o600

def test_snapshot_cap_and_missing_history_are_explicit(tmp_path):
    src=tmp_path/'events.jsonl';src.write_bytes(event(110)+event(120))
    r=snapshot_events(src,tmp_path/'out.gz',100,130,max_bytes=len(event(110)))
    assert r['truncated'] and not r['boundary_coverage'] and r['events']==1

def test_context_rejects_shell_and_wide_or_old_requests():
    assert validate_request({'start':950,'end':990},now=1000)==(950,990)
    for q in ({'start':950,'end':990,'container':'x; touch /tmp/x'},
              {'start':'950; echo x','end':990},{'start':0,'end':990},
              {'start':950,'end':1010},{'start':float('nan'),'end':990}):
        with pytest.raises(ValueError):validate_request(q,now=1000)
    assert all(n.startswith(('docker-compose_catalogue', 'docker-compose_front-end_')) for n in NAMES)
    assert len(NAMES) == 9

def test_context_bounds_output_and_timeout():
    a=bounded([sys.executable,'-c','print("x"*100000)'],limit=100)
    assert a['truncated'] and len(a['text'])==100
    b=bounded([sys.executable,'-c','import time;time.sleep(10)'],timeout=0.05)
    assert b['timed_out']

def test_preserver_ignores_replay_deduplicates_and_reports_restart(tmp_path):
    src=tmp_path/'events.jsonl';src.write_bytes(event(900)+event(999))
    root=tmp_path/'evidence'
    p=Preserver(src,root,[],clock=lambda:1000)
    r=dict(mode='replay',status='partial_infection',window_start=970,window_end=1000,
           deployment='catalogue',flagged=['r2'],incidents=[{'incident_id':'episode','state':'관측 중'}])
    p.consider(r);assert not p.jobs
    r['mode']='live';p.consider(r);p.consider(r);assert len(p.jobs)==1
    p.close()
    restarted=Preserver(src,root,[],clock=lambda:1001)
    assert not restarted.jobs
    m=json.loads(next(root.glob('incident-*/manifest.json')).read_text())
    assert m['state']=='interrupted'
    restarted.consider(r);assert not restarted.jobs
    restarted.close()

def test_retention_keeps_active_and_bounds_closed(tmp_path):
    p=Preserver(tmp_path/'events',tmp_path/'evidence',[],max_total=10,clock=lambda:1000)
    for n in ['incident-old','incident-active']:
        d=p.root/n;d.mkdir();(d/'file').write_bytes(b'x'*20)
    p.jobs['incident-active']={}
    p.prune()
    assert not (p.root/'incident-old').exists() and (p.root/'incident-active').exists()
    p.close()

def test_end_to_end_archive_writes_pre_post_hashes_and_context(tmp_path,monkeypatch):
    src=tmp_path/'events.jsonl'
    src.write_bytes(b''.join(event(t) for t in (910,940,970,999,1000,1030,1059)))
    now=[1000]
    def fake_context(command,start,end,out):
        atomic_json(out,{'start':start,'end':end,'containers':[]})
        return {'ok':True,'reason':'captured','containers':5}
    monkeypatch.setattr('scripts.preserve_incidents.remote_context',fake_context)
    p=Preserver(src,tmp_path/'evidence',[],clock=lambda:now[0])
    r=dict(mode='live',status='partial_infection',window_start=970,window_end=1000,
           deployment='catalogue',flagged=['r2'],incidents=[{'incident_id':'e','state':'관측 중'}])
    p.consider(r);p.tick()
    p.future.result(timeout=5);p.tick()
    assert next(iter(p.jobs.values()))['state']=='waiting_for_post'
    now[0]=1063;p.tick();p.future.result(timeout=5);p.tick()
    assert not p.jobs
    m=json.loads(next(p.root.glob('incident-*/manifest.json')).read_text())
    assert m['state']=='complete' and m['pre']['events']==4 and m['post']['events']==3
    assert 'service-context.json' in m['sha256']
    p.close()
