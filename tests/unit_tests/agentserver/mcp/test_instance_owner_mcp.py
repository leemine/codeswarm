"""Owner MCP policy reuses the existing store and rejects foreign consumers."""
import hashlib
import json
import os
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.governance.instance_access import (
    MCP_OWNER_METHODS,
    capture_instance_mcp_check,
    require_instance_mcp_access,
)
from jiuwenswarm.governance.organization_auth import (
    CONFIG_ENV,
    OrganizationAuthenticator,
    authenticated_scope,
)


@pytest.fixture
def actors(tmp_path, monkeypatch):
    path = tmp_path / 'organization.json'
    config = {'authority': 'owner-mcp-test', 'signing_key': 'ab' * 32, 'credentials': []}
    tokens = {name: 'synthetic-owner-mcp-test-token-' + name + '-long-enough' for name in ('alice', 'bob')}
    for name, token in tokens.items():
        config['credentials'].append({'actor_id': name, 'sha256': hashlib.sha256(token.encode()).hexdigest(),
                                      'expires_at': time.time() + 600})
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    auth = OrganizationAuthenticator(path)
    principals = {name: auth.principal({'Authorization': 'Bearer ' + token}) for name, token in tokens.items()}
    config['instance_owner'] = asdict(principals['alice'].identity())
    path.write_text(json.dumps(config))
    monkeypatch.setenv(CONFIG_ENV, str(path))
    return principals, path


@pytest.mark.parametrize('method', sorted(MCP_OWNER_METHODS))
def test_owner_only_application_admission_and_live_revoke(actors, method):
    from jiuwenswarm.governance.application_boundary import admit_application_request
    from jiuwenswarm.governance.session_sharing import SessionSharingDenied
    principals, path = actors
    with authenticated_scope(principals['alice']):
        permit = admit_application_request(method, {'name': 'fixture'}, identity_resolver=principals['alice'].identity)
        assert permit.revalidate()
    with authenticated_scope(principals['bob']):
        with pytest.raises(SessionSharingDenied):
            admit_application_request(method, {'name': 'fixture'}, identity_resolver=principals['bob'].identity)
    data = json.loads(path.read_text())
    data.pop('instance_owner')
    path.write_text(json.dumps(data))
    assert not permit.revalidate()


def test_owner_credential_store_rejects_cross_account_read_write_delete(actors, tmp_path):
    from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
    principals, _ = actors
    store = CredentialStore(workspace_dir=tmp_path)
    with authenticated_scope(principals['alice']):
        store.save_token('fixture', 'SYNTHETIC_TOKEN', 'owner-only')
        assert store.get_all('fixture') == {'SYNTHETIC_TOKEN': 'owner-only'}
        check = capture_instance_mcp_check()
    raw = store._path('fixture').read_bytes()
    assert b'owner-only' not in raw
    with authenticated_scope(principals['bob']):
        for call in (lambda: store.get_all('fixture'), lambda: store.save_token('fixture', 'K', 'v'),
                     lambda: store._save('fixture', {}), lambda: store.delete_mcp('fixture'), check):
            with pytest.raises(PermissionError):
                call()
    assert store._path('fixture').read_bytes() == raw
    assert 'SYNTHETIC_TOKEN' not in os.environ


def test_owner_change_does_not_reuse_explicit_cache_id(actors):
    from jiuwenswarm.common.mcp_config import build_mcp_server_config
    principals, path = actors
    entry = {'name': 'fixture', 'server_id': 'caller-fixed-id', 'transport': 'stdio', 'command': 'unused'}
    with authenticated_scope(principals['alice']):
        first = build_mcp_server_config(entry)
    data = json.loads(path.read_text())
    data['instance_owner'] = asdict(principals['bob'].identity())
    path.write_text(json.dumps(data))
    with authenticated_scope(principals['bob']):
        second = build_mcp_server_config(entry)
    assert first.server_id != second.server_id != 'caller-fixed-id'
    with authenticated_scope(principals['alice']), pytest.raises(PermissionError):
        require_instance_mcp_access()


def test_pending_oauth_cannot_outlive_its_original_owner(actors, monkeypatch):
    from jiuwenswarm.server.runtime.mcp.cli_driver import (
        _PENDING_AUTH_PROCS,
        CliDriver,
        CliManifest,
    )
    principals, path = actors
    proc = Mock()
    proc.poll.return_value = None
    with authenticated_scope(principals['alice']):
        driver = CliDriver('fixture', manifest=CliManifest(), runner=lambda _: None,
                           proc_runner=lambda _: (proc, 'https://example.invalid/authorize'))
        driver._start_auth_proc(0, 'unused', 'example.invalid')
        assert driver.auth_proc_done() is False
    data = json.loads(path.read_text())
    data.pop('instance_owner')
    path.write_text(json.dumps(data))
    with authenticated_scope(principals['alice']), pytest.raises(PermissionError):
        driver.auth_proc_done()
    assert 'fixture' not in _PENDING_AUTH_PROCS
    proc.kill.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)


