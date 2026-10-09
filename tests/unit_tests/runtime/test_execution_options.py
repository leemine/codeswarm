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


@pytest.mark.parametrize('config', [{}, {'execution': {'default_profile_id': 'native', 'profiles': {
    'native': {'provider_id': 'native', 'config_revision': 'r1'},
}}}])
def test_product_discovery_is_independent_of_configured_profiles(config, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.runtime.harness.execution_options.installed_execution_profiles", lambda: ())
    before = json.dumps(config)
    result = execution_options(config, [], {'mode': 'agent.code.normal', 'work_mode': 'code'}, governed=False)
    missing = {row['provider_id']: row for row in result['unconfigured_providers']}
    assert {row['provider_id'] for row in result['options']} | set(missing) == {
        'native', 'opencode', 'codex', 'claudecode', 'dsh',
    }
    assert missing['opencode'] == {'provider_id': 'opencode', 'reason': 'not_installed'}
    assert missing['codex']['reason'] == 'not_installed'
    assert missing['claudecode']['reason'] == 'provider_unavailable'
    # Discovery has no profile/fingerprint and does not mutate the actual catalog.
    assert all(set(row) == {'provider_id', 'reason'} for row in missing.values())
    assert len(result['options']) == 1 and result['options'][0]['available']
    assert json.dumps(config) == before


def test_discovery_preserves_scenario_restrictions_and_does_not_duplicate_profiles():
    assert not {'native', 'opencode', 'codex'} & {
        row['provider_id'] for row in options()['unconfigured_providers']
    }
    result = options({}, mode='agent.work.plan')
    assert all(row['reason'] == 'mode_unavailable' for row in result['unconfigured_providers'])
    result = options({})
    missing = {row['provider_id']: row['reason'] for row in result['unconfigured_providers']}
    assert missing['codex'] == 'provider_unavailable'  # still governed


@pytest.mark.parametrize("config", [{}, {"execution": {"default_profile_id": "native", "profiles": {
    "native": {"provider_id": "native", "config_revision": "r1"},
}}}])
def test_installed_engines_selectable_without_profiles(config, monkeypatch):
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
    monkeypatch.setattr("jiuwenswarm.runtime.harness.execution_options.installed_execution_profiles",
                        lambda: ("builtin:opencode", "builtin:codex"))
    result = execution_options(config, [], {"mode": "agent.code.normal", "work_mode": "code"}, governed=False)
    rows = {row["provider_id"]: row for row in result["options"]}
    assert set(rows) == {"native", "opencode", "codex"}
    assert all(row["available"] for row in rows.values())
    for provider in ("opencode", "codex"):
        row = rows[provider]
        assert row["execution_profile_id"] == f"builtin:{provider}"
        spec = load_execution_catalog(config, selected_profile_id=row["execution_profile_id"]).source().resolve()
        metadata = {"execution_profile_id": row["execution_profile_id"],
                    "execution_config_revision": spec.config_revision,
                    "execution_config_fingerprint": row["config_fingerprint"]}
        # CLI removal does not change the stored engine identity on refresh.
        monkeypatch.setattr("jiuwenswarm.runtime.harness.execution_options.installed_execution_profiles", lambda: ())
        assert execution_display(metadata, config)["provider_id"] == provider
    assert result["default_profile_id"] == ("native" if config else None)


def test_installed_defaults_preserve_mode_and_governance_restrictions(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.runtime.harness.execution_options.installed_execution_profiles",
                        lambda: ("builtin:opencode", "builtin:codex"))
    for mode in ("agent.code.plan", "team.code.normal"):
        result = execution_options({}, [], {"mode": mode, "work_mode": "code"}, governed=False)
        assert all(not row["available"] for row in result["options"] if row["provider_id"] != "native")
    assert len(options({})["options"]) == 1
    assert len(execution_options(configuration(), entries(),
        {"mode": "agent.code.normal", "work_mode": "code"}, governed=False)["options"]) == 3


def test_installed_opencode_reuses_default_model_without_exposing_secrets(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.runtime.harness.execution_options.installed_execution_profiles",
                        lambda: ("builtin:opencode",))
    config = {"models": {"defaults": entries()}}
    result = execution_options(config, entries(), {"mode": "agent.code.normal", "work_mode": "code"}, governed=False)
    row = result["options"][1]
    assert row["available"] and row["model_selection_keys"] == ["example#0"]
    for private in ("DO-NOT-EXPOSE", "127.0.0.1", "api_key"):
        assert private not in json.dumps(result)
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
    spec = load_execution_catalog(config, selected_profile_id=row["execution_profile_id"]).source().resolve()
    assert spec.provider_config["model"]["model"] == "example"
    assert spec.provider_config["model"]["api_key"] == "DO-NOT-EXPOSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["opencode", "codex"])
async def test_builtin_choice_is_persisted_before_allocation(tmp_path, monkeypatch, provider):
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    profile = f"builtin:{provider}"
    await _provisioner(state).prepare_session_create(_input(execution_profile_id=profile))
    metadata = get_session_metadata("created-session", cache_bust=True)
    assert metadata["execution_profile_id"] == profile
    assert metadata["execution_config_revision"] == ("installed-codex-v2" if provider == "codex" else "installed-engine-v1")
    assert metadata["surface_creation"]["execution_profile_id"] == profile
    assert execution_display(metadata, {})["provider_id"] == provider


def test_bound_display_survives_only_a_supported_runtime_authorization_change():
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
    from openjiuwen.harness.engine.config import config_fingerprint
    before = {'permissions': {'enabled': False}}
    spec = load_execution_catalog(before, selected_profile_id='builtin:codex').source().resolve()
    metadata = {'mode': 'agent.code.normal', 'execution_profile_id': 'builtin:codex',
        'execution_config_revision': spec.config_revision, 'execution_config_fingerprint': config_fingerprint(spec)}
    assert execution_display(metadata, {'permissions': {'enabled': True}})['provider_id'] == 'codex'
    assert execution_display({**metadata, 'mode': 'team.code.normal'}, {'permissions': {'enabled': True}})['provider_id'] is None
    assert execution_display({**metadata, 'execution_config_revision': 'changed'}, before)['provider_id'] is None
