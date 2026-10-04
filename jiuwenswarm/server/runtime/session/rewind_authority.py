"""Request-local authority for existing Single rewind consumers, never wire data.

Only owner permits are accepted. This synchronous guard may run under the
existing history/metadata file locks: owner/provenance checks acquire the
original reentrant sidecar lock and read atomic metadata/lifecycle records;
they do not read history, acquire lifecycle locks, or wait for writer queues.
Do not substitute a generic share/history delivery callback here.
"""

from __future__ import annotations

import json
from contextvars import copy_context
from dataclasses import dataclass, field

from jiuwenswarm.governance.session_boundary import SessionRequestPermit
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.runtime.session.model import RuntimeSessionState

REWIND_METHODS = frozenset(
    {
        "session.rewind",
        "session.rewind_and_restore",
        "session.rewind_compact",
        "session.rewind_context",
    }
)


def _wire(request):
    return (
        request.request_id,
        request.channel_id,
        request.session_id,
        request.req_method.value if request.req_method else "",
        json.dumps(
            request.params or {}, sort_keys=True, separators=(",", ":"), allow_nan=False
        ),
    )


def _snapshot(runtime, sid):
    snapshot = runtime._session_coordinator.snapshot_session(sid)
    if snapshot is not None and snapshot.state in {
        RuntimeSessionState.QUIESCING,
        RuntimeSessionState.CLOSED,
    }:
        raise SessionSharingDenied("rewind Session is closing")
    return snapshot


@dataclass(frozen=True, repr=False)
class RewindAuthority:
    session_id: str
    channel_id: str
    _request: object = field(repr=False)
    _wire_facts: tuple = field(repr=False)
    _permit: SessionRequestPermit = field(repr=False)
    _context: object = field(repr=False)
    _stamp: tuple = field(repr=False)
    _binding: tuple = field(repr=False)
    _runtime: object = field(repr=False)
    _generation: int | None = field(repr=False)
    _executions: frozenset = field(repr=False)
    _manager: object = field(repr=False)
    _cache_key: str = field(repr=False)
    _agent: object = field(repr=False)
    _adapter: object = field(repr=False)
    _child: object = field(repr=False)
    _deep: object = field(repr=False)
    _react: object = field(repr=False)
    _engine: object = field(repr=False)
    _live_session: object = field(repr=False)
    _initial_context: object = field(repr=False)

    def __call__(self):
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            resolve_live_agent_session,
        )

        if (
            self._runtime._closed
            or _wire(self._request) != self._wire_facts
            or not self._context.copy().run(self._permit.revalidate)
            or self._permit.host.cleanup_owner_binding(
                self.session_id, self._permit.identity, expected_stamp=self._stamp
            )
            != self._binding
        ):
            raise SessionSharingDenied("original rewind authority changed")
        decision = self._permit.host._storage.authorize(
            dict(self._binding)["project_id"], self._permit.identity.actor_id, "write"
        )
        if (
            decision.allowed is not True
            or decision.action != "write"
            or decision.project_id != dict(self._binding)["project_id"]
            or decision.actor_id != self._permit.identity.actor_id
            or decision.revision < 1
            or decision.reason in {"legacy_default", "legacy_unmanaged", "orphan_owner"}
        ):
            raise SessionSharingDenied("current project write permission required")
        current = _snapshot(self._runtime, self.session_id)
        if (
            (current.generation if current else None) != self._generation
            or current is not None
            and any(
                not item.state.terminal and item.execution_id not in self._executions
                for item in current.executions
            )
        ):
            raise SessionSharingDenied("rewind cannot select a newer execution")
        if (
            self._manager.agents.get(self.channel_id, {}).get(self._cache_key)
            is not self._agent
            or getattr(self._agent, "_adapter", None) is not self._adapter
            or self._cached_child() is not self._child
            or getattr(self._child, "_instance", None) is not self._deep
            or self._deep.react_agent is not self._react
            or self._react.context_engine is not self._engine
            or resolve_live_agent_session(self._deep, self.session_id)
            is not self._live_session
        ):
            raise SessionSharingDenied("original rewind runtime objects changed")

    def _cached_child(self):
        if getattr(self._adapter, "_is_session_scoped_adapter", False):
            return (
                self._adapter
                if getattr(self._adapter, "_parent_session_id", None) == self.session_id
                else None
            )
        lookup = getattr(self._adapter, "_get_cached_session_adapter", None)
        return lookup(self.session_id) if callable(lookup) else None

    def check_initial_context(self):
        self()
        if (
            self._engine.get_context(session_id=self.session_id)
            is not self._initial_context
        ):
            raise SessionSharingDenied(
                "original rewind context changed before consumption"
            )

    def resolve(self):
        self.check_initial_context()
        return self._deep, self._react


def capture_rewind_authority(server, request, permit):
    """Capture before the host extension await; no default Agent or warmup."""
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        resolve_live_agent_session,
    )

    if (
        type(permit) is not SessionRequestPermit
        or permit.method not in REWIND_METHODS
        or not permit.revalidate()
        or permit.cleanup is not None
        or permit.share is not None
        or permit.inventory_revision is not None
        or permit.continuation_input is not None
        or permit.continuation_options is not None
        or permit.deletion_receipt is not None
    ):
        raise SessionSharingDenied("original owner rewind permit required")
    sid = (request.params or {}).get("session_id") or request.session_id
    if len(permit.owners) != 1 or permit.owners[0][0] != sid:
        raise SessionSharingDenied("exact rewind Session required")
    stamp = permit.host.cleanup_owner_stamp(sid, permit.identity)
    binding = permit.host.cleanup_owner_binding(
        sid, permit.identity, expected_stamp=stamp
    )
    channel = dict(binding)["channel_id"]
    runtime = server._execution_runtime()
    manager = runtime._agent_manager
    owners = [
        (key, agent)
        for key, agent in tuple(manager.agents.get(channel, {}).items())
        if callable(getattr(agent, "has_session_runtime", None))
        and agent.has_session_runtime(sid)
    ]
    if len(owners) != 1:
        raise SessionSharingDenied("one existing Session owner required for rewind")
    key, agent = owners[0]
    adapter = getattr(agent, "_adapter", None)
    if getattr(adapter, "_is_session_scoped_adapter", False):
        child = adapter if getattr(adapter, "_parent_session_id", None) == sid else None
    else:
        lookup = getattr(adapter, "_get_cached_session_adapter", None)
        child = lookup(sid) if callable(lookup) else None
    deep = getattr(child, "_instance", None)
    react = getattr(deep, "react_agent", None)
    engine = getattr(react, "context_engine", None)
    if child is None or deep is None or react is None or engine is None:
        raise SessionSharingDenied("existing Session context required for rewind")
    snapshot = _snapshot(runtime, sid)
    result = RewindAuthority(
        sid,
        channel,
        request,
        _wire(request),
        permit,
        copy_context(),
        stamp,
        binding,
        runtime,
        snapshot.generation if snapshot else None,
        frozenset(
            item.execution_id for item in snapshot.executions if not item.state.terminal
        )
        if snapshot
        else frozenset(),
        manager,
        key,
        agent,
        adapter,
        child,
        deep,
        react,
        engine,
        resolve_live_agent_session(deep, sid),
        engine.get_context(session_id=sid),
    )
    result()
    return result
