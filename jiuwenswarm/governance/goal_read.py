"""Read-only Native Goal routing facts, independently checked at each hop."""
from __future__ import annotations

from dataclasses import dataclass

from .session_sharing import SessionSharingDenied


def validate_goal_get(params):
    if (type(params) is not dict or params.get('action', 'get') != 'get'
            or set(params) - {'session_id', 'action', 'mode', 'work_mode', 'project_id'}
            or any(type(value) is not str for value in params.values())):
        raise SessionSharingDenied('Only explicit read-only Goal parameters are supported')


@dataclass(frozen=True, repr=False)
class GoalReadRoute:
    descriptor: object
    channel_id: str
    check: object

    def lookup_descriptor(self):
        self.check()
        return self.descriptor


def capture_goal_read_route(session_id, params, check_owner):
    """Pin existing metadata and explicit catalog selection without binding it."""
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.runtime.goal_read import NativeGoalReadDescriptor
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
    from openjiuwen.harness.engine.config import config_fingerprint

    validate_goal_get(params)
    supplied = dict(params)
    fields = ('channel_id', 'mode', 'work_mode', 'project_id', 'execution_profile_id',
              'execution_config_revision', 'execution_config_fingerprint')

    def facts():
        metadata = get_session_metadata(session_id, cache_bust=True,
                                       enable_writeback=False, infer_defaults=False)
        if type(metadata) is not dict:
            raise SessionSharingDenied('Original Goal Session metadata unavailable')
        values = tuple(metadata.get(key) for key in fields)
        if any(type(value) is not str or not value for value in values):
            raise SessionSharingDenied('Explicit Goal Session routing required')
        channel, mode, work_mode, project, profile, revision, fingerprint = values
        from jiuwenswarm.runtime.service import AgentRuntime
        if not AgentRuntime._is_single_agent_session_mode(mode, work_mode=work_mode):
            raise SessionSharingDenied('Native Goal reading requires Single')
        catalog = load_execution_catalog(get_config())
        spec = catalog.source(explicit_profile_id=profile).resolve() if catalog else None
        if (spec is None or spec.provider_id != 'native' or spec.config_revision != revision
                or config_fingerprint(spec) != fingerprint):
            raise SessionSharingDenied('Original Native Goal configuration unavailable')
        from jiuwenswarm.runtime.request import resolve_agent_request_mode
        original_mode = resolve_agent_request_mode(mode, work_mode=work_mode)[:2]
        requested_mode = resolve_agent_request_mode(params.get('mode', mode),
            work_mode=params.get('work_mode', work_mode))[:2]
        if requested_mode != original_mode:
            raise SessionSharingDenied('Goal mode hint conflicts with original Session')
        for key, expected in (('session_id', session_id), ('project_id', project)):
            if key in params and params[key] != expected:
                raise SessionSharingDenied('Goal routing hint conflicts with original Session')
        return values

    original = facts()
    descriptor = NativeGoalReadDescriptor(session_id, 'native', original[4], original[5], original[6])

    def check():
        validate_goal_get(params)
        if params != supplied or facts() != original:
            raise SessionSharingDenied('Original Goal read route changed')
        if check_owner() is not None:
            raise SessionSharingDenied('Goal owner read check failed')
        validate_goal_get(params)
        if params != supplied or facts() != original:
            raise SessionSharingDenied('Original Goal read route changed')

    check()
    return GoalReadRoute(descriptor, original[0], check)
