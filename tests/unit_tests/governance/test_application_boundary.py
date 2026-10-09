"""Application authorization must not derive permissions from sharing state."""
from dataclasses import replace

import pytest

from jiuwenswarm.governance.application_boundary import (
    INSTANCE_CONFIG_READS, CHANNEL_CONFIG_WRITES, admit_application_request, builtin_catalog_projection,
)
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionSharingDenied


ALICE = TrustedIdentity('alice', 'alice', 'test:authority')
BOB = TrustedIdentity('bob', 'bob', 'test:authority')


@pytest.mark.parametrize('method', sorted(CHANNEL_CONFIG_WRITES))
def test_channel_settings_write_uses_management_not_session_or_sharing(method):
    policy = {'settings': ['manage']}
    permit = admit_application_request(method, {'enabled': False}, identity_resolver=lambda: ALICE,
                                       policy_supplier=lambda _: policy)
    assert permit.revalidate()
    with pytest.raises(SessionSharingDenied):
        admit_application_request(method, {'enabled': False}, identity_resolver=lambda: BOB,
                                  policy_supplier=lambda _: {})
    policy.clear()
    assert not permit.revalidate()


@pytest.mark.parametrize('method,resource', [
    ('extensions.list', 'extensions'), ('extensions.import', 'extensions'),
    ('extensions.delete', 'extensions'), ('extensions.toggle', 'extensions'),
    ('skills.toggle', 'extensions'), ('hooks.list', 'settings'),
    ('plugin_packages.show', 'extensions'), ('agent_templates.show', 'extensions'),
    ('agent_templates.file.list', 'extensions'), ('agent_templates.file.read', 'extensions'),
    ('agent_groups.show', 'extensions'), ('agent_groups.file.list', 'extensions'),
    ('agent_groups.file.read', 'extensions'), ('mcp.show', 'extensions'),
])
def test_instance_consumers_require_explicit_live_management(method, resource):
    policy = {}
    with pytest.raises(SessionSharingDenied):
        admit_application_request(method, {}, identity_resolver=lambda: ALICE,
                                  policy_supplier=lambda _: policy)
    policy[resource] = ['manage']
    permit = admit_application_request(method, {}, identity_resolver=lambda: ALICE,
                                       policy_supplier=lambda _: policy)
    assert permit.revalidate()
    policy.clear()
    assert not permit.revalidate()


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_extension_toggle_rechecks_after_agent_setup_before_mutation(monkeypatch, revoked):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from jiuwenswarm.governance import organization_auth, session_boundary
    from jiuwenswarm.server import agent_ws_server

    policy = {'extensions': ['manage']}
    permit = admit_application_request('extensions.toggle', {}, identity_resolver=lambda: ALICE,
                                       policy_supplier=lambda _: policy)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    manager = Mock()
    manager.toggle_extension.return_value = {'name': 'synthetic', 'enabled': True}
    manager.hot_reload_rail = AsyncMock()
    monkeypatch.setattr(agent_ws_server, 'get_rail_manager', lambda: manager)
    monkeypatch.setattr(agent_ws_server, 'send_wire_payload', AsyncMock())

    async def setup():
        if revoked:
            policy.clear()
        return object()

    server = SimpleNamespace(_agent_manager=SimpleNamespace(
        get_agent_nowait=lambda: SimpleNamespace(ensure_instance=setup)))
    request = SimpleNamespace(request_id='synthetic-toggle', channel_id='web',
                              params={'name': 'synthetic', 'enabled': True})
    with session_boundary.delivery_scope():
        session_boundary.set_delivery_permit(permit)
        await agent_ws_server.AgentWebSocketServer._handle_extensions_toggle(
            server, object(), request, asyncio.Lock())
    if revoked:
        manager.set_agent_instance.assert_not_called()
        manager.toggle_extension.assert_not_called()
        manager.hot_reload_rail.assert_not_awaited()
    else:
        manager.toggle_extension.assert_called_once_with('synthetic', True)
        manager.hot_reload_rail.assert_awaited_once_with('synthetic', True)


