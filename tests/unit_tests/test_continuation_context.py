"""Plaintext seed consumer; synthetic facts, no model, network or source restore."""

import asyncio
import copy
import hashlib
import pickle
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.governance.continuation import (
    ContinuationInput,
    ContinuationMessage,
    ContinuationProof,
    ContinuationSeed,
)
from jiuwenswarm.governance.continuation_context import (
    ContinuationContext,
    ContinuationContextDenied,
    create_continuation_context,
    validate_continuation_context,
)
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionHistoryRange

BOB = TrustedIdentity("bob", "subject-bob", "organization")
ALICE = TrustedIdentity("alice", "subject-alice", "organization")


@pytest.fixture
def seed():
    request = ContinuationInput(
        "source", "share", 1, "token", "target-project", "native"
    )
    history = SessionHistoryRange(
        "source", hashlib.sha256(b"source\0").hexdigest(), 1, 2, 100, 0, 100
    )
    proof = ContinuationProof(
        request, BOB, ALICE, 1, 2, 3, history, None, None, ALICE, None, None, 4
    )
    return ContinuationSeed(
        proof,
        (
            ContinuationMessage("user", "source-question"),
            ContinuationMessage("assistant", "source-answer"),
        ),
    )


def context(seed, **kwargs):
    return create_continuation_context(
        **dict(
            {
                "session_id": "target",
                "request_id": "current",
                "identity": BOB,
                "seed": seed,
                "check": lambda: None,
            },
            **kwargs,
        )
    )


def test_host_handle_is_opaque_copy_stable_and_not_restorable(seed):
    value = context(seed)
    assert copy.copy(value) is value
    assert copy.deepcopy({"opaque": value})["opaque"] is value
    assert "source-question" not in repr(value) and "subject-bob" not in repr(value)
    with pytest.raises(TypeError):
        pickle.dumps(value)
    with pytest.raises(TypeError):
        ContinuationContext()
    with pytest.raises(FrozenInstanceError):
        value.request_id = "other"
    for forged in ({"seed": seed}, SimpleNamespace(validate=lambda *_: None), None):
        with pytest.raises(ContinuationContextDenied):
            validate_continuation_context(forged, "target", "current")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": "source"},
        {"identity": ALICE},
        {"identity": {"actor_id": "bob"}},
        {"request_id": ""},
    ],
)
def test_invalid_host_context_rejected(seed, kwargs):
    with pytest.raises(ContinuationContextDenied):
        context(seed, **kwargs)


def test_exact_request_identity_and_live_check(seed):
    current = [BOB]

    def check():
        if current[0] != BOB:
            raise ValueError("sensitive-checker-detail")

    value = context(seed, check=check)
    for sid, rid in [("other", "current"), ("target", "other")]:
        with pytest.raises(ContinuationContextDenied):
            value.validate(sid, rid)
    current[0] = TrustedIdentity("bob", "other-subject", "organization")
    with pytest.raises(ContinuationContextDenied, match="not current") as error:
        value.validate("target", "current")
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("result", [False, True, object()])
def test_checker_requires_none_success(seed, result):
    with pytest.raises(ContinuationContextDenied):
        context(seed, check=lambda: result)


def test_async_checker_denied(seed):
    async def check():
        pass

    with pytest.raises(ContinuationContextDenied):
        context(seed, check=check)