def test_cli_launcher_path_is_child_only(monkeypatch):
    from jiuwenswarm.server.runtime.mcp import cli_driver
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    original = os.environ.get('PATH')
    monkeypatch.setattr(cli_driver, '_binary_dir_from_package', lambda: '/synthetic/launcher')
    seen = []
    monkeypatch.setattr(cli_driver, 'default_runner', lambda cmd, env=None: seen.append(env))
    driver = cli_driver.CliDriver('fixture', manifest=cli_driver.CliManifest())
    driver._run_command('unused')
    assert seen[0]['PATH'].startswith('/synthetic/launcher' + os.pathsep)
    assert os.environ.get('PATH') == original


def test_shell_credentials_require_original_owner_and_selected_connection(actors, tmp_path, monkeypatch):
    from jiuwenswarm.governance.instance_mcp import shell_environment
    from jiuwenswarm.server.runtime.mcp import credential, state_store
    principals, path = actors
    monkeypatch.setattr(credential, 'get_workspace_dir', lambda: tmp_path)
    monkeypatch.setattr(state_store, 'list_connected_mcps', lambda: [{'name': 'fixture'}, {'name': 'unselected'}])
    with authenticated_scope(principals['alice']):
        store = credential.CredentialStore()
        store.save_token('fixture', 'SYNTHETIC_CHILD', 'selected-only')
        store.save_token('unselected', 'MUST_NOT_PASS', 'other')
    agent, session = object(), object()
    check = Mock()
    native = SimpleNamespace(closed=False, _tool_owner=(None, agent, session),
        engine=SimpleNamespace(binding=SimpleNamespace(subject_id='alice', host_session_id='original')),
        _active_entry=lambda: SimpleNamespace(lifecycle=SimpleNamespace(source=SimpleNamespace(_check_current=check))))
    adapter = SimpleNamespace(_session_selected_mcp={'fixture'}, _native_execution=native, _parent_session_id='original')
    source = SimpleNamespace(is_current_origin=lambda: True, agent_context=SimpleNamespace(agent=agent, session=session))
    bound = SimpleNamespace(active=True, owner=native)
    monkeypatch.setattr('openjiuwen.core.foundation.tool.current_tool_execution', lambda: source)
    monkeypatch.setattr('jiuwenswarm.governance.tool_context.current_native_execution_slice', lambda: bound)
    with authenticated_scope(principals['alice']):
        assert shell_environment(adapter) == {'SYNTHETIC_CHILD': 'selected-only'}
        assert 'SYNTHETIC_CHILD' not in os.environ
        source.agent_context.session = object()
        with pytest.raises(PermissionError):
            shell_environment(adapter)
        source.agent_context.session = session
        bound.active = False
        with pytest.raises(PermissionError):
            shell_environment(adapter)
        bound.active = True
        data = json.loads(path.read_text())
        data.pop('instance_owner')
        path.write_text(json.dumps(data))
        assert shell_environment(adapter) == {}
    with authenticated_scope(principals['bob']):
        assert shell_environment(adapter) == {}


