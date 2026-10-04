"""Bind each private continuation Turn to its original durable approval."""
from __future__ import annotations

from contextvars import copy_context
from dataclasses import dataclass, field

from jiuwenswarm.governance.model_credentials import configured_model_metadata, model_entry_fingerprint
from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.runtime.continuation_targets import ContinuationTargets
from jiuwenswarm.server.runtime.session.continuation_publication import parse_proof, read_approval, read_seed

_MARKER = object()


def target_snapshot(target):
    return {'model_binding_fingerprint': target.model_binding_fingerprint,
            'model_entry_fingerprint': target.model_entry_fingerprint,
            'project_dir': target.provision_input.project_dir,
            'work_mode': target.provision_input.work_mode,
            'mode': target.provision_input.mode}


def _admitted_execution(runtime, request, *, generation=None, execution_id=None):
    from jiuwenswarm.runtime.session.model import RuntimeSessionState
    session = runtime._session_coordinator.snapshot_session(request.session_id)
    if (session is None or session.state in {RuntimeSessionState.QUIESCING, RuntimeSessionState.CLOSED}
            or generation is not None and session.generation != generation):
        raise ResourceAccessDenied('continuation original Runtime generation unavailable')
    matches = [item for item in session.executions
               if item.request_id == request.request_id and item.session_id == request.session_id
               and item.generation == session.generation and not item.state.terminal
               and not item.cancellation_requested
               and (execution_id is None or item.execution_id == execution_id)]
    if len(matches) != 1:
        raise ResourceAccessDenied('continuation original Runtime execution unavailable')
    return matches[0]


@dataclass(frozen=True, repr=False)
class ContinuationExecution:
    session_id: str
    request_id: str
    target: object
    _runtime: object = field(repr=False)
    _request: object = field(repr=False)
    _approval: dict = field(repr=False)
    _targets: object = field(repr=False)
    _context: object = field(repr=False)
    _identity: object = field(repr=False)
    _marker: object = field(repr=False)
    _generation: int = field(repr=False)
    _execution_id: str = field(repr=False)

    def __deepcopy__(self, memo):
        return self

    def check(self):
        if (self._marker is not _MARKER or self._runtime._closed
                or self._request.session_id != self.session_id
                or self._request.request_id != self.request_id):
            raise ResourceAccessDenied('continuation request is no longer current')
        _admitted_execution(self._runtime, self._request, generation=self._generation,
                            execution_id=self._execution_id)
        identity = self._context.run(self._runtime._governance_identity, self._request)
        if identity != self._identity:
            raise ResourceAccessDenied('continuation identity changed')
        if read_approval(self._runtime._organization_session_host, self.session_id, identity) != self._approval:
            raise ResourceAccessDenied('continuation original approval changed')
        self._context.run(self._targets.revalidate, self.target)
        params = self._request.params if isinstance(self._request.params, dict) else {}
        model = params.get('model_name')
        if model not in (None, '', self.target.request.model_name):
            raise ResourceAccessDenied('continuation model selection is fixed')

    def check_model(self, binding, entry_fingerprint):
        self.check()
        if binding != self.target.model_binding or entry_fingerprint != self.target.model_entry_fingerprint:
            raise ResourceAccessDenied('model differs from original continuation approval')

    def build_model(self):
        """Use the exact metadata entry; never call legacy name/cache/login fallback."""
        self.check()
        index = int(self.target.request.model_name.rpartition('#')[2])
        entries = configured_model_metadata()
        try:
            entry = entries[index]
            client, config = entry['model_client_config'], entry['model_config_obj']
            fingerprint = model_entry_fingerprint(client, config)
            from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
            self.check_model(ModelCredentialBinding.from_config(client), fingerprint)
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import build_model_from_entry
            model = build_model_from_entry(client, config, model_entry_fingerprint=fingerprint)
        except Exception:
            raise ResourceAccessDenied('continuation model construction denied') from None
        self.check()
        return model

    async def make_context(self):
        self.check()
        from jiuwenswarm.server.runtime.session.history_io import run_history_io
        seed = await run_history_io(read_seed, self._runtime._organization_session_host,
                                    self.session_id, self._identity)
        self.check()
        from jiuwenswarm.governance.continuation_context import create_continuation_context
        return create_continuation_context(session_id=self.session_id, request_id=self.request_id,
                                           identity=self._identity, seed=seed, check=self.check)


def capture_continuation_execution(runtime, request):
    host = runtime._organization_session_host
    if host is None:
        return None
    identity = runtime._governance_identity(request)
    approval = read_approval(host, request.session_id, identity)
    if approval is None:
        return None
    proof = parse_proof(approval['proof'])
    if proof.identity != identity:
        raise ResourceAccessDenied('continuation owner changed')
    targets = ContinuationTargets(runtime)
    target = targets.select(proof.request)
    if (target_snapshot(target) != approval['target_snapshot']
            or target.execution_fingerprint != approval['config_fingerprint']):
        raise ResourceAccessDenied('continuation target differs from original approval')
    execution = _admitted_execution(runtime, request)
    result = ContinuationExecution(request.session_id, request.request_id, target, runtime, request,
                                   approval, targets, copy_context(), identity, _MARKER,
                                   execution.generation, execution.execution_id)
    result.check()
    return result


def require_continuation_execution(value, runtime=None):
    if type(value) is not ContinuationExecution or value._marker is not _MARKER:
        raise ResourceAccessDenied('trusted continuation execution required')
    if runtime is not None and value._runtime is not runtime:
        raise ResourceAccessDenied('continuation belongs to a different Runtime')
    value.check()
    return value
