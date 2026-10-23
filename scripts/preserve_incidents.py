#!/usr/bin/env python3
"""Bounded forensic snapshots of existing capture files; no attack execution.

Runs on the monitor host as hycad. Original capture is read-only. Slow archive
and remote-log work is isolated from detection. The separate SSH identity can
only request a bounded read-only context snapshot from the target.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

if __package__:
    from .collect_service import build_service_command, CollectorLock
else:
    from collect_service import build_service_command, CollectorLock

# The standalone systemd entry point also loads the lightweight review adapter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hycad_monitor.investigation_reviews import ReviewPublisher

LOG = logging.getLogger('hycad.evidence')
MIB = 1024 * 1024


def atomic_json(path, data):
    temp=path.with_suffix(path.suffix+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(data,f,ensure_ascii=False,allow_nan=False); f.flush(); os.fsync(f.fileno())
    os.replace(temp,path)


def read_json(url):
    with urllib.request.urlopen(url,timeout=3) as r: return json.load(r)


def snapshot_events(source, output, start, end, max_bytes=128*MIB):
    """Pin both file descriptors before scanning, never chase a growing writer.

    Coverage describes observed boundaries and markers, not proof of zero kernel
    drops. Time interval is half-open; pre/post files cannot duplicate events.
    """
    handles=[]; identities=set(); gaps=[]; malformed=0
    count=0; written=0; first=None; last=None; cap=False; peers={}; missing_fields=0; omitted=0
    for path in (source.with_name(source.name+'.1'),source):
        try:
            f=path.open('rb'); st=os.fstat(f.fileno()); key=(st.st_dev,st.st_ino)
            if key in identities: f.close(); continue
            identities.add(key); handles.append((f,st.st_size))
        except FileNotFoundError: pass
    try:
        with gzip.open(output,'wb',compresslevel=1) as dest:
            for f,size in handles:
                while f.tell()<size:
                    line=f.readline(min(MIB+1,size-f.tell()))
                    if not line: break
                    if not line.endswith(b'\n') or len(line)>MIB:
                        malformed+=1; continue
                    if line.startswith(b'!'):
                        gaps.append(line[:100].decode(errors='replace').strip()); continue
                    try:
                        event=json.loads(line); fields=event.get('output_fields',event)
                        ts=int(fields['evt.rawtime'])/1e9
                    except (ValueError,TypeError,KeyError,AttributeError):
                        malformed+=1; continue
                    if not start <= ts < end: continue
                    if written+len(line)>max_bytes:
                        cap=True; break
                    dest.write(line); written+=len(line); count+=1
                    if 'fd.rip' not in fields or 'proc.pid' not in fields: missing_fields+=1
                    first=ts if first is None else min(first,ts); last=ts if last is None else max(last,ts)
                    if fields.get('evt.type') in ('connect','accept','accept4','socket','getpeername','getsockopt'):
                        selected={k:fields.get(k) for k in ('container.name','container.id','proc.pid','proc.ppid','proc.name','evt.type','evt.dir','evt.rawres','fd.rip','fd.rport','fd.lip','fd.lport','fd.sip','fd.sport','fd.cip','fd.cport','fd.l4proto')}
                        key=json.dumps(selected,sort_keys=True)
                        if key in peers:
                            peers[key]['count']+=1; peers[key]['last_event']=ts
                        elif len(peers)<1000:
                            peers[key]=dict(selected,count=1,first_event=ts,last_event=ts)
                        else: omitted+=1
                if cap: break
    finally:
        for f,_ in handles: f.close()
        output.chmod(0o600)
    return {'requested_start':start,'requested_end':end,'first_event':first,'last_event':last,
            'events':count,'uncompressed_bytes':written,'byte_limit':max_bytes,'truncated':cap,
            'boundary_coverage':first is not None and first <= start+2 and last >= end-2,
            'markers_seen_in_source_files':sorted(set(gaps))[:20], 'malformed_lines_in_source_files':malformed,
            'connections':list(peers.values()),'connection_summary_limit':1000,'connection_summary_omitted_events':omitted,
            'events_missing_investigation_fields':missing_fields,
            'coverage_note':'Observed timestamp coverage only; source-file markers may be outside requested interval. Does not establish zero dropped events.'}


def remote_context(command, start, end, output):
    # stdin is bounded JSON, never SSH_ORIGINAL_COMMAND or shell interpolation.
    with output.open('wb') as f:
        output.chmod(0o600)
        p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=f,stderr=subprocess.PIPE)
        try:
            _,err=p.communicate(json.dumps({'start':start,'end':end}).encode()+b'\n',timeout=100)
        except subprocess.TimeoutExpired:
            p.kill(); _,err=p.communicate(); return {'ok':False,'reason':'ssh_timeout'}
    if p.returncode: return {'ok':False,'reason':'ssh_failed','exit_code':p.returncode}
    try:
        data=json.loads(output.read_text())
        records=data['containers']
        complete=all(r['logs']['exit_code']==0 and not r['logs']['truncated'] and not r['logs']['timed_out'] and not r['log_line_limit_may_apply'] and r['metadata']['exit_code']==0 for r in records)
        return {'ok':complete,'containers':len(records),'reason':'captured' if complete else 'partial_or_limited'}
    except (ValueError,KeyError,TypeError): return {'ok':False,'reason':'invalid_response'}


class Preserver:
    def __init__(self, source, root, context_command, loki_url=None, retention_days=7, max_total=5*1024*MIB, part_limit=128*MIB, clock=time.time):
        self.source,self.root=Path(source),Path(root)
        self.command,self.loki_url=context_command,loki_url
        self.retention_days,self.max_total,self.part_limit=retention_days,max_total,part_limit
        self.clock=clock; self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.root.chmod(0o700)
        self.jobs={}; self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future=None; self.work=None; self.last_processed=None; self.last_error=''; self.last_prune=0
        self.reviewer=ReviewPublisher(self.root,self.loki_url,self.clock)
        self.review_pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.review_future=None;self.last_review_poll=float('-inf')
        self.checkpoints={}
        for d in sorted(self.root.glob('incident-*')):
            try:
                m=json.loads((d/'manifest.json').read_text())
                for key in m.get('episode_keys',[]): self.checkpoints[key]=max(self.checkpoints.get(key,0),m['window_end'])
                if m['state'] not in ('complete','limited','interrupted'):
                    m.update(state='interrupted',limitation='preservation process restarted before completion')
                    atomic_json(d/'manifest.json',m)
            except (OSError,ValueError,KeyError): continue

    def notify(self,m):
        if not self.loki_url: return
        row={k:m[k] for k in ('incident_id','deployment','mode','state','window_start','window_end')}
        row.update(updated_at=self.clock(),window_end_ms=m['window_end']*1000,replicas=', '.join(m['flagged']),cause='미확인',
                   summary=f"{m['incident_id']} | {', '.join(m['flagged'])} | 보존 {m['state']} | 원인 미확인",
                   location=m['incident_id'],limitation=m.get('limitation',''),
                   pre_events=m.get('pre',{}).get('events',0),post_events=m.get('post',{}).get('events',0),
                   service_logs=m.get('service_context',{}).get('reason','pending'))
        data={'streams':[{'stream':{'job':'hycad-evidence','deployment':m['deployment'],'mode':m['mode']},'values':[[str(int(self.clock()*1e9)),json.dumps(row,ensure_ascii=False)]]}]}
        try:
            req=urllib.request.Request(self.loki_url,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=2) as r:r.read(1024)
        except OSError: LOG.warning('Could not publish preservation status')

    def consider(self,result):
        if result.get('mode')!='live' or result['status'] not in ('partial_infection','all_infection'): return
        # Same episode gets a new bounded checkpoint at most every two minutes.
        keys=[i['incident_id'] for i in result.get('incidents',[]) if i['state']=='관측 중']
        if not keys: keys=[f"window-{result['window_end']}"]
        if all(result['window_end']-self.checkpoints.get(k,0)<120 for k in keys):return
        if len(self.jobs)>=4:
            self.last_error='queue_full';return
        if shutil.disk_usage(self.root).free < 1024*MIB:
            self.last_error='low_disk';return
        ident='incident-'+str(int(result['window_end']))+'-'+hashlib.sha256('|'.join(keys).encode()).hexdigest()[:10]
        d=self.root/ident
        if d.exists():return
        d.mkdir(mode=0o700)
        m=dict(incident_id=ident,episode_keys=keys,deployment=result['deployment'],mode='live',state='queued',
               window_start=result['window_start'],window_end=result['window_end'],
               requested_start=result['window_start']-60,requested_end=result['window_end']+60,
               flagged=result['flagged'],created_at=self.clock(),cause='unconfirmed',
               limits={'pre_seconds':60,'post_seconds':60,'raw_bytes_per_part':self.part_limit,'retention_days':self.retention_days},
               decision=result)
        commit=Path(__file__).resolve().parents[1]/'DEPLOYED_COMMIT'
        m['code_commit']=commit.read_text().strip() if commit.exists() else 'local-development'
        atomic_json(d/'manifest.json',m);self.jobs[ident]=m
        for key in keys:self.checkpoints[key]=result['window_end']
        self.last_error=''
        self.notify(m)

    def _pre(self,m):
        d=self.root/m['incident_id']
        info=snapshot_events(self.source,d/'events-before-and-trigger.jsonl.gz',m['requested_start'],m['window_end'],self.part_limit)
        atomic_json(d/'connections-before.json',info.pop('connections'))
        return info

    def _post(self,m):
        d=self.root/m['incident_id']
        info=snapshot_events(self.source,d/'events-after.jsonl.gz',m['window_end'],m['requested_end'],self.part_limit)
        atomic_json(d/'connections-after.json',info.pop('connections'))
        context=remote_context(self.command,m['requested_start'],m['requested_end'],d/'service-context.json')
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir() if p.is_file() and p.name!='manifest.json'}
        return info,context,hashes

    def tick(self):
        now=self.clock()
        if self.future and self.future.done():
            ident,stage=self.work;m=self.jobs[ident]
            try:
                value=self.future.result()
                if stage=='pre':m.update(pre=value,state='waiting_for_post')
                else:
                    post,context,hashes=value
                    complete=context['ok'] and all(x['boundary_coverage'] and not x['truncated'] and not x['markers_seen_in_source_files'] and not x['malformed_lines_in_source_files'] and not x['events_missing_investigation_fields'] for x in (m['pre'],post))
                    m.update(post=post,service_context=context,sha256=hashes,state='complete' if complete else 'limited',completed_at=now)
                    if not complete:m['limitation']='Inspect coverage, limits, source markers and service_context; missing evidence is not a normal verdict.'
            except Exception as exc:
                LOG.exception('Preservation failed');m.update(state='interrupted',limitation=type(exc).__name__)
            atomic_json(self.root/ident/'manifest.json',m);self.notify(m)
            if m['state'] in ('complete','limited','interrupted'): del self.jobs[ident]
            self.future=None;self.work=None
        if self.future is None:
            # Capture prehistory first before it rotates away.
            for m in self.jobs.values():
                if m['state']=='queued':
                    self.work=(m['incident_id'],'pre');self.future=self.pool.submit(self._pre,m);break
            if self.future is None:
                for m in self.jobs.values():
                    if m['state']=='waiting_for_post' and now >= m['requested_end']+3:
                        self.work=(m['incident_id'],'post');self.future=self.pool.submit(self._post,m);break
        if now-self.last_prune>=60:self.prune();self.last_prune=now
        if self.review_future and self.review_future.done():
            try:self.review_future.result()
            except Exception:LOG.exception('Investigation review publishing failed')
            self.review_future=None
        if self.review_future is None and now-self.last_review_poll>=30:
            self.review_future=self.review_pool.submit(self.reviewer.publish_pending)
            self.last_review_poll=now

    def prune(self):
        closed=[];total=0
        for d in self.root.glob('incident-*'):
            if not d.is_dir() or d.is_symlink():continue
            size=sum(p.stat().st_size for p in d.iterdir() if p.is_file())
            total+=size
            if d.name not in self.jobs:closed.append((d.stat().st_mtime,d,size))
        reserve=len(self.jobs)*(2*self.part_limit+12*MIB)
        target=max(0,self.max_total-reserve)
        for stamp,d,size in sorted(closed):
            if stamp<self.clock()-self.retention_days*86400 or total>target:
                try:
                    m=json.loads((d/'manifest.json').read_text());m.update(state='expired',limitation='retention or storage limit');self.notify(m)
                except (OSError, ValueError, KeyError):pass
                shutil.rmtree(d);total-=size
        # Bound long-running in-memory episode history too.
        self.checkpoints={k:v for k,v in self.checkpoints.items() if v>self.clock()-86400}

    def close(self):
        self.pool.shutdown(wait=True)
        self.review_pool.shutdown(wait=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--events',type=Path,required=True);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--host',required=True);p.add_argument('--identity',type=Path,required=True);p.add_argument('--known-hosts',type=Path,required=True)
    p.add_argument('--port',type=int,default=22);p.add_argument('--status-url',default='http://127.0.0.1:9108/status')
    p.add_argument('--health-path',type=Path,help='Independent health metadata path for this deployment.')
    p.add_argument('--loki-url',default='http://127.0.0.1:3100/loki/api/v1/push')
    args=p.parse_args();os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    command=build_service_command(args.host,args.identity,args.known_hosts,args.port)
    command.remove('-n');command[-1]='hycad-context'
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with CollectorLock(args.root/'.lock',0o600):
        worker=Preserver(args.events,args.root,command,args.loki_url)
        health={'updated_at':0,'last_poll_ok':0,'pending':0,'error':''}
        try:
            while not stop.is_set():
                try:
                    status=read_json(args.status_url);health['last_poll_ok']=time.time()
                    result=status.get('result')
                    if result and status['ready'] and result['processed_at']!=worker.last_processed:
                        worker.consider(result);worker.last_processed=result['processed_at']
                    health['error']=worker.last_error
                except (OSError,ValueError,KeyError):health['error']='detector_unavailable'
                worker.tick()
                health.update(updated_at=time.time(),pending=len(worker.jobs))
                atomic_json(args.root/'health.json',health)
                health_path=args.health_path or args.events.parent/'evidence-health.json'
                atomic_json(health_path,health)
                health_path.chmod(0o640)
                stop.wait(1)
        finally:worker.close()

if __name__=='__main__': main()
