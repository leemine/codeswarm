"""Exercise real private Host/Core collection; never use a Project or fake Session."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.application_boundary import admit_application_request
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.personal_context_execution import PersonalContextAuthority
from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


@pytest.fixture
def private_hosts(monkeypatch, tmp_path):
    identities = {name: TrustedIdentity(name, name, 'test:personal-context') for name in ('alice', 'bob')}
    active = dict(identities)
    sources = {}
    for name in identities:
        sources[name] = tmp_path / name / 'notes'
        sources[name].mkdir(parents=True)
        (sources[name] / 'note.md').write_text(f'# {name.upper()}_ONLY\nSynthetic private note for {name}.')
    policy = {'personal_context_sources': {name: {'read_roots': [str(path)]} for name, path in sources.items()},
              'private_session_resources': {name: {'revision': 1, 'resources': []} for name in identities}}
    auth = SimpleNamespace(known_actor=lambda identity: identity in identities.values(), _config=lambda: policy)
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: auth)
    monkeypatch.setattr('jiuwenswarm.governance.personal_context_execution.configured_authenticator', lambda: auth)
    monkeypatch.setattr('jiuwenswarm.governance.model_credentials.configured_model_metadata', lambda: [])
    hosts = {name: PersonalContextHostAPI(home=tmp_path/name/'context', authority=PersonalContextAuthority(identity))
             for name, identity in identities.items()}
    def permit(name):
        return admit_application_request('personal_context.runtime.start_collection', {},
                                         identity_resolver=lambda: active.get(name))
    return hosts, sources, policy, active, permit


def config(root):
    return {'collection_enabled': True, 'agent_use_enabled': False, 'strategy_profile': 'rules',
            'model_index': None, 'model_id': None, 'fetch_services': [{
                'service_id': 'notes', 'provider': 'local_files', 'enabled': True,
                'interval_seconds': 60, 'source': {'root_dir': str(root)}, 'time_range': {'mode': 'all'},
                'credentials': {}}]}


async def wait_for_page(host, marker):
    async with asyncio.timeout(15):
        while True:
            if any(marker in p.read_text() for p in (host._home/'workspace/context').rglob('*.md')):
                return
            await asyncio.sleep(.02)


@pytest.mark.asyncio
async def test_two_real_collectors_publish_only_their_sources_and_stop_on_revoke(private_hosts):
    hosts, sources, policy, active, permit = private_hosts
    try:
        for name, host in hosts.items():
            await host.bind_request_authority(permit(name))
            await host.configure(config(sources[name]))
            await host.run_fetch(service_id="notes")
        await asyncio.gather(*(wait_for_page(host, name.upper()+'_ONLY') for name, host in hosts.items()))
        for name, host in hosts.items():
            text = '\n'.join(p.read_text() for p in (host._home/'workspace/context').rglob('*.md'))
            other = 'BOB_ONLY' if name == 'alice' else 'ALICE_ONLY'
            assert other not in text
        active.pop('alice')
        async with asyncio.timeout(5):
            await hosts['alice']._authority_watch
        with pytest.raises(ResourceAccessDenied):
            await hosts['alice'].run_fetch()
        assert not hosts['bob']._authority_watch.done()
    finally:
        for host in hosts.values():
            await host.stop()


@pytest.mark.asyncio
async def test_foreign_source_and_symlink_are_denied_before_configuration(private_hosts, tmp_path):
    hosts, sources, _, _, permit = private_hosts
    host = hosts['alice']
    await host.bind_request_authority(permit('alice'))
    try:
        with pytest.raises(ResourceAccessDenied):
            await host.configure(config(sources['bob']))
        link = sources['alice'] / 'foreign'
        link.symlink_to(sources['bob'], target_is_directory=True)
        with pytest.raises(ResourceAccessDenied):
            await host.configure(config(link))
        assert not host._config_path.exists()
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_model_secret_is_resolved_only_for_exact_live_grant(private_hosts, monkeypatch):
    from jiuwenswarm.governance import personal_context_execution as execution
    from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
    from openjiuwen.harness.personal_context import PersonalContext
    hosts, sources, policy, active, permit = private_hosts
    entry = {'model_client_config': {'model_name': 'fixture', 'api_base': 'https://model.invalid/v1',
             'client_provider': 'OpenAI', 'credential_encoding': 'plain', 'api_key': 'MODEL_REQUEST_AUTHORITY'},
             'model_config_obj': {}}
    binding = ModelCredentialBinding.from_config(entry['model_client_config'])
    monkeypatch.setattr(execution, 'configured_model_metadata', lambda: [deepcopy(entry)])
    actual = deepcopy(entry); actual['model_client_config']['api_key'] = 'authorized-fixture-only'
    monkeypatch.setattr('jiuwenswarm.common.config.get_config_raw', lambda: {'models': {'defaults': [actual]}})
    authority = hosts['alice']._authority
    await hosts['alice'].bind_request_authority(permit('alice'))
    raw = config(sources['alice']); raw.pop('model_index'); raw.pop('model_id')
    raw['model_client'] = dict(entry['model_client_config']); raw['model_client'].pop('model_name')
    raw['model_request'] = {'model': 'fixture'}
    raw['strategy_profile'] = 'balanced'
    authority.config = PersonalContext.Config.from_dict(raw)
    target = SimpleNamespace(method='POST', url=binding.destination, model='fixture',
                             api_mode='chat_completions', implementation='OpenAIModelClient', operation='invoke')
    try:
        with pytest.raises(ResourceAccessDenied):
            await authority.model_request(target)
        policy['private_session_resources']['alice']['resources'] = [
            {'resource_id': 'model', 'kind': 'credential', 'reference': binding.reference, 'actions': ['use']}]
        assert await authority.model_request(target) == {'Authorization': 'Bearer authorized-fixture-only'}
        target.url = 'https://other.invalid/v1/chat/completions'
        with pytest.raises(ResourceAccessDenied):
            await authority.model_request(target)
        target.url = binding.destination
        policy['private_session_resources']['alice']['resources'] = []
        with pytest.raises(ResourceAccessDenied):
            await authority.model_request(target)
    finally:
        await hosts['alice'].stop()


@pytest.mark.asyncio
async def test_feishu_cli_requires_source_scope_before_status_or_login(private_hosts, monkeypatch):
    from unittest.mock import AsyncMock

    hosts, sources, policy, _, permit = private_hosts
    host = hosts['alice']
    await host.bind_request_authority(permit('alice'))
    try:
        await host.configure(config(sources['alice']))
        status = AsyncMock(return_value={'status': 'not_authorized'})
        login = AsyncMock(return_value={'status': 'authorization_pending'})
        monkeypatch.setattr(host._personal_context, 'get_authorization_status', status)
        monkeypatch.setattr(host._personal_context, 'authorize_provider', login)
        for operation in (host.get_authorization_status, host.authorize_provider):
            with pytest.raises(ResourceAccessDenied):
                await operation('feishu')
        status.assert_not_awaited()
        login.assert_not_awaited()
        policy['personal_context_sources']['alice']['feishu'] = True
        assert await host.get_authorization_status('feishu') == {'status': 'not_authorized'}
        assert await host.authorize_provider('feishu') == {'status': 'authorization_pending'}
        policy['personal_context_sources']['alice']['feishu'] = False
        with pytest.raises(ResourceAccessDenied):
            await host.authorize_provider('feishu')
        login.assert_awaited_once()
    finally:
        await host.stop()
