"""Actual publication commit and its sidecar history are the same disk version."""
import copy
import json

import pytest

from jiuwenswarm.server.runtime.session.sharing_audit import SharingAuditContext, SharingAuditError
from tests.unit_tests.server import test_continuation_publication as existing

setup = existing.setup


@pytest.mark.asyncio
async def test_committed_event_is_atomic_private_and_idempotent(setup, monkeypatch):
    original = setup.access._save
    commits = []
    results = []
    with existing.make_scope(setup) as scope:
        existing.register(setup, scope)
        scope.write_seed()

        def save(data):
            record = data['session_sharing']['owners']['target-session']['continuation']
            event = data['sharing_audit']['events'][-1]
            assert record['state'] == 'committed'
            assert event['facts']['publication_id'] == scope.publication_id
            commits.append(copy.deepcopy(data))
            original(data)

        monkeypatch.setattr(setup.access, '_save', save)
        context = SharingAuditContext(existing.BOB, 'original-request', 'session.share.continue')
        existing.publication.commit(scope, audit_context=context, audit_result=results.append)
        scope.commit()
    assert len(commits) == len(results) == 1
    events = setup.access._load()['sharing_audit']['events']
    assert [e['facts']['action'] for e in events] == ['create', 'continue']
    event = events[-1]
    assert event['context']['request_id'] == 'original-request'
    assert event['context']['actor']['subject_id'] == existing.BOB.subject_id
    facts = event['facts']
    assert facts['source_session_id'] == 'source-session' and facts['target_session_id'] == 'target-session'
    assert facts['source_project_id'] == setup.source.project_id
    assert facts['target_project_id'] == setup.target.project_id
    assert facts['seed_digest'] == scope.seed.digest
    text = json.dumps(event)
    for excluded in ('text-0', 'child-secret', 'never-copy', 'create-1', '/synthetic/workspace'):
        assert excluded not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['audit', 'save'])
async def test_publication_failure_stays_pending_with_no_new_event(setup, monkeypatch, failure):
    with existing.make_scope(setup) as scope:
        existing.register(setup, scope)
        scope.write_seed()
        if failure == 'audit':
            data = setup.access._load()
            data['sharing_audit']['schema_version'] = 2
            setup.access._save(data)
        else:
            from jiuwenswarm.server.runtime.session import project_access
            def failed(*args):
                raise OSError('synthetic disk failure')
            monkeypatch.setattr(project_access.os, 'replace', failed)
        before = setup.access.path.read_bytes()
        results = []
        with pytest.raises(SharingAuditError if failure == 'audit' else OSError):
            existing.publication.commit(scope, audit_result=results.append)
        assert setup.access.path.read_bytes() == before and results == []
        assert setup.access._load()['session_sharing']['owners']['target-session']['continuation']['state'] == 'pending'
