"""Host catalog selection; no live model network or ambient credentials."""
from copy import deepcopy

import pytest

from jiuwenswarm.agents.harness.team import config_loader as loader
from jiuwenswarm.governance.resources import ResourceAccessDenied


def entry(ref='credential:first'):
    return {'model_client_config': {
        'model_name': 'same-model', 'client_provider': 'OpenAI',
        'api_base': 'https://models.example/v1', 'api_key': 'synthetic-secret',
        'credential_reference': ref, 'credential_encoding': 'plain',
    }, 'model_config_obj': {'temperature': 0.2}}


@pytest.fixture
def catalog(monkeypatch):
    from jiuwenswarm.common import config
    from jiuwenswarm.governance import organization_auth
    raw = {'models': {'defaults': [entry()]}}
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(config, 'get_config_raw', lambda: deepcopy(raw))
    monkeypatch.setattr(loader, 'get_default_models', lambda *a: pytest.fail('legacy model resolution'))
    monkeypatch.setattr(loader, 'get_zen_free_model_entries', lambda: pytest.fail('ambient Zen resolution'))
    for name in ('API_KEY', 'MODEL_NAME', 'API_BASE', 'MODEL_PROVIDER'):
        monkeypatch.setenv(name, 'ambient-value')
    return raw


def team_config(member=None):
    return {'modes': {'team': {'demo': {'agents': {'leader': {}, 'teammate': member or {}}}}}}


@pytest.mark.parametrize('requested', ['login-only', 'zen-free', 'unknown'])
def test_governed_selected_model_never_falls_back(catalog, requested):
    login = entry()
    login['model_client_config']['model_name'] = requested
    with pytest.raises(ResourceAccessDenied):
        loader.get_effective_team_model_entries({}, requested_model_name=requested, login_model_entry=login)


def test_empty_catalog_does_not_consume_environment(catalog):
    catalog['models']['defaults'] = []
    with pytest.raises(ResourceAccessDenied):
        loader.get_effective_team_model_entries({})


def test_two_references_survive_real_pool_and_typed_config(catalog, monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager
    from openjiuwen.agent_teams import TeamAgentSpec
    catalog['models']['defaults'].append(entry('credential:second'))
    monkeypatch.setattr(team_manager, 'get_config', team_config)
    spec = team_manager.TeamManager._load_team_spec('synthetic-session')
    # Exercise the actual serializer + core materializer, not a constructor spy.
    restored = TeamAgentSpec.model_validate_json(spec.model_dump_json())
    typed = [item.to_team_model_config() for item in restored.model_pool]
    assert [m.model_client_config.credential_reference for m in typed] == [
        'credential:first', 'credential:second']
    assert all(m.model_client_config.credential_encoding == 'plain' for m in typed)
    assert all(m.model_client_config.api_key == 'MODEL_REQUEST_AUTHORITY' for m in typed)
    assert all(m.model_request_config.model_name == 'same-model' for m in typed)
    assert 'synthetic-secret' not in spec.model_dump_json()
    assert len({item.model_id for item in spec.model_pool}) == 2


def test_explicit_member_uses_its_own_catalog_reference(catalog):
    catalog['models']['defaults'].append(entry('credential:second'))
    model = entry('credential:second')
    model['model_client_config']['api_key'] = 'inline-secret-is-not-authority'
    spec = loader.load_team_spec_dict(team_config({'model': model}))
    assert spec['agents']['leader']['model']['model_client_config']['credential_reference'] == 'credential:first'
    member = spec['agents']['teammate']['model']
    assert member['model_client_config']['credential_reference'] == 'credential:second'
    assert member['model_client_config']['api_key'] == 'MODEL_REQUEST_AUTHORITY'
    assert member['model_request_config']['temperature'] == 0.2
    assert catalog['models']['defaults'][1]['model_client_config']['api_key'] == 'synthetic-secret'


@pytest.mark.parametrize('change', ['reference', 'encoding', 'request_model', 'duplicate'])
def test_invalid_member_binding_rejected_before_materialization(catalog, change):
    model = entry()
    if change == 'reference':
        model['model_client_config']['credential_reference'] = 'credential:unknown'
    elif change == 'encoding':
        del model['model_client_config']['credential_encoding']
    elif change == 'request_model':
        model['model_request_config'] = {'model': 'another-model'}
    else:
        catalog['models']['defaults'].append(entry())
    with pytest.raises(ResourceAccessDenied, match='catalog binding unavailable'):
        loader.load_team_spec_dict(team_config({'model': model}))
