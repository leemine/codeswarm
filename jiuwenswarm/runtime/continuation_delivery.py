"""Final local delivery checks from original persisted continuation authority.

Gateway and AgentServer use the same host catalogs and original sidecar. No
callback crosses the wire and no response field creates a grant or binding.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from jiuwenswarm.governance.continuation import ContinuationInput
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.model_credentials import configured_model_metadata
from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.runtime.continuation_targets import ContinuationTargets
from jiuwenswarm.runtime.continuation_execution import target_snapshot
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.model_catalog import build_model_catalog
from jiuwenswarm.server.runtime.session.continuation import ContinuationCompiler
from jiuwenswarm.server.runtime.session.continuation_publication import parse_proof, read_approval


def _targets(host, identity_resolver):
    return ContinuationTargets(
        identity_resolver=lambda _: identity_resolver(),
        project_access=lambda pid, identity, action: host._storage.authorize(pid, identity.actor_id, action),
        resource_authorizer=host._storage,
    )


def _identity(resolver):
    identity = resolver()
    if type(identity) is not TrustedIdentity:
        raise SessionSharingDenied('trusted delivery identity required')
    return identity


def capture_continuation_delivery(host, identity_resolver, original_input, target_session_id):
    if type(original_input) is not ContinuationInput or target_session_id == original_input.session_id:
        raise SessionSharingDenied('private continuation result required')
    identity = _identity(identity_resolver)
    approval = read_approval(host, target_session_id, identity)
    if approval is None or parse_proof(approval['proof']).request != original_input:
        raise SessionSharingDenied('continuation result does not match original request')
    if parse_proof(approval['proof']).identity != identity:
        raise SessionSharingDenied('continuation result owner mismatch')
    revision = host.owner_revision(target_session_id, identity)
    selector = _targets(host, identity_resolver)
    target = selector.select(original_input)
    if (target_snapshot(target) != approval['target_snapshot']
            or target.execution_fingerprint != approval['config_fingerprint']):
        raise SessionSharingDenied('continuation approved target changed')

    def check():
        if _identity(identity_resolver) != identity:
            raise SessionSharingDenied('continuation delivery identity changed')
        if (read_approval(host, target_session_id, identity) != approval
                or host.owner_revision(target_session_id, identity) != revision):
            raise SessionSharingDenied('continuation result authority changed')
        selector.revalidate(target)
        if _identity(identity_resolver) != identity:
            raise SessionSharingDenied('continuation delivery identity changed')
    check()
    return check


def _options_request(params):
    keys = {'session_id', 'share_id', 'expected_revision', 'target_project_id'}
    if type(params) is not dict or set(params) != keys:
        raise ValueError('invalid continuation options fields')
    # A source proof for metadata lookup only. This token never enters Session
    # allocation/publication and is not returned as a creation capability.
    return ContinuationInput(**params, create_token='options-only', execution_profile_id='options-only')


def continuation_options(host, identity_resolver, params):
    from jiuwenswarm.common.config import get_config_raw
    original = _options_request(params)
    identity = _identity(identity_resolver)
    compiler = ContinuationCompiler(host, identity_resolver=identity_resolver, project_authorizer=host._storage)
    proof = compiler._capture(original)
    catalog = load_execution_catalog(get_config_raw())
    models = build_model_catalog(tuple(configured_model_metadata())).models
    selector = _targets(host, identity_resolver)
    targets, options = [], []
    if catalog is not None:
        if len(catalog.profile_ids) * len(models) > 1000:
            raise ResourceAccessDenied('continuation options inventory exceeds limit')
        for profile in catalog.profile_ids:
            for model in models:
                candidate = replace(original, execution_profile_id=profile, model_name=model.selection_key)
                try:
                    target = selector.select(candidate)
                except ResourceAccessDenied:
                    continue
                targets.append(target)
                options.append({'execution_profile_id': profile, 'provider_id': target.provider_id,
                                'mode': target.provision_input.mode, 'model_name': model.selection_key,
                                'label': model.display_name})
    payload = {**params, 'options': options}

    def check():
        if _identity(identity_resolver) != identity:
            raise SessionSharingDenied('continuation options identity changed')
        compiler.revalidate(proof)
        for target in targets:
            selector.revalidate(target)
    check()
    return payload, check


def capture_continuation_options_delivery(host, identity_resolver, original_options_dict, payload):
    expected = deepcopy(payload)
    params = deepcopy(original_options_dict)
    current, source_check = continuation_options(host, identity_resolver, params)
    if current != expected:
        raise SessionSharingDenied('continuation options changed before delivery')

    def check():
        source_check()
        current, _ = continuation_options(host, identity_resolver, params)
        if current != expected:
            raise SessionSharingDenied('continuation options changed before delivery')
    check()
    return check
