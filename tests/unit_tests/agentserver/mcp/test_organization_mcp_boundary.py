"""Organization mode must not consume installation-wide MCP credentials."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from jiuwenswarm.common import mcp_config
from jiuwenswarm.governance.organization_auth import CONFIG_ENV


@pytest.fixture(autouse=True)
def legacy_mode(monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)


@pytest.fixture
def organization(tmp_path, monkeypatch):
    path = tmp_path / 'organization.json'
    path.write_text(json.dumps({'authority': 'fixture', 'signing_key': 'ab' * 32, 'credentials': []}))
    path.chmod(0o600)
    monkeypatch.setenv(CONFIG_ENV, str(path))
    return path


@pytest.fixture
def deep():
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    return interface_deep


def config(transport='sse'):
    return McpServerConfig(server_name='fixture', server_id='fixture',
                           server_path='https://invalid.example/mcp', client_type=transport)


def blocked():
    return Mock(side_effect=AssertionError('unauthorized consumer was reached'))


def test_organization_resolver_denies_before_shared_store(organization, monkeypatch):
    store = blocked()
    monkeypatch.setattr(mcp_config, 'CredentialStore', store)
    with pytest.raises(PermissionError, match='organization-scoped MCP authorization required'):
        mcp_config.build_mcp_credential_resolver('fixture')
    store.assert_not_called()


def test_legacy_resolver_cannot_outlive_organization_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_config, 'CredentialStore', lambda: SimpleNamespace(get_all=lambda _: {'TOKEN': 'synthetic'}))
    monkeypatch.setenv('MCP_FIXTURE_FALLBACK', 'synthetic-env')
    resolver = mcp_config.build_mcp_credential_resolver('fixture')
    assert resolver('TOKEN') == 'synthetic'
    assert resolver('MCP_FIXTURE_FALLBACK') == 'synthetic-env'
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / 'missing.json'))
    for key in ('TOKEN', 'MCP_FIXTURE_FALLBACK'):
        with pytest.raises(PermissionError):
            resolver(key)


@pytest.mark.parametrize('transport', ['stdio', 'sse', 'http', 'streamable-http'])
def test_handcrafted_entry_cannot_bypass_with_literal_or_custom_resolver(organization, transport):
    resolver = blocked()
    entry = {'name': 'fixture', 'transport': transport, 'command': 'unused',
             'url': 'https://invalid.example/mcp', 'headers': {'Authorization': 'synthetic'},
             'env': {'TOKEN': '${TOKEN}'}, 'authority': 'caller-forgery'}
    with pytest.raises(PermissionError):
        mcp_config.build_mcp_server_config(entry, credential_resolver=resolver)
    resolver.assert_not_called()


def test_enabled_assembly_denies_before_inventory_or_credential_resolution(organization, monkeypatch):
    inventory = blocked()
    monkeypatch.setattr(mcp_config, 'extract_enabled_mcp_server_entries', inventory)
    with pytest.raises(PermissionError):
        mcp_config.build_enabled_mcp_server_configs({}, resolve_credentials=True)
    inventory.assert_not_called()


@pytest.mark.asyncio
async def test_probe_denies_even_skill_only_before_inventory(organization, monkeypatch):
    inventory = blocked()
    monkeypatch.setattr(mcp_config, 'get_mcp_server_config', inventory)
    assert await mcp_config.probe_mcp_live_connection('skill-only') == (
        False, 'organization-scoped MCP authorization required')
    inventory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['stdio', 'sse'])
async def test_direct_preflight_denies_before_http_or_stdio_success(organization, monkeypatch, transport):
    import httpx
    http = blocked()
    monkeypatch.setattr(httpx, 'AsyncClient', http)
    assert await mcp_config.preflight_mcp_server_reachable(config(transport)) == (
        False, 'organization-scoped MCP authorization required')
    http.assert_not_called()


@pytest.mark.asyncio
async def test_swallowed_preflight_failure_cannot_reach_connect(tmp_path, monkeypatch):
    import openjiuwen.core.runner as runner
    connect = AsyncMock()
    monkeypatch.setattr(runner, 'Runner', SimpleNamespace(resource_mgr=SimpleNamespace(add_mcp_server=connect)))
    monkeypatch.setattr(mcp_config, 'CredentialStore', lambda: SimpleNamespace(get_all=lambda _: {}))
    monkeypatch.setattr(mcp_config, 'get_mcp_server_config', lambda _: {
        'name': 'fixture', 'transport': 'sse', 'url': 'https://invalid.example/mcp'})

    async def failed_preflight(_):
        monkeypatch.setenv(CONFIG_ENV, str(tmp_path / 'missing.json'))
        raise RuntimeError('legacy preflight failure')

    monkeypatch.setattr(mcp_config, 'preflight_mcp_server_reachable', failed_preflight)
    ok, reason = await mcp_config.probe_mcp_live_connection('fixture')
    assert not ok and reason == 'organization-scoped MCP authorization required'
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_common_and_adapter_prewarm_skip_before_inventory(organization, monkeypatch, deep):
    from jiuwenswarm.server.runtime.mcp import state_store
    inventory = blocked()
    monkeypatch.setattr(state_store, 'list_truly_connected_mcps', inventory)
    await mcp_config.prewarm_connected_mcps()
    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    adapter._start_mcp_prewarm()  # No initialized task/agent fields needed.
    inventory.assert_not_called()
    assert not hasattr(adapter, '_mcp_prewarm_task')


def test_global_token_sync_and_clear_do_not_touch_store_or_environment(organization, monkeypatch, deep):
    from jiuwenswarm.server.runtime.mcp import credential, state_store
    store, inventory = blocked(), blocked()
    monkeypatch.setattr(credential, 'CredentialStore', store)
    monkeypatch.setattr(state_store, 'list_connected_mcps', inventory)
    monkeypatch.setenv('MCP_FIXTURE_TOKEN', 'synthetic-existing')
    assert deep.JiuWenSwarmDeepAdapter._sync_mcp_credentials_environment() is False
    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    adapter._clear_mcp_credentials_environment('fixture')
    assert deep.os.environ['MCP_FIXTURE_TOKEN'] == 'synthetic-existing'
    store.assert_not_called()
    inventory.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_register_denies_handcrafted_config_before_preflight(organization, monkeypatch, deep):
    preflight, connect = AsyncMock(), AsyncMock()
    monkeypatch.setattr(deep, 'preflight_mcp_server_reachable', preflight)
    monkeypatch.setattr(deep, 'Runner', SimpleNamespace(resource_mgr=SimpleNamespace(add_mcp_server=connect)))
    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    with pytest.raises(PermissionError):
        await adapter._register_mcp_server(config(), tag='fixture')
    preflight.assert_not_awaited()
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_rechecks_after_preflight_before_connect(tmp_path, monkeypatch, deep):
    connect = AsyncMock()
    monkeypatch.setattr(deep, 'Runner', SimpleNamespace(resource_mgr=SimpleNamespace(add_mcp_server=connect)))

    async def preflight(_):
        monkeypatch.setenv(CONFIG_ENV, str(tmp_path / 'missing.json'))
        return True, ''

    monkeypatch.setattr(deep, 'preflight_mcp_server_reachable', preflight)
    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    adapter._instance = Mock()
    with pytest.raises(PermissionError):
        await adapter._register_mcp_server(config(), tag='fixture')
    connect.assert_not_awaited()
    adapter._instance.ability_manager.add.assert_not_called()


@pytest.mark.asyncio
async def test_named_register_denies_before_cached_or_skill_only_success(organization, monkeypatch, deep):
    inventory = blocked()
    monkeypatch.setattr(deep, 'get_mcp_server_config', inventory)
    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    with pytest.raises(PermissionError):
        await adapter.register_mcp_by_name('fixture')
    inventory.assert_not_called()


@pytest.mark.asyncio
async def test_selected_reconcile_denies_before_optimistic_child_and_skill_creation(organization, deep):
    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    adapter._get_or_create_session_adapter = AsyncMock()
    with pytest.raises(PermissionError):
        await adapter.reconcile_session_mcp('private-session', ['fixture'])
    adapter._get_or_create_session_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_organization_global_startup_and_reload_skip_mcp_inventory(organization, deep):
    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    adapter._yaml_enabled_mcp_entries = blocked()
    adapter._state_enabled_mcp_entries = blocked()
    await adapter._register_mcp_servers_from_config({})
    await adapter._sync_mcp_servers_for_runtime({})
    adapter._yaml_enabled_mcp_entries.assert_not_called()
    adapter._state_enabled_mcp_entries.assert_not_called()


@pytest.mark.parametrize('operation', ['get_all', 'get_token', '_load'])
def test_shared_credential_store_rejects_before_reading_file(organization, tmp_path, monkeypatch, operation):
    from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
    store = CredentialStore(workspace_dir=tmp_path)
    path = blocked()
    monkeypatch.setattr(store, '_path', path)
    with pytest.raises(PermissionError):
        getattr(store, operation)('fixture', *(['TOKEN'] if operation == 'get_token' else []))
    path.assert_not_called()


def test_direct_placeholder_resolution_cannot_accept_untrusted_custom_store(organization):
    from jiuwenswarm.server.runtime.mcp.credential import resolve_placeholders
    store = SimpleNamespace(get_all=blocked())
    with pytest.raises(PermissionError):
        resolve_placeholders({'name': 'fixture', 'env': {'TOKEN': '${TOKEN}'}}, store)
    store.get_all.assert_not_called()


@pytest.mark.parametrize('operation,args', [
    ('connect_mcp', ('fixture',)),
    ('_connect_cli', ('fixture', 0)),
    ('complete_cli_auth', ('fixture', 0)),
    ('_finalize_cli', ('fixture', None)),
])
def test_registry_connect_paths_deny_before_package_or_cli(organization, monkeypatch, operation, args):
    from jiuwenswarm.server.runtime.mcp import registry, cli_driver
    package, driver = blocked(), blocked()
    monkeypatch.setattr(registry, '_resolve_package', package)
    monkeypatch.setattr(cli_driver, 'CliDriver', driver)
    with pytest.raises(PermissionError):
        getattr(registry, operation)(*args)
    package.assert_not_called()
    driver.assert_not_called()


def test_direct_cli_driver_denies_before_env_fallback_or_runner(organization):
    from jiuwenswarm.server.runtime.mcp.cli_driver import CliDriver
    runner = blocked()
    with pytest.raises(PermissionError):
        CliDriver('fixture', runner=runner)
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_handcrafted_agentserver_precheck_cannot_create_client(organization, monkeypatch):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    factory = blocked()
    monkeypatch.setattr(ToolMgr, '_create_client', factory)
    assert await AgentWebSocketServer._pre_check_mcp_server({
        'name': 'fixture', 'transport': 'sse', 'url': 'https://invalid.example/mcp',
        'headers': {'Authorization': 'synthetic'},
    }) == (False, 'organization-scoped MCP authorization required')
    factory.assert_not_called()


def test_credential_schema_metadata_remains_available(organization):
    from jiuwenswarm.server.runtime.mcp.credential import extract_placeholders
    assert extract_placeholders({'name': 'fixture', 'env': {'TOKEN': '${TOKEN}'}}) == {'TOKEN'}


def test_direct_cli_spawn_consumers_reject_without_constructing_driver(organization, monkeypatch):
    from jiuwenswarm.server.runtime.mcp import cli_driver
    run, popen = blocked(), blocked()
    monkeypatch.setattr(cli_driver.subprocess, 'run', run)
    monkeypatch.setattr(cli_driver.subprocess, 'Popen', popen)
    with pytest.raises(PermissionError):
        cli_driver.default_runner('unused', env={'TOKEN': 'synthetic'})
    driver = object.__new__(cli_driver.CliDriver)
    with pytest.raises(PermissionError):
        driver._start_auth_proc(0, 'unused', '')
    with pytest.raises(PermissionError):
        driver._build_cred_env()
    run.assert_not_called()
    popen.assert_not_called()
