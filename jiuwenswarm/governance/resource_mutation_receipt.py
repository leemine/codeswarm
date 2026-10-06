"""Only correlated, nonsecret resource mutation facts may cross an error sink."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceMutationRequest:
    method: str
    project_id: str
    resource_id: str
    target_actor: str
    expected_resource_revision: int


def capture_resource_mutation(method, params):
    if method not in {'project.resources.grant', 'project.resources.revoke'} or type(params) is not dict:
        return None
    for key in ('project_id', 'resource_id', 'target_actor'):
        item = params.get(key)
        if (type(item) is not str or not item or len(item) > 200 or item != item.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in item)):
            return None
    before = params.get('expected_resource_revision')
    if type(before) is not int or not 0 <= before < 2 ** 53 - 1:
        return None
    return ResourceMutationRequest(method, params['project_id'], params['resource_id'], params['target_actor'], before)


def resource_mutation_error_payload(value, request):
    if (type(request) is not ResourceMutationRequest or type(value) is not dict
            or value.get('code') != 'EXIT_UNCONFIRMED' or value.get('exit_confirmed') is not False):
        return None
    mutation = value.get('mutation')
    if (type(mutation) is not dict
            or set(mutation) != {'committed', 'project_id', 'resource_id', 'target_actor', 'resource_revision'}
            or mutation['committed'] is not True
            or any(mutation[key] != getattr(request, key) for key in ('project_id', 'resource_id', 'target_actor'))
            or type(mutation['resource_revision']) is not int
            or mutation['resource_revision'] != request.expected_resource_revision + 1):
        return None
    return {'code': 'EXIT_UNCONFIRMED', 'exit_confirmed': False, 'mutation': dict(mutation)}
