import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.extensions.yuanrong_frontend_client import (
    YuanrongFrontendAgentClient,
    YuanrongAgentApiError,
)
from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
    AgentOSRouterClient,
)


def client(info):
    c = YuanrongFrontendAgentClient(
        frontend_endpoint="http://localhost:8888",
        function_version_urn="",
        require_function_urn=False,
        wait_running_timeout_s=0.015,
        wait_running_interval_s=0.001,
    )
    c.get_agent_info = AsyncMock(return_value=info)
    return c


@pytest.mark.asyncio
async def test_missing_status_requires_explicit_positive_application_probe():
    info = {"instance_id": "owned"}
    c = client(info)
    probe = AsyncMock(return_value=True)
    assert await c.wait_until_running("owned", readiness_probe=probe) is info
    assert "status" not in info
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_remains_strict_on_missing_status():
    with pytest.raises(YuanrongAgentApiError, match="not running"):
        await client({"instance_id": "owned"}).wait_until_running("owned")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info",
    [{"instance_id": "other"}, {}, {"instance_id": "owned", "status": "pending"}],
)
async def test_probe_never_overrides_identity_or_explicit_pending(info):
    probe = AsyncMock(return_value=True)
    with pytest.raises(YuanrongAgentApiError):
        await client(info).wait_until_running("owned", readiness_probe=probe)
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_failure_never_probes():
    probe = AsyncMock(return_value=True)
    with pytest.raises(YuanrongAgentApiError, match="failed"):
        await client({"instance_id": "owned", "status": "failed"}).wait_until_running(
            "owned", readiness_probe=probe
        )
    probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("denied")])
async def test_failed_probe_never_claims_running(failure):
    probe = AsyncMock(
        return_value=failure if failure is False else None,
        side_effect=failure if isinstance(failure, Exception) else None,
    )
    with pytest.raises(YuanrongAgentApiError):
        await client({"instance_id": "owned"}).wait_until_running(
            "owned", readiness_probe=probe
        )


@pytest.mark.asyncio
async def test_cancel_propagates_from_probe():
    with pytest.raises(asyncio.CancelledError):
        await client({"instance_id": "owned"}).wait_until_running(
            "owned", readiness_probe=AsyncMock(side_effect=asyncio.CancelledError)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,enabled,expected",
    [
        ("jiuwenswarm", True, True),
        ("jiuwenswarm", False, False),
        ("3rdagent", True, False),
    ],
)
async def test_router_probes_only_opted_in_builtin(kind, enabled, expected):
    r = object.__new__(AgentOSRouterClient)
    r._builtin_ws_readiness = enabled
    waiter = AsyncMock(return_value={"instance_id": "owned"})
    r._yuanrong = SimpleNamespace(wait_until_running=waiter)
    await r._wait_yuanrong_running("owned", agent_type=kind)
    assert ("readiness_probe" in waiter.call_args.kwargs) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,ready",
    [
        (
            '{"type":"event","event":"connection.ack","payload":{"status":"ready"}}',
            True,
        ),
        (
            '{"type":"event","event":"connection.ack","payload":{"status":"starting"}}',
            False,
        ),
        ('{"type":"event","event":"other","payload":{"status":"ready"}}', False),
    ],
)
async def test_probe_validates_application_ack(raw, ready):
    r = object.__new__(AgentOSRouterClient)
    r._yuanrong = SimpleNamespace(
        frontend_endpoint="http://127.0.0.1:18888", agent_namespace="default"
    )
    context = AsyncMock()
    context.__aenter__.return_value.recv = AsyncMock(return_value=raw)
    with patch("websockets.connect", return_value=context) as connect:
        assert await r._probe_builtin_readiness("owned") is ready
        assert "instance=owned" in connect.call_args.args[0]
