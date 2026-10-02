# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Team-owned Provider construction from an admitted root execution snapshot."""
from __future__ import annotations

from dataclasses import asdict, fields, replace
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams import TEAM_MEMBER_RUNTIME_FACTORY, TeamAgentSpec, TeamMemberRuntimeBuild
from openjiuwen.agent_teams.external.member_runtime import ExternalHarnessMemberRuntime
from openjiuwen.agent_teams.prompts.sections import build_team_member_system_prompt
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.team_context import TeamContextTracker
from openjiuwen.harness.engine import ExecutionBinding, create_harness_engine
from openjiuwen.harness_protocol import HostCapability, ResumePolicy, WorkspaceAccess, UserInputRequest, json_value_to_builtin
from openjiuwen.harness_providers.construction import execution_authorization, configured_provider_capabilities

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import BoundExecution, ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.context_bridge import build_external_context
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.surface import (
    EffectiveSurfaceSnapshot, SurfaceAdmissionError, build_surface_identity, compile_surface_policy,
    canonical_surface_mode,
)
from jiuwenswarm.runtime.harness.team_tools import team_product_tools
from jiuwenswarm.runtime.harness.capability_catalog import compile_capability_catalog
from jiuwenswarm.runtime.harness.ui_capability_manifest import compile_ui_capability_manifest
from jiuwenswarm.runtime.harness.external_subagents import ExternalSubagentRuntime, PRODUCT_SUBAGENT_TOOL_NAMES
from jiuwenswarm.runtime.harness.external_subagent_profiles import surface_external_subagent_profiles
from jiuwenswarm.runtime.harness.external_browser_admission import ExternalBrowserAdmission, browser_runtime_enabled
from jiuwenswarm.runtime.harness.team_projection import TeamMemberProjection, TeamProductParentSession
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolScope
from jiuwenswarm.runtime.harness.tool_transport import ManagedProductToolTransport


def _host_config():
    from jiuwenswarm.common.config import get_config
    return get_config()


def _session_metadata(session_id):
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
    return get_session_metadata(session_id, cache_bust=True, enable_writeback=False, infer_defaults=False)


def _paths_record(paths: RuntimeWorkspacePaths) -> dict[str, Any]:
    return {field.name: str(value) if isinstance(value, Path) else value
            for field in fields(paths) if (value := getattr(paths, field.name)) is not None}


def _validate_metadata_workspace(metadata, binding):
    root = metadata.get('project_dir')
    if not root:
        from jiuwenswarm.common.projectless_workspace import get_registered_projectless_task_root
        root = get_registered_projectless_task_root(binding.host_session_id)
    if root is None or str(Path(root).resolve()) != binding.workspace:
        raise ValueError('Team Session workspace is missing or changed')


