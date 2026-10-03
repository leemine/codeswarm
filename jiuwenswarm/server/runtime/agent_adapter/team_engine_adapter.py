# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""External member construction with the original product Team stream owner."""
from __future__ import annotations

from contextlib import aclosing
from copy import copy
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.surface import canonical_surface_mode
from jiuwenswarm.runtime.harness.team_execution import ExternalTeamMemberFactory


class ExternalTeamAgentAdapter:
    """Thin Team facade; it owns no Provider, event cursor or scheduler.

    Global product admission stays gated until the remaining Team capabilities
    and interaction/history acceptance are complete.
    """

    def __init__(self, route: AdmittedExecutionRoute) -> None:
        surface = route.surface
        factory = ExternalTeamMemberFactory(route, team_name=surface.identity.team_name if surface else '')
        self._route = route
        self._heartbeat_service = None
        self._created = False
        self._goal_runtime = None
        self._history_release = {}
        self._manifest = factory.surface.ui_capability_manifest

    @property
    def supports_goal_execution(self):
        return self._goal_runtime is not None

    @property
    def route(self):
        return self._route

    @property
    def ui_capability_manifest(self):
        return self._manifest

    def bind_route(self, route: AdmittedExecutionRoute) -> None:
        if (route.cache_identity != self._route.cache_identity
                or route.bound.binding is not self._route.bound.binding
                or route.trusted_subject_id != self._route.trusted_subject_id
                or (route.trusted_subject_id is not None
                    and route.trusted_subject_id != route.bound.binding.subject_id)
                or route.surface is None
                or route.surface.identity != self._route.surface.identity
                or route.runtime_paths != self._route.runtime_paths):
            raise ValueError('External Team adapter binding changed')
        self._route.surface.validate_mode(route.surface.initial_mode, require_policy=False, topology='team')
        if route.surface.initial_mode != self._route.surface.initial_mode:
            raise ValueError('External Team execution state changed')

    def select_execution_for_request(self, request: AgentRequest) -> None:
        route = getattr(request, '_execution_route', None)
        if not isinstance(route, AdmittedExecutionRoute):
            raise ValueError('External Team request has no admitted execution route')
        self.bind_route(route)
        binding = self._route.bound.binding
        if (request.session_id != binding.host_session_id
                or request.channel_id != self._route.channel_id
                or (self._route.trusted_subject_id is None
                    and request.user_id and request.user_id != binding.subject_id)):
            raise ValueError('External Team request identity changed')
        params = request.params if isinstance(request.params, dict) else {}
        self._route.surface.validate_mode(
            params.get('mode') or self._route.surface.initial_mode, require_policy=False, topology='team',
        )
        for declaration in (params, request.metadata or {}):
            if declaration.get('mode') and canonical_surface_mode({
                'mode': declaration['mode'], 'work_mode': self._route.surface.identity.work_mode,
            }) != self._route.surface.initial_mode:
                raise ValueError('External Team execution state changed')
        if params.get('work_mode') not in (None, self._route.surface.identity.work_mode):
            raise ValueError('External Team Work/Code Surface changed')
        if params.get('execution_profile_id') not in (None, self._route.surface.identity.execution_profile_id):
            raise ValueError('External Team profile changed')
        for key in ('project_dir', 'workspace_dir'):
            if params.get(key) and str(Path(params[key]).resolve()) != binding.workspace:
                raise ValueError('External Team workspace changed')
        if params.get('cwd') and Path(params['cwd']).resolve() != self._route.runtime_paths.cwd.resolve():
            raise ValueError('External Team member cwd changed')

    async def create_instance(self, config: dict[str, Any] | None = None, *,
                              mode: str = 'agent', sub_mode: str | None = None) -> None:
        del config, mode, sub_mode
        # Identity and state come from admission, not facade profile aliases.
        self.bind_route(self._route)
        if self._goal_runtime is None and self._route.recovery is not None:
            from jiuwenswarm.runtime.harness.external_subagents import ExternalSubagentParentSession
            from jiuwenswarm.runtime.harness.team_goal import ExternalTeamGoalRuntime
            from jiuwenswarm.runtime.harness.tool_gateway import ProductToolScope
            binding = self._route.bound.binding
            async def no_product_output(chunk):
                raise RuntimeError('Root Team Goal state does not own a product output stream')
            parent = ExternalSubagentParentSession(binding.host_session_id,
                write_output=no_product_output, recovery=self._route.recovery)
            self._goal_runtime = ExternalTeamGoalRuntime(self, parent,
                ProductToolScope(binding.subject_id, binding.host_session_id, binding.workspace))
        self._created = True

    def set_heartbeat_service(self, service) -> None:
        self._heartbeat_service = service

    async def reload_agent_config(self, config_base=None, env_overrides=None,
                                  target_session_id=None, reload_scopes=None) -> None:
        # Profile changes apply to new bindings; the member host checks drift.
        return None

    async def process_message_stream_impl(self, request: AgentRequest, inputs: dict[str, Any]):
        self.select_execution_for_request(request)
        if not self._created:
            raise RuntimeError('External Team adapter has not been initialized')
        from jiuwenswarm.server.runtime.agent_adapter.goal_control import wants_attach_goal, tui_goal_operation
        if (request.req_method == ReqMethod.COMMAND_GOAL or wants_attach_goal(request.params)
                or tui_goal_operation(request) is not None):
            if self._goal_runtime is not None:
                from jiuwenswarm.runtime.context import get_current_runtime
                from jiuwenswarm.server.runtime.agent_adapter.goal_control import structured_goal_operation
                runtime = get_current_runtime()
                if runtime is None:
                    raise RuntimeError('External Team Goal requires its original Runtime producer')
                owner = runtime.external_execution_owner(request.session_id, request.request_id)
                operation = structured_goal_operation(request) or tui_goal_operation(request)
                async with aclosing(self._goal_runtime.stream(request, inputs, runtime=runtime,
                                                              owner=owner, operation=operation)) as stream:
                    async for chunk in stream:
                        yield chunk
                return
            result = self._unsupported(request, 'Goal')
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                     payload=result.payload, is_complete=True)
            return
        if self._goal_runtime is not None and self._goal_runtime.owner is not None:
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                payload={'event_type': 'chat.error', 'code': 'goal_owner_busy',
                         'error': 'Cancel the active Team Goal before submitting an ordinary Team request.'},
                is_complete=True)
            return
        for key in ('project_dir', 'cwd'):
            if inputs.get(key) and Path(inputs[key]).resolve() != (
                self._route.runtime_paths.cwd.resolve() if key == 'cwd'
                else self._route.runtime_paths.project_root.resolve()
            ):
                raise ValueError('External Team input workspace changed')
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
            bind_team_heartbeat_service, process_team_message_stream, reset_team_heartbeat_service,
        )
        token = bind_team_heartbeat_service(self._heartbeat_service)
        try:
            request = copy(request)
            request.params = {**request.params, 'mode': self._route.surface.initial_mode}
            request.metadata = {**(request.metadata or {}), 'mode': self._route.surface.initial_mode}
            # The helper keeps the historical unused parent parameter for Native
            # callers. Team assembly needs only its original Spec/BuildContext.
            async with aclosing(process_team_message_stream(request, inputs, None)) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            reset_team_heartbeat_service(token)

    @property
    def team_manager(self):
        from jiuwenswarm.agents.harness.team import get_team_manager
        return get_team_manager(self._route.channel_id)

    def needs_goal_assessor(self, request):
        from jiuwenswarm.server.runtime.agent_adapter.goal_control import wants_attach_goal
        return self._goal_runtime is not None and wants_attach_goal(request.params)

    def set_goal_assessor_factory(self, factory, *, request=None):
        if request is not None:
            request._goal_assessor_factory = factory
        elif self._goal_runtime is not None:
            self._goal_runtime.assessor_factory = factory

    async def process_goal_round(self, request, inputs, goal):
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import process_team_message_stream
        self.select_execution_for_request(request)
        current = copy(request)
        current._team_goal_attempt = goal
        current.params = {**request.params, 'mode': self._route.surface.initial_mode}
        current.metadata = {**(request.metadata or {}), 'mode': self._route.surface.initial_mode}
        async with aclosing(process_team_message_stream(current, inputs, None)) as stream:
            async for chunk in stream:
                yield chunk

    async def confirm_goal_round_exit(self, goal):
        manager = self.team_manager
        sid = self._route.bound.binding.host_session_id
        if goal.attempt is None:
            if manager.is_round_active(sid) or manager.is_runtime_active(sid) or manager.is_runtime_pending(sid):
                raise RuntimeError('Team Goal exit cannot take another Round owner')
            return
        await manager.confirm_goal_round_exit(sid, goal.owner.request_id, goal.attempt)

    def release_goal_owner(self, runtime, owner, request, goal):
        if (getattr(request, '_defer_execution_until_history', False)
                and not getattr(request, '_execution_history_complete', False)
                and runtime.holds_external_execution(owner)):
            self._history_release[request.request_id] = (runtime, owner, goal)
            return
        runtime.release_external_execution(owner)
        goal.release_owner(owner)

    async def complete_request_history(self, request):
        pending = self._history_release.get(request.request_id)
        if pending is not None:
            from jiuwenswarm.server.runtime.agent_adapter.goal_history import flush_goal_set
            runtime, owner, goal = pending
            await flush_goal_set(owner.session_id)
            self._history_release.pop(request.request_id, None)
            runtime.release_external_execution(owner)
            goal.release_owner(owner)
        request._execution_history_complete = True

    def _unsupported(self, request: AgentRequest, operation: str) -> AgentResponse:
        self.select_execution_for_request(request)
        return AgentResponse(
            request_id=request.request_id, channel_id=request.channel_id, ok=False,
            payload={'event_type': 'chat.error', 'code': 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE',
                     'error': f'External Team {operation} integration is not available'},
            metadata=request.metadata,
        )

    async def process_message_impl(self, request, inputs):
        return self._unsupported(request, 'unary execution')

    async def process_interrupt(self, request):
        # The facade owns SessionManager and routes Team controls directly.
        return self._unsupported(request, 'direct adapter control; use the Team facade')

    async def handle_user_answer(self, request):
        # Control delivery borrows the already-bound Session, without request
        # admission allocating another producer. Preserve any explicit route
        # so conflicting bindings still fail the normal identity checks.
        request = copy(request)
        if getattr(request, '_execution_route', None) is None:
            request._execution_route = self._route
        self.select_execution_for_request(request)
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
        from jiuwenswarm.agents.harness.team import get_team_manager
        params = request.params or {}
        from openjiuwen.agent_teams.external.interaction_address import decode_interaction_address
        valid = (decode_interaction_address(params.get('request_id')) is not None
                 and isinstance(params.get('answers'), list) and bool(params['answers'])
                 and params.get('source') in {'ask_user_interrupt', 'confirm_interrupt'})
        answer = JiuWenSwarm._build_interactive_input_from_answers(
            params.get('request_id'), params.get('answers') or [], params.get('source'),
        ) if valid else None
        accepted, reason = (False, 'invalid_member_interaction')
        if answer is not None:
            accepted, reason = await get_team_manager(self._route.channel_id).interact(request.session_id, answer)
        return AgentResponse(request_id=request.request_id, channel_id=request.channel_id, ok=accepted,
                             payload={'event_type': 'chat.answer', 'resolved': accepted, 'reason': reason},
                             metadata=request.metadata)

    async def handle_swarmflow_reply(self, request):
        return self._unsupported(request, 'Swarmflow')

    async def handle_heartbeat(self, request):
        return self._unsupported(request, 'unary heartbeat')

    async def handle_goal_command_structured(self, params, session_id):
        if session_id != self._route.bound.binding.host_session_id:
            raise ValueError('External Team Goal Session changed')
        if self._goal_runtime is not None:
            from jiuwenswarm.server.runtime.agent_adapter.goal_control import structured_goal_control_kwargs
            return await self._goal_runtime.control(structured_goal_control_kwargs(params))
        return {'result_type': 'goal_error',
                'action': str((params or {}).get('action') or 'get').strip().lower(),
                'error_code': 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE',
                'code': 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE',
                'error': 'External Team Goal integration is not available'}

    def has_session_runtime(self, session_id=None) -> bool:
        sid = self._route.bound.binding.host_session_id
        if session_id is not None and session_id != sid:
            return False
        from jiuwenswarm.agents.harness.team import get_team_manager
        manager = get_team_manager(self._route.channel_id)
        return bool(manager.is_runtime_active(sid) or manager.is_runtime_pending(sid)
                    or manager.get_team_agent(sid) is not None or manager.has_waiters(sid))

    async def cancel_active_goal(self):
        goal = self._goal_runtime
        if goal is None or goal.identity is None:
            return False
        await goal.cancel_attempt(goal_id=goal.identity.goal_id, reason='user_cancel')
        return True

    async def cleanup_session_adapter(self, session_id: str) -> bool:
        if session_id != self._route.bound.binding.host_session_id:
            return False
        goal = self._goal_runtime
        if goal is not None and goal.identity is not None:
            await goal.cancel_attempt(goal_id=goal.identity.goal_id, reason='adapter_cleanup')
            if goal.owner.state.terminal:
                owner, runtime, request = goal.owner, goal.runtime, goal.request
                await goal._cleanup_attempt(assessor_unknown=True)
                self.release_goal_owner(runtime, owner, request, goal)
            else:
                await goal.owner_done.wait()
        from jiuwenswarm.agents.harness.team import get_team_manager
        return await get_team_manager(self._route.channel_id).stop_session_runtime(
            session_id, reason='External Team adapter cleanup', require_exit_confirmation=True,
        )

    async def cleanup(self) -> None:
        await self.cleanup_session_adapter(self._route.bound.binding.host_session_id)
        self._created = False

    async def abort_on_gateway_disconnect(self, *, exclude_session_ids=None) -> None:
        sid = self._route.bound.binding.host_session_id
        if sid in (exclude_session_ids or ()):
            return
        if await self.cancel_active_goal():
            return
        from jiuwenswarm.agents.harness.team import get_team_manager
        await get_team_manager(self._route.channel_id).cancel_session_runtime(
            sid, reason='External Team gateway disconnect', workflow_disposition='pause',
            require_exit_confirmation=True,
        )
