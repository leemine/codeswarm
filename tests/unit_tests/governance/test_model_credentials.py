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
                   credential_reference='model-account:' + actor, api_key=actor + '-synthetic-token')
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
            current_identity=lambda identity=identity: identity, is_current_execution=lambda: True, config_source=source)
    yield store, project.project_id, bindings, calls, config, source
    project_store.invalidate_cache()


def target(binding):
    return SimpleNamespace(method='POST', url=binding.destination, model=binding.model,
                           implementation='OpenAIModelClient', api_mode='chat_completions', operation='invoke')


@pytest.mark.asyncio
async def test_same_model_and_endpoint_resolve_only_explicit_subject_reference(models):
    _, _, bindings, calls, _, source = models
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](bindings['alice'], target(bindings['alice']))
    source.assert_not_called()
    for actor in ('alice', 'bob'):
        assert await calls[actor](bindings[actor], target(bindings[actor])) == {
            'Authorization': 'Bearer ' + actor + '-synthetic-token'}


@pytest.mark.asyncio
async def test_revoked_credential_does_not_resolve_even_while_project_execute_remains(models):
    store, pid, bindings, calls, _, source = models
    binding = bindings['bob']
    assert await calls['bob'](binding, target(binding))
    store.revoke_resource(pid, TrustedIdentity('bob', 'bob', 'organization'), 'bob',
                          subject_id='bob', expected_revision=2)
    assert store.authorize(pid, 'bob', 'execute').allowed
    source.reset_mock()
    with pytest.raises(ResourceAccessDenied):
        await calls['bob'](binding, target(binding))
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
        await calls['bob'](bindings['bob'], actual)
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
        await calls['bob'](bindings['bob'], target(bindings['bob']))