def test_catalog_needs_identity_but_no_sharing_host_or_project():
    permit = admit_application_request('plugin_packages.list', {}, identity_resolver=lambda: ALICE)
    assert permit.host is None
    assert permit.revalidate()
    with pytest.raises(SessionSharingDenied):
        admit_application_request('plugin_packages.list', {}, identity_resolver=lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_mcp_detail_rechecks_after_hub_lookup(monkeypatch, revoked):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from jiuwenswarm.governance import organization_auth, session_boundary
    from jiuwenswarm.server import agent_ws_server

    policy = {'extensions': ['manage']}
    permit = admit_application_request('mcp.show', {'name': 'synthetic'},
                                       identity_resolver=lambda: ALICE,
                                       policy_supplier=lambda _: policy)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    async def lookup(name):
        assert name == 'synthetic'
        if revoked:
            policy.clear()
        return {'name': name, 'connection_state': 'disconnected', 'tools': []}
    monkeypatch.setattr('jiuwenswarm.server.runtime.mcp.marketplace.show_mcp_with_hub', lookup)
    fallback = AsyncMock()
    monkeypatch.setattr('jiuwenswarm.common.mcp_config.fill_mcp_tools_fallback', fallback)
    send = AsyncMock()
    monkeypatch.setattr(agent_ws_server, 'send_wire_payload', send)
    request = SimpleNamespace(request_id='detail', channel_id='web', params={'name': 'synthetic'})
    with session_boundary.delivery_scope():
        session_boundary.set_delivery_permit(permit)
        await agent_ws_server.AgentWebSocketServer._handle_mcp_show(
            object(), object(), request, asyncio.Lock())
    if revoked:
        fallback.assert_not_awaited()
    else:
        fallback.assert_awaited_once()
    assert send.await_args.args[1]['response_kind'] == ('e2a.error' if revoked else 'e2a.complete')


def test_identity_change_stops_queued_catalog_delivery():
    current = [ALICE]
    permit = admit_application_request('plugin_packages.list', {}, identity_resolver=lambda: current[0])
    current[0] = BOB
    assert not permit.revalidate()


def test_settings_permission_is_explicit_and_rechecked_without_sharing():
    policy = {'settings': ['manage']}
    permit = admit_application_request('config.set', {}, identity_resolver=lambda: ALICE,
                                       policy_supplier=lambda _: policy)
    assert permit.revalidate()
    policy.clear()
    assert not permit.revalidate()
    with pytest.raises(SessionSharingDenied):
        admit_application_request('config.set', {}, identity_resolver=lambda: ALICE,
                                  policy_supplier=lambda _: policy)


@pytest.mark.parametrize('params', [{'user_id': 'bob'}, {'actor_id': 'alice'},
                                    {'authority': 'test:authority'}, {'share_id': 's'},
                                    {'filter': '../private'}, {'include_secrets': True}])
def test_catalog_rejects_identity_and_scope_injection(params):
    with pytest.raises(SessionSharingDenied):
        admit_application_request('plugin_packages.list', params, identity_resolver=lambda: ALICE)


def test_catalog_is_a_projection_not_a_raw_global_package_listing(monkeypatch):
    from jiuwenswarm.server.runtime import extension_package_manager as packages
    calls = []
    def read(params):
        calls.append(params)
        return [{'id': 'public', 'source': 'builtin', 'installed': True,
                 'path': '/secret/home', 'credentials': {'token': 'secret'},
                 'avatar': '/private/avatar'},
                {'id': 'bobs-package', 'source': 'local', 'installed': True}]
    monkeypatch.setattr(packages, 'list_plugin_packages', read)
    result = builtin_catalog_projection('plugin_packages.list', {})
    assert calls[0]['filter'] == 'builtin'
    assert result['packages'] == [{'id': 'public', 'source': 'builtin', 'installed': False, 'read_only': True, 'connection_state': 'disconnected'}]
    assert result['read_only'] is True


def test_unclassified_method_does_not_gain_access_by_name():
    with pytest.raises(SessionSharingDenied):
        admit_application_request('secrets.list', {}, identity_resolver=lambda: ALICE)

@pytest.mark.parametrize('method,key', [('plugin_packages.list','packages'), ('mcp.list','items'),
                                       ('agent_templates.list','templates'), ('agent_groups.list','agentGroups')])
def test_public_catalog_does_not_claim_instance_installations_are_mine(method, key):
    result = builtin_catalog_projection(method, {'filter': 'mine'})
    assert result[key] == []
    assert result['read_only'] is True


def test_vendor_metadata_is_not_a_shared_secret_or_project_resource():
    assert admit_application_request('vendors.list', {}, identity_resolver=lambda: ALICE).revalidate()


@pytest.mark.parametrize('method,resource', [('models.list', 'settings'), ('plugin_packages.list', 'extensions')])
def test_operator_read_role_is_rechecked_before_delivery(method, resource):
    policy = {resource: ['manage']}
    permit = admit_application_request(method, {}, identity_resolver=lambda: ALICE,
                                       policy_supplier=lambda _: policy)
    assert permit.revalidate()
    policy.clear()
    assert not permit.revalidate()


@pytest.mark.parametrize('field', ['token', 'system_token', 'user_id', 'market_url', '_catalog_anonymous'])
def test_public_hub_cannot_borrow_a_private_identity_or_endpoint(field):
    with pytest.raises(SessionSharingDenied):
        admit_application_request('skills.swarmskillshub.recommend', {field: 'private'},
                                  identity_resolver=lambda: ALICE)


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_application_queue_rechecks_role_without_chat_side_effects(monkeypatch, revoked):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from jiuwenswarm.governance import organization_auth, session_boundary, application_boundary
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.governance.contracts import TrustedIdentity
    actor = TrustedIdentity('operator', 'operator', 'organization')
    principal = SimpleNamespace(identity=lambda: actor)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: None)
    original_admit = application_boundary.admit_application_request
    monkeypatch.setattr(application_boundary, 'admit_application_request',
        lambda *args, **kwargs: original_admit(*args, **kwargs,
            policy_supplier=lambda _: {} if revoked else {'extensions': ['manage']}))
    msg = SimpleNamespace(req_method=SimpleNamespace(value='plugin_packages.install'),
                          params={'package_name': 'synthetic-test'}, is_stream=False,
                          id='application-request', channel_id='web',
                          _queued_organization_principal=principal)
    handler = SimpleNamespace(_running=True,
        _process_non_stream_request=AsyncMock(), message_to_e2a=Mock(return_value='envelope'),
        publish_robot_messages=AsyncMock(), _build_error_out_message=Mock(return_value='denied'),
        _handle_channel_control=AsyncMock(side_effect=AssertionError('chat control must not run')))
    async def consume(**_):
        handler._running = False
        return msg
    handler.consume_user_messages = consume
    await MessageHandler._forward_loop(handler)
    if revoked:
        handler._process_non_stream_request.assert_not_awaited()
        handler.publish_robot_messages.assert_awaited_once_with('denied')
    else:
        handler._process_non_stream_request.assert_awaited_once_with(msg, 'envelope')
        handler.publish_robot_messages.assert_not_awaited()
    handler._handle_channel_control.assert_not_awaited()


@pytest.mark.parametrize('method', sorted(INSTANCE_CONFIG_READS))
def test_channel_credentials_require_explicit_current_settings_maintainer(method):
    grants = {'settings': ['manage']}
    with pytest.raises(SessionSharingDenied):
        admit_application_request(method, {}, identity_resolver=lambda: BOB, policy_supplier=lambda _: {})
    permit = admit_application_request(method, {}, identity_resolver=lambda: ALICE, policy_supplier=lambda _: grants)
    assert permit.revalidate()
    grants.clear()
    assert not permit.revalidate()
    with pytest.raises(SessionSharingDenied):
        admit_application_request(method, {'account': 'other'}, identity_resolver=lambda: ALICE,
                                  policy_supplier=lambda _: {'settings': ['manage']})