@pytest.mark.asyncio
async def test_actual_sdk_mcp_owner_call_foreign_session_and_revocation(actors, tmp_path, monkeypatch):
    import asyncio
    import sys
    from pathlib import Path

    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackContext,
        AgentCallbackEvent,
    )

    from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import (
        NativeResourceAuthorityRail,
    )
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.common import mcp_config
    from jiuwenswarm.governance.instance_access import authorize_native_catalog
    from jiuwenswarm.governance.instance_mcp import (
        authorize_instance_mcp,
        install_instance_mcp_tools,
    )
    from jiuwenswarm.governance.tool_context import tool_authority_scope
    from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
    from jiuwenswarm.server.runtime.mcp import credential, state_store
    principals, path = actors
    identity = principals['alice'].identity()
    entry = {'name': 'fixture', 'transport': 'stdio', 'command': sys.executable,
             'args': [str(Path(__file__).with_name('owner_mcp_fixture.py'))]}
    monkeypatch.setattr(config_module, 'get_mcp_server_config', lambda _: entry)
    monkeypatch.setattr(state_store, 'list_connected_mcps', lambda: [{'name': 'fixture'}])
    monkeypatch.setattr(credential, 'get_workspace_dir', lambda: tmp_path)
    manager = AbilityManager(owner_id='owner-mcp-' + tmp_path.name)
    rail = NativeResourceAuthorityRail()
    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
                await rail.before_tool_call(ctx)
    agent = SimpleNamespace(card=SimpleNamespace(id='owner-agent', name='owner-agent'),
                            ability_manager=manager, agent_callback_manager=Callbacks())
    state = {}
    session = SimpleNamespace(get_session_id=lambda: 'owner-session',
                              get_state=lambda key, *a, **k: state.get(key), update_state=state.update)
    native = SimpleNamespace(closed=False, engine=SimpleNamespace(binding=SimpleNamespace(
        subject_id=identity.subject_id, host_session_id='owner-session', workspace=str(tmp_path))))
    adapter = SimpleNamespace(_native_execution=native, _session_selected_mcp={'fixture'})
    execution = ResourceExecutionContext('private', identity, 'owner-session', str(tmp_path), 'native')
    live = True
    async def authorize(operation):
        from jiuwenswarm.governance.organization_auth import current_identity
        catalog = authorize_native_catalog(execution, operation, current_identity=current_identity,
            is_current=lambda: live, owns_session=lambda e, a, s: a is agent and s is session)
        if catalog is not None:
            return catalog
        return authorize_instance_mcp(execution, operation, current_identity=current_identity,
            is_current=lambda: live, owns_session=lambda e, a, s: a is agent and s is session)
    bound = SimpleNamespace(active=True, owner=native, tool_authorizer=authorize)
    monkeypatch.setattr('jiuwenswarm.governance.tool_context.current_native_execution_slice', lambda: bound)
    records = []
    with authenticated_scope(principals['alice']):
        cfg = mcp_config.build_mcp_server_config(entry)
        await Runner.resource_mgr.add_mcp_server(cfg)
        try:
            records = install_instance_mcp_tools(adapter, native, agent, session, cfg)
            async def invoke(name, arguments, principal, selected=session):
                with authenticated_scope(principal), tool_authority_scope(authorize):
                    return await manager.execute(AgentCallbackContext(agent=agent), ToolCall(
                        id='fixture-call', type='function', name='mcp_fixture_' + name,
                        arguments=json.dumps(arguments)), session=selected)
            result = await invoke('add', {'a': 7, 'b': 8}, principals['alice'])
            assert '15' in str(result), str(result)
            result = await invoke('add', {'a': 100, 'b': 23}, principals['bob'])
            assert '123' not in str(result)
            other = SimpleNamespace(get_session_id=lambda: 'foreign', get_state=lambda *a, **k: None)
            result = await invoke('add', {'a': 200, 'b': 34}, principals['alice'], other)
            assert '234' not in str(result)
            with pytest.raises(PermissionError):
                await records[0].call_tool(records[0].executor.card.name, {})
            from openjiuwen.core.foundation.tool import ToolExposure
            from openjiuwen.harness.rails.progressive_tool_rail import (
                ProgressiveToolRail,
            )
            progressive = ProgressiveToolRail(SimpleNamespace(language='en'))
            progressive.init(agent)
            for record in records:
                record.executor.card.exposure = ToolExposure.DEFERRED
                record.card_snapshot = record.executor.card.model_dump(mode='json')
            progressive.finalize_startup(agent)
            async def meta(name, args, principal=principals['alice'], selected=session):
                with authenticated_scope(principal), tool_authority_scope(authorize):
                    return await manager.execute(AgentCallbackContext(agent=agent), ToolCall(
                        id='meta-call', type='function', name=name, arguments=json.dumps(args)), session=selected)
            assert 'mcp_fixture_add' in str(await meta('tool_search', {'query': 'add'}))
            assert '42' in str(await meta('tool_call', {'name': 'mcp_fixture_add', 'args': {'a': 40, 'b': 2}}))
            assert '123' not in str(await meta('tool_call', {'name': 'mcp_fixture_add', 'args': {'a': 100, 'b': 23}}, principals['bob']))
            assert 'mcp_fixture_add' not in str(await meta('tool_search', {'query': 'add'}, selected=other))
            search = Runner.resource_mgr.get_tool(manager.get('tool_search').id, session=None)
            callback = search._search_tools
            search._search_tools = lambda *a: []
            assert 'PERMISSION_DENIED' in str(await meta('tool_search', {'query': 'add'}))
            search._search_tools = callback
            # A late result cannot be delivered after the original Runtime
            # authority changes, even when the transport itself completes.
            delayed = next(record for record in records if record.remote_name == 'delayed')
            original_call = delayed.client.call_tool
            entered = asyncio.Event()
            async def observe_call(*args, **kwargs):
                entered.set()
                return await original_call(*args, **kwargs)
            monkeypatch.setattr(delayed.client, 'call_tool', observe_call)
            pending = asyncio.create_task(invoke('delayed', {}, principals['alice']))
            await asyncio.wait_for(entered.wait(), 5)
            live = False
            result = await asyncio.wait_for(pending, 5)
            assert 'late-private-result' not in str(result)
            live = True
            entered.clear()
            pending = asyncio.create_task(invoke('delayed', {}, principals['alice']))
            await asyncio.wait_for(entered.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            # The original connection still works after cancelling just one call.
            result = await invoke('add', {'a': 40, 'b': 2}, principals['alice'])
            assert '42' in str(result), str(result)
            card = records[0].executor.card
            original_description = card.description
            card.description = 'changed after authorization'
            assert records[0].current() is False
            card.description = original_description
            assert records[0].current() is True
            data = json.loads(path.read_text())
            data.pop('instance_owner')
            path.write_text(json.dumps(data))
            result = await invoke('add', {'a': 300, 'b': 45}, principals['alice'])
            assert '345' not in str(result)
        finally:
            for record in records:
                record.close()
            manager.teardown_tools()
            await Runner.resource_mgr.remove_mcp_server(cfg.server_id)
