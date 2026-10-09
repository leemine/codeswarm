"""Real signed tokens and original FD/chunk reads, with live owner withdrawal."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask
from jiuwenswarm.agents.harness.common.tools.web_file_download import WebFileDownloadManager
from jiuwenswarm.governance.application_boundary import admit_application_request
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.rsi_download import issue_experiment_download
from jiuwenswarm.governance.session_boundary import delivery_scope, set_delivery_permit
from jiuwenswarm.governance.workspace_download import capture_workspace_request

OWNER = TrustedIdentity('owner', 'owner', 'test:instance')
OTHER = TrustedIdentity('other', 'other', 'test:instance')

@pytest.fixture
def setup(tmp_path, monkeypatch):
    ctx = build_rsi_service_context(tmp_path / 'tasks')
    config = {'instance_owner': asdict(OWNER)}
    auth = SimpleNamespace(known_actor=lambda i: i in (OWNER, OTHER), _config=lambda: config)
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: auth)
    monkeypatch.setattr('jiuwenswarm.governance.rsi_download.experiment_store', lambda: ctx.store)
    monkeypatch.setattr('jiuwenswarm.governance.rsi_boundary.experiment_store', lambda: ctx.store)
    monkeypatch.setattr(WebFileDownloadManager, '_instance', WebFileDownloadManager(secret='test-key'*8))
    ctx.store.create(RsiTask(task_id='rsi-own', name='own', scenario='HARNESS',
                           status='COMPLETED', created_at='now', owner_identity=asdict(OWNER)))
    path = tmp_path / 'tasks' / 'rsi-own' / 'artifact.txt'
    path.write_text('owner private artifact')
    with delivery_scope():
        permit = admit_application_request('rsi.artifact.download', {'task_id': 'rsi-own'},
                                          identity_resolver=lambda: OWNER, rsi_store=ctx.store)
        set_delivery_permit(permit)
        result = issue_experiment_download(str(path), 'rsi-own')
    params = {'token': result['download_token'], 'offset': 0, 'limit': 65536}
    return ctx, config, path, result, params


def test_original_chunk_transport_reads_owned_rsi_without_chat_session(setup):
    _, _, _, result, params = setup
    assert parse_qs(urlsplit(result['download_url']).query)['session_id'] == ['rsi-own']
    permit = capture_workspace_request(None, lambda: OWNER, 'rsi-own', params)
    assert permit.channel_id == 'web'
    assert permit.read(0, 65536) == b'owner private artifact'
    assert permit.name == 'artifact.txt'
    with pytest.raises(PermissionError):
        capture_workspace_request(None, lambda: OTHER, 'rsi-own', params)
    with pytest.raises(PermissionError):
        capture_workspace_request(None, lambda: OWNER, 'different-id', params)


@pytest.mark.parametrize('change', ['credential', 'owner_role', 'ownership', 'replace_file', 'symlink', 'expired'])
def test_buffered_download_revocation_and_file_change_denied(setup, change, monkeypatch):
    ctx, config, path, _, params = setup
    identity = [OWNER]
    permit = capture_workspace_request(None, lambda: identity[0], 'rsi-own', params)
    assert permit.read(0, 3) == b'own'
    if change == 'credential': identity[0] = None
    elif change == 'owner_role': config.clear()
    elif change == 'ownership':
        task = ctx.store.get('rsi-own'); task.owner_identity = asdict(OTHER)
        ctx.store.delete('rsi-own'); ctx.store.create(task)
    elif change == 'replace_file':
        path.unlink(); path.write_text('other private artifact')
    elif change == 'symlink':
        other = path.parent / 'other'; other.write_text('private')
        path.unlink(); path.symlink_to(other)
    elif change == 'expired': monkeypatch.setattr('time.time', lambda: permit.payload['exp'] + 1)
    with pytest.raises((PermissionError, OSError)):
        permit.read(3, 5)
    with pytest.raises((PermissionError, OSError)):
        permit.check()


def test_experiment_push_recipient_and_final_guard(setup):
    from jiuwenswarm.governance.rsi_boundary import experiment_event_delivery
    _, _, _, _, _ = setup
    identity = [OWNER]
    frame = {'type': 'event', 'event': 'rsi.training.progress',
             'payload': {'task_id': 'rsi-own', 'session_id': 'browser-route', 'iteration': 1}}
    actual, guard = experiment_event_delivery(frame, lambda: identity[0])
    assert actual == frame and guard()
    with pytest.raises(PermissionError): experiment_event_delivery(frame, lambda: OTHER)
    identity[0] = None
    assert not guard()
