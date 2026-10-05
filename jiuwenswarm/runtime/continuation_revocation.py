"""Retained stop-only ownership for the original private continuation Session.

No incoming request can construct or borrow this capability. Current access is
rechecked independently; once it is lost, cleanup uses the originally captured
owner and Binding without requiring the revoked grant again.
"""

from __future__ import annotations

from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.runtime.continuation_targets import ContinuationTargets
from jiuwenswarm.server.runtime.session.continuation_publication import read_approval


class ContinuationRevocation:
    def __init__(self, runtime, execution):
        execution.check()
        self.runtime = runtime
        self.host = runtime._organization_session_host
        self.session_id = execution.session_id
        self.identity = execution._identity
        self.approval = execution._approval
        self.generation = execution._generation
        self.stamp = self.host.cleanup_owner_stamp(self.session_id, self.identity)
        self.binding = self.host.cleanup_owner_binding(
            self.session_id, self.identity, expected_stamp=self.stamp
        )
        self.channel = dict(self.binding)["channel_id"]
        self.owner = runtime._agent_manager.get_agent_for_session_nowait(
            self.channel, self.session_id
        )
        self._owner_slots = self._slots(self.owner) if self.owner is not None else ()
        # Do not turn a connection's expiring token into authority to stop other
        # connections. Persisted source/project/resource grants apply to this
        # complete immutable continuation Binding, across its ordinary Turns.
        self.targets = ContinuationTargets(
            runtime, identity_resolver=lambda _: self.identity
        )
        self.target = self.targets.select(execution.target.request)
        if self.target != execution.target:
            raise ResourceAccessDenied("original continuation target changed")
        self.scope = (
            self.session_id,
            self.generation,
            self.identity,
            self.stamp,
            self.binding,
            self.approval,
        )
        self.check_authority()

    def _slots(self, owner):
        from jiuwenswarm.server.runtime.agent_manager import _normalize_channel_id

        return tuple(
            (key, item)
            for key, item in getattr(self.runtime._agent_manager, "agents", {})
            .get(_normalize_channel_id(self.channel), {})
            .items()
            if item is owner
        )

    def bind_owner(self, execution, owner):
        # Called only by the original Runtime producer after admitted preparation,
        # before invoking the returned agent. Never discover a replacement at stop.
        execution.check()
        self.check_owner()
        if (
            execution._runtime is not self.runtime
            or execution._generation != self.generation
            or execution._approval != self.approval
            or owner is None
        ):
            raise ResourceAccessDenied("original continuation producer required")
        if self.owner is None:
            slots = self._slots(owner)
            if len(slots) != 1:
                raise ResourceAccessDenied(
                    "original cached continuation owner required"
                )
            self.owner, self._owner_slots = owner, slots
        elif owner is not self.owner or self._slots(owner) != self._owner_slots:
            raise ResourceAccessDenied("original cached continuation owner replaced")

    def check_cached_owner(self):
        if self.owner is None:
            if (
                self.runtime._agent_manager.get_agent_for_session_nowait(
                    self.channel, self.session_id
                )
                is not None
            ):
                raise ResourceAccessDenied(
                    "uncaptured continuation runtime cannot be released"
                )
        elif not self._owner_slots or self._slots(self.owner) != self._owner_slots:
            raise ResourceAccessDenied(
                "original cached execution changed during revoke"
            )

    def check_owner(self):
        current = self.runtime._session_coordinator.snapshot_session(self.session_id)
        if (
            self.runtime._closed
            or self.runtime._organization_session_host is not self.host
            or current is None
            or current.generation != self.generation
            or self.host._cleanup_owner_binding(
                self.session_id,
                self.identity,
                expected_stamp=self.stamp,
                require_known_actor=False,
            )
            != self.binding
        ):
            raise ResourceAccessDenied("original continuation cleanup owner changed")

    def check_authority(self):
        self.check_owner()
        try:
            if (
                read_approval(self.host, self.session_id, self.identity)
                != self.approval
            ):
                raise ResourceAccessDenied("original continuation approval changed")
            self.targets.revalidate(self.target)
        except Exception:
            raise ResourceAccessDenied(
                "original continuation authority revoked"
            ) from None

    async def release(self):
        self.check_owner()
        manager = self.runtime._agent_manager

        def original():
            self.check_owner()
            self.check_cached_owner()

        original()
        await manager.release_subagent_runtime_for_session(
            channel_id=self.channel,
            session_id=self.session_id,
            reason="continuation_revoked",
        )
        original()
        await manager.stop_existing_session_runtime(
            channel_id=self.channel, session_id=self.session_id
        )
        original()
        await manager.cleanup_session_runtime(
            channel_id=self.channel, session_id=self.session_id
        )
        self.check_owner()
        await self.runtime._forget_agent_execution_owner(
            channel_id=self.channel, session_id=self.session_id
        )
        self.check_owner()
        await self.runtime._clear_pending_interaction(self.session_id)
        self.check_owner()
