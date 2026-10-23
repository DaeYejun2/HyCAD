"""Track observed per-replica episodes without treating gaps as recovery."""
import copy
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

from .decision_context import decision_context


class IncidentTracker:
    def __init__(self, state_path=None):
        self.active = {}
        self.previous_end = None
        self.state_path = Path(state_path) if state_path else None
        self.persistence_error = False
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                self.active, self.previous_end = saved['active'], saved['previous_end']
            except (OSError, ValueError, KeyError, TypeError):
                self.persistence_error = True
                logging.getLogger('hycad').exception('Could not restore episode state')

    def update(self, result):
        start, end = result['window_start'], result['window_end']
        # A replay file boundary or a missing source window is not recovery.
        contiguous = self.previous_end is None or start == self.previous_end
        valid = result['status'] != 'insufficient_data'
        flagged = set(result['flagged']) if valid else set()
        updates = []
        for replica, old in list(self.active.items()):
            if not contiguous or not valid:
                old.update(state='관측 중단 · 지속 여부 미확인', closed_at=end)
            elif replica not in flagged:
                old.update(state='지목 해제', closed_at=end)
            else:
                continue
            updates.append(copy.deepcopy(old))
            del self.active[replica]
        for replica in sorted(flagged):
            if replica not in self.active:
                self.active[replica] = dict(
                    incident_id=f"{result['mode']}-{int(start)}-{replica}",
                    replica=replica, first_seen=start, windows=0, observed_seconds=0,
                    cause='미확인 · 모델 판정 근거', state='관측 중')
            item = self.active[replica]
            ev = result['explanation'].get('replica_evidence', {}).get(replica, {})
            differences = []
            for feature in ev.get('top_features', []):
                f = ev['features'][feature]
                examples = ', '.join(x['value'] for x in f.get('examples_absent_from_some_peers', []))
                differences.append(f["label"] + (': ' + examples if examples else ' 차이'))
            reason = f"A={result.get('a_scores', {}).get(replica, 0):.4g} / 기준 {result['thresholds']['a']:.4g}; " + '; '.join(differences)
            if result['status'] == 'all_infection':
                reason = '전체 이상 규칙: ' + result['explanation']['summary']
            item.update(decision_context(result, replica))
            item.update(last_seen=end, windows=item['windows'] + 1,
                        observed_seconds=item['observed_seconds'] + end - start,
                        state='관측 중', evidence=reason,
                        a_score=result.get('a_scores', {}).get(replica),
                        a_threshold=result['thresholds']['a'])
            updates.append(copy.deepcopy(item))
        self.previous_end = end
        for item in updates:
            item.update(deployment=result['deployment'], mode=result['mode'],
                        processed_at=result['processed_at'],
                        first_seen_ms=item['first_seen']*1000, last_seen_ms=item['last_seen']*1000,
                        first_seen_text=datetime.fromtimestamp(item['first_seen'], timezone.utc).isoformat(),
                        last_seen_text=datetime.fromtimestamp(item['last_seen'], timezone.utc).isoformat())
        if self.state_path:
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.state_path.with_suffix('.tmp')
                with temp.open('w') as stream:
                    json.dump({'active':self.active,'previous_end':self.previous_end}, stream, ensure_ascii=False)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, self.state_path)
                self.persistence_error = False
            except OSError:
                self.persistence_error = True
                logging.getLogger('hycad').exception('Could not persist episode state')
        return updates
