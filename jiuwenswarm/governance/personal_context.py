"""Resolve the same private home for the Host API and the existing Core Rail."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .contracts import TrustedIdentity


PERSONAL_CONTEXT_READS = {
    'personal_context.runtime.status': frozenset(),
    'personal_context.runtime.get_config': frozenset(),
    'personal_context.fetch.list_services': frozenset(),
    'personal_context.fetch.get_run_status': frozenset({'service_id', 'run_id'}),
    'personal_context.fetch.get_authorization_status': frozenset({'provider'}),
    'personal_context.context.stream_graph': frozenset({'root_id', 'depth'}),
    'personal_context.context.stream_tree': frozenset({'root_id', 'depth'}),
    'personal_context.context.search_pages': frozenset({'query'}),
    'personal_context.context.get_node': frozenset({'node_id'}),
    'personal_context.context.get_source': frozenset({'source_id'}),
}
PERSONAL_CONTEXT_WRITES = {
    **{f'personal_context.runtime.{op}': frozenset() for op in (
        'start_collection', 'stop_collection', 'start_agent_use', 'stop_agent_use')},
    'personal_context.runtime.patch_config': frozenset({'patch'}),
    'personal_context.runtime.select_model': frozenset({'model_index'}),
    'personal_context.fetch.create_service': frozenset({'service'}),
    'personal_context.fetch.patch_service': frozenset({'service_id', 'patch'}),
    **{f'personal_context.fetch.{op}': frozenset({'service_id'}) for op in (
        'delete_service', 'start_service', 'stop_service', 'run_one', 'stop_run')},
    'personal_context.fetch.run_all': frozenset(),
    'personal_context.fetch.authorize_provider': frozenset({'provider', 'credentials', 'reauthorize'}),
}
PERSONAL_CONTEXT_METHODS = {**PERSONAL_CONTEXT_READS, **PERSONAL_CONTEXT_WRITES}
PERSONAL_CONTEXT_STREAMS = frozenset({
    'personal_context.context.stream_graph', 'personal_context.context.stream_tree',
})


def personal_context_home(identity=None, *, root=None):
    from .organization_auth import configured_authenticator, current_identity
    root = Path(root) if root is not None else Path.home() / '.jiuwenswarm' / '.personal_context'
    if configured_authenticator() is None:
        return root
    identity = identity if identity is not None else current_identity()
    if not isinstance(identity, TrustedIdentity):
        raise PermissionError('Personal context identity unavailable')
    # Never use a wire user_id or an account name as a directory component.
    # Full authority and subject distinguish equal actor names on different hosts.
    key = hashlib.sha256(json.dumps(asdict(identity), sort_keys=True,
                                   separators=(',', ':')).encode()).hexdigest()
    target = root / 'subjects' / key
    for path in (target, *target.parents):
        if path.is_symlink():
            raise PermissionError('Personal context home unavailable')
    return target