@pytest.fixture
def warmup(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service as ops

    live = SimpleNamespace(get_session_id=lambda: "target")
    pool = []
    engine = SimpleNamespace(get_context=lambda **_: pool[0] if pool else None)

    async def create(**kwargs):
        pool.append(kwargs["history_messages"])

    async def clear(**kwargs):
        pool.clear()

    engine.create_context = AsyncMock(side_effect=create)
    engine.clear_context = AsyncMock(side_effect=clear)
    deep = SimpleNamespace(
        _loop_session=live,
        react_agent=SimpleNamespace(
            context_engine=engine, _config=SimpleNamespace(context_processors=[])
        ),
    )
    records = []
    monkeypatch.setattr(
        ops, "history_exists", lambda sid: sid == "target" and bool(records)
    )

    def load(sid):
        assert sid == "target", "must never read source history"
        return records[:]

    monkeypatch.setattr(ops, "load_history_records", load)
    for name in [
        "_side_parent_for_session",
        "_fork_source_for_session",
        "write_history_records",
    ]:
        monkeypatch.setattr(
            ops,
            name,
            Mock(side_effect=AssertionError("no source state or history writes")),
        )

    async def run(handle, request="current"):
        return await ops.warmup_session_context(
            deep_agent=deep,
            session_id="target",
            history_before_request_id=request,
            continuation_context=handle,
        )

    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_first_empty_target_and_next_turn_no_repeat(seed, warmup):
    value = context(seed)
    assert await warmup.run(value)
    assert [(m.role, m.content) for m in warmup.pool[0]] == [
        ("user", "source-question"),
        ("assistant", "source-answer"),
    ]
    assert await warmup.run(context(seed, request_id="next"), "next")
    assert warmup.engine.create_context.await_count == 1
    warmup.pool.clear()  # idle eviction/rebuild, not a source checkpoint restore
    assert await warmup.run(context(seed, request_id="third"), "third")
    assert len(warmup.pool[0]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("current_visible", [False, True])
async def test_seed_then_own_history_excludes_current_and_later(
    seed, warmup, current_visible
):
    warmup.records.extend(
        [
            {"role": "user", "content": "bob-old", "request_id": "old"},
            {
                "role": "assistant",
                "content": "bob-answer",
                "event_type": "chat.final",
                "request_id": "old",
            },
        ]
    )
    if current_visible:
        warmup.records.extend(
            [
                {"role": "user", "content": "current-query", "request_id": "current"},
                {"role": "user", "content": "later", "request_id": "later"},
            ]
        )
    assert await warmup.run(context(seed))
    assert [m.content for m in warmup.pool[0]] == [
        "source-question",
        "source-answer",
        "bob-old",
        "bob-answer",
    ]


@pytest.mark.asyncio
async def test_cached_context_rechecks_without_history_read(seed, warmup, monkeypatch):
    live = [True]

    def check():
        if not live[0]:
            raise ValueError("revoked")

    handle = context(seed, check=check)
    await warmup.run(handle)
    live[0] = False
    with pytest.raises(ContinuationContextDenied):
        await warmup.run(handle)
    assert warmup.engine.create_context.await_count == 1


@pytest.mark.asyncio
async def test_no_temporary_session_fallback(seed, warmup):
    warmup.deep._loop_session = None
    with pytest.raises(ContinuationContextDenied, match="live continuation"):
        await warmup.run(context(seed))
    warmup.engine.create_context.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revoke", "error", "cancel"])
async def test_await_failure_cleans_new_context_and_preserves_cancel(
    seed, warmup, failure
):
    live = [True]

    def check():
        if not live[0]:
            raise ValueError("revoked")

    async def create(**kwargs):
        warmup.pool.append(kwargs["history_messages"])
        await asyncio.sleep(0)
        if failure == "revoke":
            live[0] = False
        elif failure == "cancel":
            raise asyncio.CancelledError()
        else:
            raise ValueError("sensitive-build-detail")

    warmup.engine.create_context.side_effect = create
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ContinuationContextDenied
    ):
        await warmup.run(context(seed, check=check))
    assert warmup.pool == []
    warmup.engine.clear_context.assert_awaited_once_with(
        context_id="default_context_id", session_id="target"
    )


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mask_cancel(seed, warmup):
    warmup.engine.create_context.side_effect = asyncio.CancelledError()
    warmup.engine.clear_context.side_effect = RuntimeError("sensitive-cleanup-detail")
    with pytest.raises(asyncio.CancelledError):
        await warmup.run(context(seed))


@pytest.mark.asyncio
async def test_history_error_denied_without_seed_only_fallback(
    seed, warmup, monkeypatch
):
    warmup.records.append({"role": "user"})
    monkeypatch.setattr(
        warmup.ops, "load_history_records", Mock(side_effect=OSError("secret-path"))
    )
    with pytest.raises(ContinuationContextDenied, match="history unavailable"):
        await warmup.run(context(seed))
    warmup.engine.create_context.assert_not_awaited()


@pytest.fixture
def adapter(seed, warmup, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    from jiuwenswarm.common.schema.agent import AgentRequest

    root = JiuWenSwarmDeepAdapter()
    child = root._new_session_scoped_adapter("target")
    child._instance = warmup.deep
    child.create_instance = AsyncMock()
    child.restore_skill_retrieval_session = Mock()
    child.persist_skill_retrieval_session_profile = Mock()
    child.cleanup = AsyncMock()
    execution = SimpleNamespace(
        enable_turn_outputs=Mock(), request_id_for_turn=lambda _: None
    )

    async def start(**kwargs):
        child._native_execution = execution
        return execution

    child.start_native_interaction = AsyncMock(side_effect=start)
    root._new_session_scoped_adapter = Mock(return_value=child)
    root._load_skill_retrieval_session_profile = Mock(return_value=None)
    root._reload_session_adapter_if_stale = AsyncMock()
    bound = SimpleNamespace(
        spec=SimpleNamespace(provider_id="native"),
        binding=SimpleNamespace(
            host_session_id="target", subject_id=BOB.subject_id, workspace="/tmp"
        ),
    )

    def request(rid="current", handle=None):
        result = AgentRequest(
            request_id=rid, channel_id="web", session_id="target", params={}
        )
        result._bound_execution = bound
        result._execution_source = object()
        result._execution_bindings = object()
        result._continuation_context = handle or context(seed, request_id=rid)
        return result

    req = request()
    root.select_execution_for_request(req)

    async def get(rid="current"):
        return await root._get_or_create_session_adapter(
            "target", history_before_request_id=rid
        )

    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_route_seed_warmup_publishes_only_after_success_and_reuses_context(
    adapter, warmup
):
    original = warmup.engine.create_context.side_effect

    async def create(**kwargs):
        assert "target" not in adapter.root._session_adapters
        await original(**kwargs)

    warmup.engine.create_context.side_effect = create
    assert await adapter.get() is adapter.child
    assert adapter.root._session_adapters["target"] is adapter.child
    adapter.root.select_execution_for_request(adapter.request("next"))
    assert await adapter.get("next") is adapter.child
    assert warmup.engine.create_context.await_count == 1


@pytest.mark.asyncio
async def test_route_does_not_borrow_next_request_before_allocation(adapter):
    adapter.root.select_execution_for_request(adapter.request("next"))
    with pytest.raises(ContinuationContextDenied):
        await adapter.get()
    adapter.root._new_session_scoped_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_route_switch_during_creation_cleans_owned_child(adapter, warmup):
    async def create(*args, **kwargs):
        adapter.root.select_execution_for_request(adapter.request("next"))
        await asyncio.sleep(0)

    adapter.child.create_instance.side_effect = create
    with pytest.raises(ContinuationContextDenied):
        await adapter.get()
    adapter.child.start_native_interaction.assert_not_awaited()
    adapter.child.cleanup.assert_awaited_once()
    assert "target" not in adapter.root._session_adapters
    warmup.engine.create_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_governed_warmup_error_propagates_and_does_not_publish(adapter, warmup):
    warmup.engine.create_context.side_effect = ValueError("sensitive")
    adapter.child.cleanup.side_effect = RuntimeError("cleanup-sensitive")
    with pytest.raises(ContinuationContextDenied):
        await adapter.get()
    assert "target" not in adapter.root._session_adapters
    adapter.child.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_governed_creation_cancel_propagates_and_cleans(adapter):
    adapter.child.create_instance.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await adapter.get()
    adapter.child.cleanup.assert_awaited_once()
    assert "target" not in adapter.root._session_adapters


def test_route_rejects_forged_private_handle_and_downgrade(adapter):
    req = adapter.request()
    req._continuation_context = {"seed": "wire-data"}
    with pytest.raises(ContinuationContextDenied):
        adapter.root.select_execution_for_request(req)
    del req._continuation_context
    with pytest.raises(PermissionError, match="cannot be removed"):
        adapter.root.select_execution_for_request(req)


@pytest.mark.asyncio
async def test_cached_route_recheck_after_reload_prevents_use(adapter, warmup):
    await adapter.get()

    async def reload(*args, **kwargs):
        adapter.root.select_execution_for_request(adapter.request("next"))

    adapter.root._reload_session_adapter_if_stale.side_effect = reload
    with pytest.raises(ContinuationContextDenied):
        await adapter.get()
    assert warmup.engine.create_context.await_count == 1


def test_primary_model_uses_only_host_execution_and_exact_request(monkeypatch):
    import sys
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.governance.resources import ResourceAccessDenied
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    root = JiuWenSwarmDeepAdapter()
    # Main Runtime's live helper is an independently integrated package. This
    # test verifies adapter dispatch only; it does not emulate its authority.
    execution = SimpleNamespace(
        session_id="target",
        request_id="current",
        build_model=Mock(return_value=object()),
    )
    require = Mock(
        side_effect=lambda value: (
            execution
            if value is execution
            else (_ for _ in ()).throw(ResourceAccessDenied("invalid execution"))
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.runtime.continuation_execution",
        SimpleNamespace(require_continuation_execution=require),
    )
    root._requested_model_name = Mock(
        side_effect=AssertionError("no legacy model selection")
    )
    root._request_scoped_login_model = Mock(
        side_effect=AssertionError("no login credential fallback")
    )
    root._resolve_model_by_name = Mock(side_effect=AssertionError("no cache fallback"))
    request = AgentRequest(
        request_id="current", channel_id="web", session_id="target", params={}
    )
    request._continuation_execution = execution
    assert (
        root._resolve_model_for_request(request) is execution.build_model.return_value
    )
    request.request_id = "different"
    with pytest.raises(ResourceAccessDenied, match="request changed"):
        root._resolve_model_for_request(request)
    assert execution.build_model.call_count == 1
    request._continuation_execution = {}
    with pytest.raises(ResourceAccessDenied):
        root._resolve_model_for_request(request)


def test_model_builder_forwards_optional_exact_entry_fingerprint(monkeypatch):
    from jiuwenswarm.governance import model_consumer
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    kwargs = Mock(return_value={})
    monkeypatch.setattr(model_consumer, "runtime_model_kwargs", kwargs)
    constructor = Mock()
    monkeypatch.setattr(interface_deep, "Model", constructor)
    client = {
        "model_name": "chat",
        "client_provider": "OpenAI",
        "api_key": "synthetic",
        "api_base": "https://example.invalid/v1",
    }
    interface_deep.build_model_from_entry(client, {})
    assert "model_entry_fingerprint" not in kwargs.call_args.kwargs
    interface_deep.build_model_from_entry(client, {}, model_entry_fingerprint="a" * 64)
    assert kwargs.call_args.kwargs["model_entry_fingerprint"] == "a" * 64
    assert kwargs.call_args.kwargs["binding_config"] is client


@pytest.mark.asyncio
async def test_real_context_engine_seed_rebuild_once_and_no_source_checkpoint(
    seed, warmup
):
    from openjiuwen.core.context_engine.context_engine import ContextEngine
    from openjiuwen.core.context_engine.schema.config import ContextEngineConfig

    engine = ContextEngine(
        ContextEngineConfig(enable_openrouter_model_context_window_tokens=False)
    )
    warmup.deep.react_agent.context_engine = engine
    assert await warmup.run(context(seed))
    actual = engine.get_context(session_id="target")
    assert [m.content for m in actual.get_messages()] == [
        "source-question",
        "source-answer",
    ]
    assert await warmup.run(context(seed, request_id="second"), "second")
    assert engine.get_context(session_id="target") is actual
    assert len(actual.get_messages()) == 2
    await engine.clear_context(session_id="target")
    warmup.records.append(
        {"role": "user", "request_id": "old", "content": "bob-history"}
    )
    assert await warmup.run(context(seed, request_id="third"), "third")
    rebuilt = engine.get_context(session_id="target")
    assert rebuilt is not actual
    assert [m.content for m in rebuilt.get_messages()] == [
        "source-question",
        "source-answer",
        "bob-history",
    ]
    await engine.clear_context(session_id="target")


@pytest.mark.asyncio
async def test_governed_history_boundary_is_required(seed, warmup):
    with pytest.raises(ContinuationContextDenied, match="boundary required"):
        await warmup.run(context(seed), request=None)
    warmup.engine.create_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_from_live_checker_is_preserved(seed, warmup):
    alive = [True]

    def check():
        if not alive[0]:
            raise asyncio.CancelledError()

    handle = context(seed, check=check)
    alive[0] = False
    with pytest.raises(asyncio.CancelledError):
        await warmup.run(handle)
    warmup.engine.create_context.assert_not_awaited()
