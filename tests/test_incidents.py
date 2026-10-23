from hycad_monitor.incidents import IncidentTracker

def result(start, flags=('r2',), status='partial_infection'):
    return dict(window_start=start,window_end=start+30,flagged=list(flags),status=status,
                mode='live',deployment='catalogue',processed_at=start+31,
                explanation={'summary':'部分 이상 의심 근거'},a_scores={'r2':0.31},thresholds={'a':0.14})

def test_continuous_duration_then_clear_without_counting_clear_window():
    t=IncidentTracker()
    a=t.update(result(100))[0];b=t.update(result(130))[0]
    assert a['incident_id']==b['incident_id']
    assert b['observed_seconds']==60 and b['windows']==2
    c=t.update(result(160,(), 'normal'))[0]
    assert c['state']=='지목 해제' and c['observed_seconds']==60 and c['last_seen']==160
    assert t.update(result(190,(),'normal'))==[]

def test_gap_is_not_recovery_and_new_hit_starts_new_episode():
    t=IncidentTracker();old=t.update(result(100))[0]
    closed=t.update(result(130,(),'insufficient_data'))[0]
    assert closed['state']=='관측 중단 · 지속 여부 미확인'
    new=t.update(result(160))[0]
    assert new['incident_id']!=old['incident_id'] and new['observed_seconds']==30

def test_missing_window_or_replay_time_reset_does_not_extend_duration():
    t=IncidentTracker();t.update(result(100))
    rows=t.update(result(190))
    assert len(rows)==2 and rows[0]['state'].startswith('관측 중단')
    assert rows[1]['observed_seconds']==30

def test_restart_preserves_episode_but_gap_is_unknown(tmp_path):
    path=tmp_path/'state.json'
    t=IncidentTracker(path);old=t.update(result(100))[0]
    t=IncidentTracker(path);rows=t.update(result(160,(),'insufficient_data'))
    assert rows[0]['incident_id']==old['incident_id']
    assert rows[0]['state'].startswith('관측 중단')

def test_persistence_failure_does_not_drop_decision(tmp_path,monkeypatch):
    t=IncidentTracker(tmp_path/'state.json')
    def fail(*args,**kwargs):raise OSError('full disk')
    monkeypatch.setattr('hycad_monitor.incidents.os.replace',fail)
    assert t.update(result(100))[0]['observed_seconds']==30
    assert t.persistence_error

def all_result(start):
    r=result(start,('r1','r2','r3','r4'),'all_infection')
    r.update(a_scores={r:0 for r in ('r1','r2','r3','r4')},
             b_scores={'r1':1,'r2':200,'r3':1,'r4':1},c_score=0.17,
             thresholds={'a':0.14,'b':100,'c':0.1},
             explanation={'summary':'전체 이상 의심','b_quorum':{'total':4,'hits':1,'required_hits':1}})
    return r


def test_cohort_verdict_distinguishes_actual_b_threshold_replica():
    rows=IncidentTracker().update(all_result(100))
    by_replica={r['replica']:r for r in rows}
    assert all(r['model_status_label']=='전체 이상 의심' for r in rows)
    assert all(r['a_exceeded_replicas']=='없음' and r['b_exceeded_replicas']=='r2' for r in rows)
    assert by_replica['r2']['current_replica_trigger']=='B 초과'
    assert by_replica['r1']['current_replica_trigger']=='전체 규칙에 포함 · 개별 초과 없음'
    assert by_replica['r3']['b_quorum']=='1/4 초과 · 필요 1/4'


def test_closed_episode_retains_original_trigger_not_next_normal_values(tmp_path):
    path=tmp_path/'state.json'
    t=IncidentTracker(path);rows=t.update(all_result(100))
    t=IncidentTracker(path)
    closed=t.update(result(130,(),'normal'))
    for old,new in zip(rows,closed):
        for key in ('model_status_label','a_exceeded_replicas','b_exceeded_replicas','current_replica_trigger','b_quorum','c_evidence','rule_summary'):
            assert new[key]==old[key]
        assert new['state']=='지목 해제'
