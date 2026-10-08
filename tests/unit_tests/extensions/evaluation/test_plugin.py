"""Actual registry/loader and isolated application service composition."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.evaluation.backend.adapters.store import CatalogError
from jiuwenswarm.extensions.evaluation.extension import EvaluationApplicationPlugin
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.governance.contracts import TrustedIdentity
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

ALICE = TrustedIdentity("alice", "alice", "test")
BOB = TrustedIdentity("bob", "bob", "test")


@pytest.mark.asyncio
async def test_plugin_load_and_instance_services(tmp_path):
    registry = ExtensionRegistry(
        callback_framework=AsyncCallbackFramework(), config={}, logger=None
    )
    root = Path(__file__).parents[4] / "jiuwenswarm/extensions/evaluation"
    assert await ExtensionLoader(registry).load_extension(root)
    registry.require_capabilities({"evaluation.application": ">=1,<2"})
    plugin = registry.get_application_plugin("evaluation-experiments")
    first = plugin.compose(runtime=SimpleNamespace(), data_root=tmp_path / "first")
    second = plugin.compose(runtime=SimpleNamespace(), data_root=tmp_path / "second")
    try:
        await first.call("evaluation.examples", {}, ALICE)
        first_catalog = await first.call("evaluation.catalog", {}, ALICE)
        assert len(first_catalog["tasks"]) == 3
        assert len(first_catalog["datasets"]) == 1
        assert not (await first.call("evaluation.catalog", {}, BOB))["tasks"]
        assert not (await second.call("evaluation.catalog", {}, ALICE))["tasks"]
        await first.call("evaluation.examples", {}, ALICE)
        assert len((await first.call("evaluation.catalog", {}, ALICE))["tasks"]) == 3
        with pytest.raises(CatalogError, match="INVALID_PARAMS"):
            await first.call("evaluation.catalog", {"owner": "alice"}, BOB)
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_untrusted_identity_closed_service_and_bad_input(tmp_path):
    service = EvaluationApplicationPlugin().compose(runtime=None, data_root=tmp_path)
    try:
        with pytest.raises(PermissionError):
            await service.call("evaluation.catalog", {}, None)
        with pytest.raises(CatalogError, match="INVALID_REVISION"):
            await service.call(
                "evaluation.task.save", {"expected_revision": True}, ALICE
            )
        with pytest.raises(CatalogError, match="EVALUATION_UNAVAILABLE"):
            await service.call("evaluation.not_registered", {}, ALICE)
    finally:
        await service.close()
    with pytest.raises(CatalogError, match="EVALUATION_UNAVAILABLE"):
        await service.call("evaluation.catalog", {}, ALICE)


@pytest.mark.asyncio
async def test_empty_list_survives_real_wire_codec(tmp_path):
    from jiuwenswarm.common.e2a.wire_codec import (
        encode_agent_response_for_wire,
        parse_agent_server_wire_unary,
    )
    from jiuwenswarm.common.schema.agent import AgentResponse

    service = EvaluationApplicationPlugin().compose(runtime=None, data_root=tmp_path)
    try:
        payload = await service.call("evaluation.experiment.list", {}, ALICE)
        response = AgentResponse(
            request_id="request", channel_id="web", ok=True, payload=payload
        )
        decoded = parse_agent_server_wire_unary(
            encode_agent_response_for_wire(response, response_id="request")
        )
        assert decoded.payload == {"experiments": []}
    finally:
        await service.close()
