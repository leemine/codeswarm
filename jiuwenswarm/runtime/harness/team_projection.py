"""Member attribution over the existing External and Team product projections."""
from __future__ import annotations

import hashlib

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import TurnEventKind, TurnLifecycleEvent, HarnessState

from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection


class TeamMemberProjection:
    def __init__(self, route, request, member_binding, *, agent_id):
        self.route = route
        self.request = request
        self.binding = member_binding
        paths = route.runtime_paths
        self.projection = ExternalEventProjection(
            route.bound.binding.host_session_id, workspace_root=paths.runtime_workspace_root,
            work_mode=route.surface.identity.work_mode,
            projection_scope=member_binding.host_session_id,
            cwd=paths.cwd, outputs_dir=paths.outputs_dir,
            provider_id=route.provider_id, artifact_sink=self.artifact,
            require_artifact_attribution=True,
        )
        self.runtime = None
        self._active_turn_id = None
        self._goal_observer = self.goal_observer(member_binding.host_session_id, agent_id)

    def goal_observer(self, host_session_id, agent_id, *, product_subagent=False):
        from jiuwenswarm.agents.harness.team import get_team_manager
        from jiuwenswarm.runtime.harness.team_goal_evidence import TeamGoalEventObserver, TeamGoalSource
        root = self.route.bound.binding.host_session_id
        # Capture the original manager, not whatever registry a later request
        # might install for the same textual Session id.
        manager = get_team_manager(self.route.channel_id)
        return TeamGoalEventObserver(TeamGoalSource(root, self.binding.host_session_id,
            host_session_id, agent_id, self.request.context.member_name, product_subagent),
            lambda: getattr(manager, 'current_goal_attempt_evidence', lambda _: None)(root))

    def product_goal_observer(self, host_session_id, agent_id):
        return self.goal_observer(host_session_id, agent_id, product_subagent=True)

    def identity(self, turn_id=None):
        return {
            'team_name': self.request.spec.team_name,
            'source_member': self.request.context.member_name,
            'member_name': self.request.context.member_name,
            'member_id': self.request.card.id,
            'role': self.request.context.role.value,
            'member_session_id': self.binding.host_session_id,
            'provider_session_id': self.runtime.session_id if self.runtime else None,
            'provider_id': self.route.provider_id,
            'turn_id': turn_id,
        }

    async def observe(self, envelope):
        await self._goal_observer(envelope)
        if isinstance(envelope.event, TurnLifecycleEvent) and envelope.event.kind is TurnEventKind.STARTED:
            self._active_turn_id = envelope.turn_id
            from jiuwenswarm.agents.harness.team import get_team_manager
            owner = get_team_manager(self.route.channel_id).current_round_request_id(
                self.route.bound.binding.host_session_id)
            self.projection.register_turn(
                envelope.turn_id, request_id=owner or f'team-member:{envelope.turn_id}',
                channel_id=self.route.channel_id, mode=self.route.surface.initial_mode,
            )
        await self.projection.observe(envelope)
        if (isinstance(envelope.event, TurnLifecycleEvent)
                and envelope.event.kind in {TurnEventKind.FINISHED, TurnEventKind.ABORTED, TurnEventKind.FAILED}
                and envelope.turn_id == self._active_turn_id):
            self._active_turn_id = None

    async def output(self, item):
        if not self.projection.accepts_turn_output(item.turn_id):
            return None
        chunk = item.chunk
        if chunk is not None and chunk.type == '__interaction__':
            value = chunk.payload.value
            if isinstance(value, dict) and value.get('kind') == 'tool_approval':
                payload = {
                    'event_type': 'chat.ask_user_question', 'request_id': chunk.payload.id,
                    'source': 'confirm_interrupt',
                    'questions': [{'question': value.get('reason') or f"允许执行 {value['tool_name']}？",
                                   'header': '工具审批', 'options': [
                                       {'label': '本次允许', 'value': 'allow_once'},
                                       {'label': '拒绝', 'value': 'reject'},
                                   ], 'multiSelect': False}],
                }
            else:
                payload = self.projection.payload(item)
        elif item.turn_id:
            payload = self.projection.payload(item)
        elif chunk is not None:
            payload = self.projection.payload(item)
        else:
            payload = None
        if payload is None:
            return None
        payload = {**payload, **self.identity(item.turn_id)}
        # Provider terminals settle a member Turn, never the whole Team.
        if item.terminal is not None:
            payload['event_type'] = 'team.member_turn'
            payload.setdefault('terminal_status', item.terminal.value)
        tool = payload.get('tool_call')
        if isinstance(tool, dict) and tool.get('tool_call_id'):
            payload['tool_call'] = {**tool, 'tool_call_id': self.tool_id(item.turn_id, tool['tool_call_id'])}
        if payload.get('tool_call_id'):
            payload['tool_call_id'] = self.tool_id(item.turn_id, payload['tool_call_id'])
        for container in (payload, payload.get('tool_call')):
            activity = container.get('surface_projection') if isinstance(container, dict) else None
            if isinstance(activity, dict) and activity.get('item_id'):
                container['surface_projection'] = {
                    **activity, 'item_id': self.tool_id(item.turn_id, activity['item_id']),
                }
        if payload.get('event_type') == 'chat.ask_user_question':
            from jiuwenswarm.runtime.context import get_current_runtime
            from jiuwenswarm.runtime.events import RuntimeEvent
            from jiuwenswarm.agents.harness.team import get_team_manager
            host = get_current_runtime()
            if host is not None:
                root = self.route.bound.binding.host_session_id
                owner = get_team_manager(self.route.channel_id).current_round_request_id(root)
                if not owner:
                    raise RuntimeError('Team interaction has no active root round owner')
                await host.register_host_interaction(RuntimeEvent.control(
                    request_id=owner, channel_id=self.route.channel_id, session_id=root, payload=payload,
                ))
        if not payload.pop('_team_product_history_written', False):
            if not await self.projection.persist_member_output(item, payload):
                payload = {
                    **self.identity(item.turn_id), 'event_type': 'chat.error',
                    'code': 'HISTORY_PERSISTENCE_UNCONFIRMED', 'terminal_status': 'unknown',
                    'error': 'Team member history persistence was not confirmed',
                }
        payload['_team_history_owned'] = True
        return OutputSchema(type='team_projection', index=chunk.index if chunk else 0, payload=payload)

    def tool_id(self, turn_id, item_id):
        identity = '\0'.join((self.binding.host_session_id, self.runtime.session_id or '', turn_id or '', item_id))
        return 'team-tool-' + hashlib.sha256(identity.encode()).hexdigest()

    async def product_output(self, chunk):
        from jiuwenswarm.server.runtime.agent_adapter.subagent_projection import parse_subagent_stream_chunk
        from jiuwenswarm.server.runtime.session.history_io import run_history_io
        # Product ownership remains the member; the existing UI history lives
        # under the root conversation and retains the member as provenance.
        raw = dict(chunk.payload)
        for key, value in raw.items():
            if isinstance(value, dict):
                raw[key] = {**value, 'parent_session_id': self.route.bound.binding.host_session_id,
                            'member_session_id': self.binding.host_session_id,
                            'source_member': self.request.context.member_name}
        chunk = chunk.model_copy(update={'payload': raw})
        payload = await run_history_io(
            parse_subagent_stream_chunk, chunk,
            parent_session_id=self.route.bound.binding.host_session_id,
        )
        if payload is not None and self.runtime.state is not HarnessState.TERMINATED:
            await self.runtime.publish_output(OutputSchema(
                type='team_projection', index=chunk.index,
                payload={**payload, **self.identity(), '_team_product_history_written': True},
            ))

    def artifact_tools(self):
        """Explicit member delivery through the existing projected file service.

        Shell writes have no reliable file attribution. Delivery is an explicit
        product tool call, scoped to this member and the admitted workspace.
        """
        import json
        from pathlib import Path
        from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
        from jiuwenswarm.runtime.harness.surface_projection import _project_output_artifact

        async def send_file_to_user(abs_file_path_list: str):
            if not self._active_turn_id:
                raise RuntimeError('Team artifact delivery requires an active member Turn')
            raw = json.loads(abs_file_path_list) if abs_file_path_list.startswith('[') else [abs_file_path_list]
            if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
                raise ValueError('Artifact paths must be a nonempty list of absolute file paths')
            artifacts = []
            for item in raw:
                path = Path(item)
                if not path.is_absolute():
                    raise ValueError('Artifact path must be absolute')
                # Validate all paths before delivering any of a multi-file call.
                artifact = _project_output_artifact(
                    path, workspace_root=self.route.runtime_paths.runtime_workspace_root,
                    provider_id=self.route.provider_id, session_id=self.binding.host_session_id,
                    turn_id=self._active_turn_id,
                )
                artifacts.append((artifact, path))
            for artifact, path in artifacts:
                await self.artifact(artifact, path)
            return 'Files delivered to the current conversation.'

        return [LocalFunction(card=ToolCard(
            name='send_file_to_user',
            description='Deliver generated files from this workspace to the user in the current conversation. '
                        'Use an absolute file path or a JSON array string of absolute file paths.',
            input_params={'type': 'object', 'properties': {
                'abs_file_path_list': {'type': 'string'}}, 'required': ['abs_file_path_list']},
        ), func=send_file_to_user)]

    async def artifact(self, artifact, path):
        artifact = artifact.model_copy(update={'metadata': {
            **artifact.metadata, **self.identity(artifact.metadata.get('turn_id')),
        }})
        await self.projection.publish_product_artifact(artifact, path)

    async def close(self):
        self._goal_observer.close()
        await self.projection.close()


class TeamProductParentSession:
    """Product child state in the original member Session, no second state store."""
    def __init__(self, session_id, projection):
        self._session_id = session_id
        self._projection = projection

    def get_session_id(self):
        return self._session_id

    def get_state(self, key=None):
        return self._projection.runtime.member_session.get_state(key)

    def update_state(self, data):
        self._projection.runtime.member_session.update_state(data)

    async def write_stream(self, chunk):
        await self._projection.product_output(chunk)
