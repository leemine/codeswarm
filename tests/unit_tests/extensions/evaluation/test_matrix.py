"""Frozen matrix, legacy migration, actual model identity and bounded submissions."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from jiuwenswarm.extensions.evaluation.backend.models import ExperimentDraft, decode, digest
from jiuwenswarm.extensions.evaluation.backend.adapters.store import EvaluationStore, CatalogError
from jiuwenswarm.extensions.evaluation.backend.adapters.runtime_execution import RuntimeExecution
from jiuwenswarm.extensions.evaluation.backend.results import statistics
from jiuwenswarm.extensions.evaluation.backend.trials import Trials
from jiuwenswarm.governance.contracts import TrustedIdentity

ACTOR = TrustedIdentity('local', 'local', 'local-single-user-installation')
PLANS = [dict(model='m#0', execution_profile_id='native', provider_id='native'),
         dict(model='m#0', execution_profile_id='oc', provider_id='opencode')]


def populate(store, **changes):
    store.save_draft(ACTOR, dict(task_id='sum', name='Sum', instruction='Add'))
    store.publish_task(ACTOR, 'sum', 1)
    definition = dict(name='Matrix', tasks=[dict(task_id='sum', revision=1)],
                      plans=PLANS, repeats=2, concurrency=2,
                      shared_environment_acknowledged=True, **changes)
    return store.create_experiment(ACTOR, definition, 'key', versions={})


def test_matrix_counts_retries_and_reopen(tmp_path):
    path = tmp_path / 'store.db'
    store = EvaluationStore(path)
    experiment = populate(store)
    assert [(t['plan_index'], t['repeat_index']) for t in experiment['trials']] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    first = experiment['trials'][0]
    store.update_attempt(ACTOR, experiment['id'], first['attempts'][0]['id'], revision=0,
                         phase='settled', body={'outcome': 'test_failed'})
    retried = store.retry_trial(ACTOR, experiment['id'], first['id'])
    retry = retried['trials'][0]['attempts'][1]
    store.update_attempt(ACTOR, experiment['id'], retry['id'], revision=0,
                         phase='settled', body={'outcome': 'passed'})
    store.close()
    store = EvaluationStore(path)
    result = statistics(store.experiment(ACTOR, experiment['id']))
    assert result['denominator'] == 4 and result['passed'] == 0
    assert [p['denominator'] for p in result['plans']] == [2, 2]
    assert result['plans'][0]['first_attempt_outcomes'] == {'test_failed': 1, 'pending': 1}
    store.close()


def test_v1_migration_preserves_ids_fingerprints_and_attempts(tmp_path):
    path = tmp_path / 'store.db'
    store = EvaluationStore(path)
    old = populate(store)
    # Build the previous five-column table and a genuine single-plan v1 body.
    definition = dict(name='Legacy', tasks=[dict(task_id='sum', revision=1)],
                      model='m#0', execution_profile_id='native', shared_environment_acknowledged=True)
    value = decode(ExperimentDraft, definition).model_dump(mode='json'); value.pop('plans')
    body = {k:v for k,v in old.items() if k not in {'id', 'created', 'trials'}}
    body['definition'] = value
    store.db.execute('DELETE FROM attempts WHERE trial_id IN (SELECT id FROM trials WHERE plan_index=1)')
    store.db.execute('DELETE FROM trials WHERE plan_index=1')
    store.db.execute('UPDATE experiments SET fingerprint=?,body=?', (digest(value),json.dumps(body)))
    before = store.experiment(ACTOR,old['id'])
    store.db.execute('PRAGMA foreign_keys=OFF')
    store.db.executescript('''CREATE TABLE old_trials (
      id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(id),
      task_id TEXT NOT NULL, task_revision INTEGER NOT NULL, repeat_index INTEGER NOT NULL,
      UNIQUE(experiment_id,task_id,task_revision,repeat_index));
      INSERT INTO old_trials SELECT id,experiment_id,task_id,task_revision,repeat_index FROM trials;
      DROP TABLE trials; ALTER TABLE old_trials RENAME TO trials; PRAGMA user_version=1;''')
    store.close()
    migrated = EvaluationStore(path)
    after = migrated.create_experiment(ACTOR,definition,'key',versions={})
    assert after == before
    assert migrated.db.execute('PRAGMA foreign_key_check').fetchall() == []
    migrated.close()


@pytest.mark.parametrize('change', [dict(plans=PLANS * 2), dict(model='ambiguous'),
                                   dict(concurrency=5), dict(plans=[{**PLANS[0], 'provider_id':'codex'}])])
def test_invalid_matrix_rejected(change):
    value=dict(name='M',tasks=[dict(task_id='t',revision=1)], plans=PLANS,
               shared_environment_acknowledged=True)
    with pytest.raises(ValidationError):
        decode(ExperimentDraft, {**value, **change})


@pytest.mark.asyncio
async def test_model_identity_and_provider_checked_before_preparation(monkeypatch):
    runtime=NS(start=AsyncMock(), prepare_session_create=AsyncMock())
    port=RuntimeExecution(runtime)
    port.options=AsyncMock(return_value=dict(models=[{'selection_key':'m#0'}], profiles=[dict(
        id='oc', revision='v1', provider_id='opencode', available=True, model_selection_keys=['other#1'])]))
    definition=decode(ExperimentDraft,dict(name='M',tasks=[dict(task_id='t',revision=1)],
       model='m#0',execution_profile_id='oc',provider_id='opencode',shared_environment_acknowledged=True))
    with pytest.raises(CatalogError,match='CONFIGURATION_UNAVAILABLE'):
        await port.prepare(definition,title='T',request_id='r')
    runtime.prepare_session_create.assert_not_called()
    port.options.return_value['profiles'][0]['model_selection_keys']=['m#0']
    with pytest.raises(CatalogError,match='CONFIGURATION_UNAVAILABLE'):
        await port.configuration(definition.model_copy(update={'provider_id':'native'}))


@pytest.mark.asyncio
async def test_parallel_claims_bounded_cancel_pending_without_submission(tmp_path, monkeypatch):
    store=EvaluationStore(tmp_path/'store.db');exp=populate(store)
    trials=Trials(store,tmp_path,None)
    entered=[];active=0;peak=0;release=asyncio.Event();ready=asyncio.Event()
    async def run(identity,experiment_id,definition,task,attempt_id,*,plan_index):
        nonlocal active,peak
        entered.append((attempt_id,definition.provider_id,plan_index))
        active+=1;peak=max(peak,active)
        if active==2: ready.set()
        await release.wait()
        active-=1
        trials._patch(identity,experiment_id,attempt_id,'settled',outcome='cancelled',exit_confirmed=True)
    monkeypatch.setattr(trials,'_run_attempt',run)
    worker=asyncio.create_task(trials._run(ACTOR,exp['id']))
    await asyncio.wait_for(ready.wait(),2)
    await trials.cancel(ACTOR,exp['id'])
    release.set();await asyncio.wait_for(worker,2)
    assert peak==2 and len(entered)==2
    result=store.experiment(ACTOR,exp['id'])
    assert all(t['attempts'][0]['body']['outcome']=='cancelled' for t in result['trials'])
    assert all(t['attempts'][0]['body'].get('submitted') is False for t in result['trials'][2:])
    await trials.close();store.close()
