"""Owner RSI files consume configured credentials, never Native placeholders."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi.model_resolver import RsiModelConfigResolver
from jiuwenswarm.governance.application_boundary import admit_application_request
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_boundary import delivery_scope, set_delivery_permit


@pytest.fixture
def snapshot(monkeypatch):
    owner = TrustedIdentity('owner', 'owner', 'test:instance')
    policy = {'instance_owner': asdict(owner)}
    auth = SimpleNamespace(known_actor=lambda identity: identity == owner, _config=lambda: policy)
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: auth)
    entry = {'model_client_config': {
        'model_name': 'synthetic', 'client_provider': 'OpenAI',
        'api_base': 'https://example.test/v1', 'credential_encoding': 'plain',
        'api_key': 'synthetic-private-key'}, 'model_config_obj': {'temperature': 0.2}}
    config = {'models': {'defaults': [entry]}}
    monkeypatch.setattr('jiuwenswarm.common.config.get_config_raw', lambda: config)
    # Use the actual catalog masking and production Model builder.
    resolver = RsiModelConfigResolver(config_loader=lambda: config, zen_loader=lambda: [])
    with delivery_scope():
        permit = admit_application_request('rsi.task.create', {}, identity_resolver=lambda: owner)
        set_delivery_permit(permit)
        yield resolver, policy, config


def test_actual_guarded_builder_materializes_private_owner_credential(snapshot, tmp_path):
    resolver, _, _ = snapshot
    assert resolver.resolve('synthetic')[0]['model_client_config']['api_key'] == 'MODEL_REQUEST_AUTHORITY'
    manifest = resolver.resolve_to_file('synthetic', 'evaluation', tmp_path)
    target = tmp_path / 'evaluation.yaml'
    data = yaml.safe_load(target.read_text())
    assert data['model_client_config']['api_key'] == 'synthetic-private-key'
    assert target.stat().st_mode & 0o777 == 0o600
    assert 'synthetic-private-key' not in str(manifest)


@pytest.mark.parametrize('change', ['revoked', 'wrong_method', 'duplicate', 'changed_entry'])
def test_model_snapshot_rejects_invalid_authority_or_catalog(snapshot, tmp_path, change):
    resolver, policy, config = snapshot
    if change == 'revoked':
        policy.clear()
    elif change == 'wrong_method':
        from jiuwenswarm.governance.session_boundary import current_application_permit
        owner = current_application_permit('rsi.task.create').identity
        set_delivery_permit(admit_application_request('rsi.task.list', {}, identity_resolver=lambda: owner))
    elif change == 'duplicate':
        config['models']['defaults'].append(deepcopy(config['models']['defaults'][0]))
    else:
        entry = deepcopy(resolver.resolve('synthetic')[0])
        entry['model_config_obj']['temperature'] = 0.9
        resolver._defaults_loader = lambda _: [entry]
    with pytest.raises(PermissionError):
        resolver.resolve_to_file('synthetic', 'evaluation', tmp_path)
    assert not list(tmp_path.glob('*.yaml'))


def test_revocation_during_builder_does_not_publish_key(snapshot, tmp_path):
    resolver, policy, _ = snapshot
    def revoke(client, request):
        policy.clear()
        return SimpleNamespace(model_client_config=client, model_config=request)
    resolver._model_builder = revoke
    with pytest.raises(Exception, match='授权已失效'):
        resolver.resolve_to_file('synthetic', 'evaluation', tmp_path)
    assert not list(tmp_path.iterdir())
