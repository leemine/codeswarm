"""Read-only UI candidates and pre-allocation configuration drift checks."""
import json

import pytest

from jiuwenswarm.runtime.harness.execution_options import execution_display, execution_options, parse_execution_options
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput, SessionProvisionError
from tests.unit_tests.runtime.test_session_create_provisioner import _State, _install_product_hooks, _input, _provisioner


def configuration():
    return {'permissions': {'enabled': True}, 'execution': {'default_profile_id': 'native', 'profiles': {
        'native': {'provider_id': 'native', 'config_revision': 'r1'},
        'opencode': {'provider_id': 'opencode', 'config_revision': 'r1',
                     'provider_config': {'model': {'model': 'example', 'api_base': 'http://127.0.0.1:9999/v1'}}},
        'codex': {'provider_id': 'codex', 'config_revision': 'r1'},
    }}}


def entries():
    return [{'model_client_config': {'model_name': 'example', 'api_base': 'http://127.0.0.1:9999/v1',
        'client_provider': 'OpenAI', 'credential_reference': 'model-account:test', 'credential_encoding': 'plain',
        'api_key': 'DO-NOT-EXPOSE'}, 'model_config_obj': {}, 'is_default': True}]


def options(config=None, mode='agent.work.normal'):
    return execution_options(config if config is not None else configuration(), entries(),
                             {'mode': mode, 'work_mode': 'work'}, governed=True)


def test_catalog_has_no_credentials_paths_or_execution_side_effects(monkeypatch):
    from openjiuwen.harness_providers.opencode import OpenCodeHarness
    monkeypatch.setattr(OpenCodeHarness, '__init__', lambda *a, **kw: pytest.fail('Provider constructed'))
    result = options()
    assert [row['available'] for row in result['options']] == [True, True, False]
    assert result['options'][1]['model_selection_keys'] == ['example#0']
    encoded = json.dumps(result)
    for private in ('DO-NOT-EXPOSE', '127.0.0.1', 'model-account:', 'provider_config', 'api_base'):
        assert private not in encoded


@pytest.mark.parametrize('params', [None, {}, {'mode': 'agent', 'work_mode': 'bogus'},
    {'mode': 'agent', 'work_mode': 'work', 'session_id': 'private'},
    {'mode': 'agent', 'work_mode': 'work', 'provider_config': {}}])
def test_options_reject_authority_or_arbitrary_configuration_fields(params):
    with pytest.raises((ValueError, TypeError)):
        parse_execution_options(params)


def test_absent_catalog_retains_native_and_broken_catalog_does_not_fallback():
    assert options({})['options'][0]['config_fingerprint'] == 'legacy-native'
    with pytest.raises(ValueError):
        options({'execution': {'default_profile_id': 'missing', 'profiles': {}}})


def test_unqualified_combination_remains_unavailable():
    assert not options(mode='agent.work.plan')['options'][1]['available']
    assert all(not row['available'] for row in options(mode='team.work.normal')['options'])
    config = configuration()
    config['execution']['profiles']['opencode']['provider_config']['model']['api_key'] = 'DO-NOT-EXPOSE'
    assert not options(config)['options'][1]['available']


def test_historical_display_never_uses_changed_profile():
    config = configuration()
    row = options(config)['options'][1]
    metadata = {'execution_profile_id': 'opencode', 'execution_config_revision': 'r1',
                'execution_config_fingerprint': row['config_fingerprint']}
    assert execution_display(metadata, config)['provider_id'] == 'opencode'
    config['execution']['profiles']['opencode']['config_revision'] = 'r2'
    assert execution_display(metadata, config)['provider_id'] is None
    assert execution_display({}, config)['provider_id'] == 'native'


@pytest.mark.asyncio
@pytest.mark.parametrize('expected', ['legacy-native', '0' * 64])
async def test_configuration_drift_rejected_before_session_claim(tmp_path, monkeypatch, expected):
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', configuration)
    with pytest.raises(SessionProvisionError, match='configuration changed'):
        await _provisioner(state).prepare_session_create(_input(
            execution_profile_id='native', execution_expected_fingerprint=expected))
    assert 'claim' not in state.events


@pytest.mark.parametrize('fingerprint', ['', 'x' * 64, 123])
def test_expected_fingerprint_validation(fingerprint):
    with pytest.raises(ValueError):
        SessionCreateInput(channel_id='web', execution_expected_fingerprint=fingerprint)

@pytest.mark.asyncio
async def test_adapter_requires_principal_and_never_echoes_config_errors(monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.gateway_adapter.session_adapter import SessionAdapter
    from jiuwenswarm.governance import organization_auth, model_credentials
    from jiuwenswarm.common import config
    request = AgentRequest(request_id='options', channel_id='web',
        req_method=ReqMethod.SESSION_EXECUTION_OPTIONS,
        params={'mode':'agent.work.normal','work_mode':'work'})
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'current_identity', lambda: None)
    monkeypatch.setattr(config, 'get_config', lambda: pytest.fail('must authenticate first'))
    response = await SessionAdapter().handle(request)
    assert not response.ok
    assert response.payload['code'] == 'FORBIDDEN'
    monkeypatch.setattr(organization_auth, 'current_identity', lambda: object())
    def broken():
        raise ValueError('SECRET_config_value')
    monkeypatch.setattr(config, 'get_config', broken)
    response = await SessionAdapter().handle(request)
    assert not response.ok
    assert response.payload['code'] == 'CONFIGURATION_UNAVAILABLE'
    assert 'SECRET' not in str(response)
    monkeypatch.setattr(config, 'get_config', configuration)
    monkeypatch.setattr(model_credentials, 'configured_model_metadata', entries)
    response = await SessionAdapter().handle(request)
    assert response.ok
    assert response.payload['default_profile_id'] == 'native'
    request.params['session_id'] = 'private'
    response = await SessionAdapter().handle(request)
    assert not response.ok
    assert response.payload['code'] == 'BAD_REQUEST'


def test_personal_supported_providers_keep_original_creation_routes():
    config = configuration()
    config['execution']['profiles']['opencode']['provider_config'].pop('model')
    result = execution_options(config, [], {'mode':'agent.code.normal','work_mode':'code'}, governed=False)
    assert all(row['available'] for row in result['options'])
    assert all(row['model_selection_keys'] is None for row in result['options'])