class ExternalTeamMemberFactory:
    """One frozen root selection; original Team owns all member lifecycles."""

    def __init__(self, route: AdmittedExecutionRoute, *, team_name: str,
                 include_personal_context: bool = False) -> None:
        binding = route.bound.binding
        binding.validate_spec(route.bound.spec)
        surface = route.surface
        if binding.provider_id not in {'codex', 'opencode'}:
            raise ValueError('External Team supports Codex and OpenCode construction')
        if surface is None or surface.identity.binding != binding or surface.identity.paths != route.runtime_paths:
            raise ValueError('External Team requires the exact admitted Surface and Binding')
        if surface.identity.channel_id != route.channel_id:
            raise ValueError('External Team channel differs from its Surface')
        if not surface.identity.execution_profile_id.strip():
            raise ValueError('External Team requires an explicit execution profile')
        if surface.identity.team_name and surface.identity.team_name != team_name:
            raise ValueError('External Team name differs from its Surface')
        if not team_name or surface.identity.topology != 'team':
            raise ValueError('External Team requires a named Team Surface')
        if not surface.initial_mode.endswith('.normal'):
            raise SurfaceAdmissionError('External Team plan integration is not available')
        self._authorization = execution_authorization(route.bound.spec)
        self._surface = compile_surface_policy(
            surface, authorization=self._authorization,
            include_personal_context=include_personal_context, topology='team',
        )
        self._route = route
        self._team_name = team_name
        self._include_personal_context = include_personal_context
        self._browser_enabled = browser_runtime_enabled(os.environ)
        self._review_state_lock = asyncio.Lock()
        catalog = compile_capability_catalog(
            self._surface, provider_inventory=configured_provider_capabilities(route.bound.spec),
            product_tool_names=PRODUCT_SUBAGENT_TOOL_NAMES,
            product_subagent_types=tuple(p.subagent_type for p in surface_external_subagent_profiles(
                self._surface.identity.work_mode, browser_available=self._browser_enabled,
            )), authorization=self._authorization,
        )
        self._surface = replace(self._surface, capability_catalog=catalog,
                                ui_capability_manifest=compile_ui_capability_manifest(catalog))

    @property
    def surface(self):
        return self._surface

    @property
    def provider_id(self) -> str:
        return self._route.provider_id

    def validate_team_spec(self, spec: TeamAgentSpec) -> None:
        self._validate_team_spec(spec)

    def _validate_team_spec(self, spec: TeamAgentSpec, *, scheduled: bool = False) -> None:
        if spec.team_name != self._team_name or spec.execution_provider != self.provider_id:
            raise ValueError('Team Spec identity differs from admitted execution')
        workspace = spec.workspace
        if workspace is not None and workspace.enabled:
            if (not workspace.root_path or not Path(workspace.root_path).resolve().is_relative_to(
                    self._route.runtime_paths.runtime_workspace_root.resolve())):
                raise ValueError('Team workspace escapes the admitted root')
            if workspace.remote_url:
                raise ValueError('External Team distributed workspace is not integrated')
        if spec.spawn_mode != 'inprocess':
            raise ValueError('External Team member interaction confirmation requires inprocess spawning')
        if spec.worktree and spec.worktree.enabled:
            raise ValueError('External Team worktree relocation is not integrated')
        for role_spec in spec.agents.values():
            if any((role_spec.tools, role_spec.rails, role_spec.subagents, role_spec.mcps,
                    role_spec.skills, role_spec.agent_template_spec)):
                raise ValueError('External Team Native capability declarations are not integrated')
            if any(path and Path(path).resolve() != self._route.runtime_paths.cwd.resolve()
                   for path in (role_spec.cwd, role_spec.project_root)):
                raise ValueError('Team member paths differ from the admitted workspace')
        if not self._host_selection_unchanged():
            raise ValueError('Team execution authorization or configuration changed before start')

    def to_seed(self) -> dict[str, Any]:
        """Identity only: no Provider config, token, runtime or callback."""
        return {
            'version': 1, 'team_name': self._team_name,
            'binding': asdict(self._route.bound.binding),
            'paths': _paths_record(self._route.runtime_paths),
            'surface': self._surface.identity.record(),
            'mode': self._surface.initial_mode,
            'include_personal_context': self._include_personal_context,
        }

    @classmethod
    def from_seed(cls, seed: dict[str, Any], *, config: dict[str, Any]):
        """Revalidate stored identity against local config and Session metadata."""
        if seed.get('version') != 1:
            raise ValueError('Unsupported Team execution seed')
        binding = ExecutionBinding(**seed['binding'])
        raw_paths = seed['paths']
        paths = RuntimeWorkspacePaths(**{
            key: value if key == 'is_projectless' else Path(value)
            for key, value in raw_paths.items()
        })
        catalog = load_execution_catalog(config)
        if catalog is None:
            raise ValueError('Team execution profile is no longer configured')
        source = catalog.source(explicit_profile_id=seed['surface']['execution_profile_id'])
        spec = source.resolve()
        binding.validate_spec(spec)
        metadata = _session_metadata(binding.host_session_id)
        if (not isinstance(metadata, dict)
                or metadata.get('execution_config_fingerprint') != binding.fingerprint
                or metadata.get('execution_config_revision') != binding.config_revision):
            raise ValueError('Team Session execution identity is missing or changed')
        _validate_metadata_workspace(metadata, binding)
        identity = build_surface_identity(
            metadata=metadata, binding=binding, paths=paths, channel_id=seed['surface']['channel_id'],
        )
        if identity.record() != seed['surface']:
            raise ValueError('Team Session Surface changed during reconstruction')
        if canonical_surface_mode(metadata) != seed['mode']:
            raise ValueError('Team Session execution state changed during reconstruction')
        route = AdmittedExecutionRoute(
            channel_id=identity.channel_id, source=source, bindings=ExecutionBindingStore(),
            bound=BoundExecution(binding, spec), runtime_paths=paths,
            surface=EffectiveSurfaceSnapshot(identity, seed['mode']),
        )
        return cls(route, team_name=seed['team_name'],
                   include_personal_context=seed['include_personal_context'])

    def _host_selection_unchanged(self) -> bool:
        try:
            if browser_runtime_enabled(os.environ) != self._browser_enabled:
                return False
            catalog = load_execution_catalog(_host_config())
            if catalog is None:
                return False
            spec = catalog.source(explicit_profile_id=self._surface.identity.execution_profile_id).resolve()
            self._route.bound.binding.validate_spec(spec)
            binding = self._route.bound.binding
            metadata = _session_metadata(binding.host_session_id)
            if (not isinstance(metadata, dict)
                    or metadata.get('execution_config_fingerprint') != binding.fingerprint
                    or metadata.get('execution_config_revision') != binding.config_revision):
                return False
            _validate_metadata_workspace(metadata, binding)
            identity = build_surface_identity(metadata=metadata, binding=binding,
                                              paths=self._route.runtime_paths, channel_id=self._route.channel_id)
            return (identity.record() == self._surface.identity.record()
                    and canonical_surface_mode(metadata) == self._surface.initial_mode)
        except (ValueError, TypeError):
            return False

    def build_review_runtime(self, request):
        from jiuwenswarm.runtime.harness.team_review import ExternalTeamReviewRuntime
        self._validate_team_spec(request.spec, scheduled=True)
        return ExternalTeamReviewRuntime(self, request, create_engine=create_harness_engine,
                                         transport_type=ManagedProductToolTransport)

    def build_member_runtime(self, request: TeamMemberRuntimeBuild) -> ExternalHarnessMemberRuntime:
        self.validate_team_spec(request.spec)
        if request.context.role not in {TeamRole.LEADER, TeamRole.TEAMMATE}:
            raise ValueError('External Team requires an ordinary execution member')
        paths = self._route.runtime_paths
        if request.context.worktree_path:
            raise ValueError('External Team worktree relocation is not integrated')
        root = self._route.bound.binding
        member_key = hashlib.sha256(json.dumps(
            [self._team_name, request.card.id, request.context.member_name], separators=(',', ':'),
        ).encode()).hexdigest()
        agent_id = f'team-member:{member_key}'
        binding = ExecutionBinding.create(
            self._route.bound.spec, subject_id=root.subject_id,
            host_session_id=f'{root.host_session_id}:team-member:{member_key}', workspace=root.workspace,
        )
        engine = create_harness_engine(self._route.bound.spec, binding=binding)
        scope = ProductToolScope(binding.subject_id, binding.host_session_id, binding.workspace)
        transport: ManagedProductToolTransport | None = None
        products: ExternalSubagentRuntime | None = None
        projection: TeamMemberProjection | None = None
        accepting = False

        async def admit(actual_scope, invocation):
            if not (accepting and actual_scope == scope and self._host_selection_unchanged()):
                return False
            if invocation.name in {'subagent_spawn', 'subagent_resume', 'subagent_send_input'}:
                runtime.member_session.update_state({'external_member_products_active': True})
                await runtime.member_session.commit()
            return True

        async def context_factory(team_session):
            nonlocal transport, accepting, products, projection
            if team_session is None or team_session.get_session_id() != root.host_session_id:
                raise ValueError('Member start belongs to another Team Session')
            self.validate_team_spec(request.spec)
            if request.context.role is TeamRole.LEADER:
                from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
                validate_review_recovery(team_session)
            projection = TeamMemberProjection(self._route, request, binding, agent_id=agent_id)
            projection.runtime = runtime
            browser = None

            async def publish_browser_question(payload, delivery_id):
                del delivery_id
                question = payload['questions'][0]['question']
                response = await asyncio.wait_for(runtime.request_interaction(UserInputRequest(
                    request_id=payload['request_id'], prompt=question, choices=('allow_once', 'reject'),
                )), timeout=300)
                value = json_value_to_builtin(response.content)
                answer = value.get('answers', {}).get(question, '') if isinstance(value, dict) else ''
                await browser.answer({'request_id': payload['request_id'],
                                      'answers': [{'selected_options': [answer]}]})

            if self._browser_enabled:
                browser = ExternalBrowserAdmission(
                    parent_subject_id=binding.subject_id, parent_session_id=binding.host_session_id,
                    runtime_paths=paths, publish=publish_browser_question,
                )
            member_route = replace(self._route, bound=BoundExecution(binding, self._route.bound.spec),
                                   surface=self._surface, recovery=None)
            products = ExternalSubagentRuntime(
                member_route, write_output=projection.product_output,
                event_observer_factory=projection.product_goal_observer,
                parent_session=TeamProductParentSession(binding.host_session_id, projection),
                additional_tools=[*team_product_tools(request), *projection.artifact_tools()], tool_admit=admit,
                invoke_kwargs={'member_name': request.context.member_name,
                               'display_name': request.context.member_name},
                browser_admit=browser, browser_artifact_sink=projection.artifact if browser else None,
                browser_decision_id_for=browser.decision_id_for if browser else None,
            )
            gateway = products.gateway
            context = build_external_context(
                paths=paths, host_session_id=binding.host_session_id,
                channel_id=self._route.channel_id, provider_id=self.provider_id, surface=products.surface,
            )
            prompt = build_team_member_system_prompt(
                role=request.context.role, member_name=request.context.member_name,
                display_name=request.context.display_name or '', member_prompt=request.context.prompt or '',
                lifecycle=request.spec.lifecycle, teammate_mode=str(request.spec.teammate_mode),
                team_mode=request.team_mode, dispatch_mode=request.spec.dispatch_mode,
                language=request.language, workspace_prompt_variant='external',
                base_prompt=(request.spec.agents.get(request.context.role.value)
                             or request.spec.agents['leader']).system_prompt,
            )
            transport = ManagedProductToolTransport(gateway, host_session_id=binding.host_session_id)
            await transport.start()
            accepting = True
            return replace(
                context, agent_name=request.context.member_name, agent_id=agent_id,
                system_prompt=context.system_prompt + '\n\n' + prompt,
                mcp_servers=(transport.server_config(),),
                host_capabilities=context.host_capabilities | {HostCapability.MCP_SERVERS},
                resume_policy=(ResumePolicy.REQUIRE_RESUME if request.team_backend.history_restored
                               else ResumePolicy.NEW),
                metadata={**context.metadata, 'team_name': self._team_name,
                          'member_name': request.context.member_name, 'member_role': request.context.role.value,
                          'member_card_id': request.card.id,
                          'parent_session_id': root.host_session_id, 'member_binding': asdict(binding)},
            )

        async def cleanup():
            nonlocal transport, accepting, products, projection
            accepting = False
            if products is not None:
                await products.close()
                products = None
            if transport is not None:
                await transport.stop()
                if not transport.exit_confirmed:
                    raise RuntimeError('Team member MCP transport exit was not confirmed')
                transport = None
            if projection is not None:
                await projection.close()
                projection = None

        async def observe(envelope):
            if projection is not None:
                await projection.observe(envelope)

        async def project(item):
            if projection is not None:
                return await projection.output(item)
            return item.chunk

        tracker = TeamContextTracker(
            team_backend=request.team_backend, member_name=request.context.member_name,
            role=request.context.role, display_name=request.context.display_name or '',
            member_prompt=request.context.prompt or '', language=request.language,
            team_workspace_path=(request.spec.workspace.root_path if request.spec.workspace
                                 and request.spec.workspace.enabled else None),
            team_outputs_dir=str(paths.outputs_dir) if paths.outputs_dir else None,
        )
        runtime = ExternalHarnessMemberRuntime(
            harness=engine.harness, context=context_factory, team_context_tracker=tracker,
            auto_approve_tools=self._surface.runtime_policy.workspace_access is WorkspaceAccess.FULL_ACCESS,
            resume_external_backend=True, strict_checkpoint_validation=True,
            interaction_scope=(self._team_name, request.context.member_name, root.host_session_id),
            event_observer=observe, output_projection=project,
        )
        runtime.add_teardown_hook(cleanup)
        return runtime


def attach_external_team_execution(spec: TeamAgentSpec, route: AdmittedExecutionRoute) -> TeamAgentSpec:
    """Attach the host port through the existing Swarm BuildContext and seed."""
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from openjiuwen.agent_teams.agent.runtime_factory import require_member_runtime_factory

    factory = ExternalTeamMemberFactory(route, team_name=spec.team_name)
    spec.execution_provider = factory.provider_id
    context = SwarmBuildContext(
        session_id=route.bound.binding.host_session_id, user_id=route.bound.binding.subject_id,
        channel_id=route.channel_id, channel=route.channel_id, mode=route.surface.initial_mode,
        project_dir=str(route.runtime_paths.project_root), team_id=spec.team_name,
        team_outputs_dir=str(route.runtime_paths.outputs_dir) if route.runtime_paths.outputs_dir else None,
        external_team_execution=factory.to_seed(),
        extras={TEAM_MEMBER_RUNTIME_FACTORY: factory},
    )
    spec.build_context = context
    spec.build_context_seed = context.to_seed()
    require_member_runtime_factory(spec)
    return spec
