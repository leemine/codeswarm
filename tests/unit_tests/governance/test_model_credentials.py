from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.model_credentials import ModelCredentialBinding, NativeModelCredentialAuthority
from jiuwenswarm.governance.resources import ResourceAccessDenied, ResourceDefinition
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    project_store.invalidate_cache()
    project = project_store.create_project('Models', str(tmp_path))
    store = ProjectAccessStore()
    store.initialize(project.project_id, 'alice')
    store.replace_acl(project.project_id, 'alice', acl={'bob': ['read', 'execute']}, expected_revision=1)
    entries, bindings, calls = [], {}, {}
    for index, actor in enumerate(('alice', 'bob')):
        mcc = dict(model_name='same-model', api_base='https://model.example/v1', client_provider='OpenAI',
                   credential_reference='model-account:' + actor, credential_encoding='plain',
                   api_key=actor + '-synthetic-token')
        entries.append({'model_client_config': mcc, 'model_config_obj': {}})
        binding = ModelCredentialBinding.from_config(mcc)
        bindings[actor] = binding
        store.register_resource(project.project_id, ResourceDefinition(actor, 'credential', binding.reference),
                                owner_subject_id=actor, actions=('use',), expected_revision=index)
    config = {'models': {'defaults': entries}}
    source = Mock(return_value=config)
    for actor in ('alice', 'bob'):
        identity = TrustedIdentity(actor, actor, 'organization')
        execution = ResourceExecutionContext(project.project_id, identity, actor + '-private', str(tmp_path), 'native')
        calls[actor] = NativeModelCredentialAuthority(execution, resource_authorizer=store,
            current_identity=lambda identity=identity: identity, is_current_execution=lambda: True, owns_execution=lambda execution, native: native is calls, config_source=source)
    yield store, project.project_id, bindings, calls, config, source
    project_store.invalidate_cache()


def target(binding):
    return SimpleNamespace(method='POST', url=binding.destination, model=binding.model,
                           implementation='OpenAIModelClient', api_mode='chat_completions', operation='invoke')


@pytest.mark.asyncio
async def test_same_model_and_endpoint_resolve_only_explicit_subject_reference(models):
    _, _, bindings, calls, _, source = models
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](bindings['alice'], target(bindings['alice']), native_session=calls)
    source.assert_not_called()
    for actor in ('alice', 'bob'):
        assert await calls[actor](bindings[actor], target(bindings[actor]), native_session=calls) == {
            'Authorization': 'Bearer ' + actor + '-synthetic-token'}


@pytest.mark.asyncio
async def test_revoked_credential_does_not_resolve_even_while_project_execute_remains(models):
    store, pid, bindings, calls, _, source = models
    binding = bindings['bob']
    assert await calls['bob'](binding, target(binding), native_session=calls)
    store.revoke_resource(pid, TrustedIdentity('bob', 'bob', 'organization'), 'bob',
                          subject_id='bob', expected_revision=2)
    assert store.authorize(pid, 'bob', 'execute').allowed
    source.reset_mock()
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](binding, target(binding), native_session=calls)
    source.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('url', 'https://other.example/v1/chat/completions'),
                                         ('model', 'another-model'), ('implementation', 'OtherClient'),
                                         ('api_mode', 'responses'), ('method', 'GET')])
async def test_actual_request_target_checked_before_catalog_access(models, field, value):
    _, _, bindings, calls, _, source = models
    actual = target(bindings['bob'])
    setattr(actual, field, value)
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](bindings['bob'], actual, native_session=calls)
    source.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['empty', 'duplicate', 'oauth', 'changed_endpoint'])
async def test_no_ambient_or_ambiguous_catalog_fallback(models, monkeypatch, change):
    _, _, bindings, calls, config, _ = models
    monkeypatch.setenv('API_KEY', 'ambient-must-not-be-used')
    entries = config['models']['defaults']
    if change == 'empty':
        config['models'] = {}
    elif change == 'duplicate':
        entries.append(entries[1])
    elif change == 'oauth':
        entries[1]['model_client_config']['api_key'] = 'jiuwen-login:another-user'
    else:
        entries[1]['model_client_config']['api_base'] = 'https://other.example/v1'
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](bindings['bob'], target(bindings['bob']), native_session=calls)


@pytest.mark.asyncio
async def test_default_source_never_expands_environment(models, monkeypatch):
    import jiuwenswarm.common.config as config_module
    _, _, bindings, calls, config, _ = models
    config['models']['defaults'][1]['model_client_config']['api_key'] = '${API_KEY}'
    monkeypatch.setenv('API_KEY', 'ambient-secret-sentinel')
    raw = Mock(return_value=config)
    expanded = Mock(side_effect=AssertionError('expanded config must not be consumed'))
    monkeypatch.setattr(config_module, 'get_config_raw', raw)
    monkeypatch.setattr(config_module, 'get_config', expanded)
    calls['bob']._config_source = None
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](bindings['bob'], target(bindings['bob']), native_session=calls)
    raw.assert_called_once()
    expanded.assert_not_called()


@pytest.mark.asyncio
async def test_decrypt_failure_is_not_logged_or_used_as_raw_credential(models, monkeypatch, caplog):
    import traceback
    _, _, _, calls, config, _ = models
    mcc = config['models']['defaults'][1]['model_client_config']
    mcc['credential_encoding'] = 'host_crypto'
    mcc['api_key'] = 'encrypted-sentinel'
    binding = ModelCredentialBinding.from_config(mcc)
    crypto = SimpleNamespace(decrypt=Mock(side_effect=RuntimeError('decrypt-secret-sentinel')))
    calls['bob']._credential_decoder = crypto.decrypt
    with pytest.raises(ResourceAccessDenied) as error:
        await calls['bob'](binding, target(binding), native_session=calls)
    crypto.decrypt.assert_called_once_with('encrypted-sentinel')
    assert 'decrypt-secret-sentinel' not in caplog.text
    assert 'decrypt-secret-sentinel' not in ''.join(traceback.format_exception(error.value))


def test_credential_encoding_must_be_explicit():
    with pytest.raises(ValueError):
        ModelCredentialBinding.from_config({'model_name': 'x', 'api_base': 'https://model.example/v1'})


@pytest.mark.asyncio
async def test_model_consumer_requires_exact_live_host_owner(models):
    _, _, bindings, calls, _, source = models
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](bindings['bob'], target(bindings['bob']), native_session=object())
    source.assert_not_called()
    calls['bob']._owns_execution = lambda execution, native: False
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](bindings['bob'], target(bindings['bob']), native_session=calls)
    source.assert_not_called()
