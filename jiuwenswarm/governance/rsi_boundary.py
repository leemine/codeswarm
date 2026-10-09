"""Experiment ownership is independent of chat routing and sharing grants."""
from dataclasses import asdict

from .session_sharing import SessionSharingDenied


RSI_READ_METHODS = frozenset({
    'rsi.task.get', 'rsi.report.get', 'rsi.usage.get', 'rsi.tree.get',
    'rsi.artifact.files.list', 'rsi.artifact.files.get',
})

# Explicitly entrusted instance execution is separate from catalog management
# and experiment ownership. Ordinary accounts retain private reads only.
RSI_OWNER_METHODS = frozenset({
    'rsi.dataset.validate', 'rsi.task.create', 'rsi.task.delete',
    'rsi.training.start', 'rsi.training.pause', 'rsi.training.resume',
    'rsi.training.terminate', 'rsi.artifact.download', 'rsi.harness.install',
    'rsi.harness.versions.list', 'rsi.harness.rollback',
})
RSI_TASK_OPERATIONS = RSI_OWNER_METHODS - {
    'rsi.dataset.validate', 'rsi.task.create',
    'rsi.harness.versions.list', 'rsi.harness.rollback',
}


# Compatibility export: RSI and instance MCP share the same host owner policy.
from .instance_access import is_instance_owner


def experiment_store():
    from jiuwenswarm.agents.harness.common.rsi.context import get_rsi_workspace_root
    from jiuwenswarm.agents.harness.common.rsi.task_store import RsiTaskStore
    return RsiTaskStore(get_rsi_workspace_root() / 'tasks')


def experiment_owner_check(task_id, identity, *, store=None):
    """Read original durable ownership again at admission and final delivery."""
    if store is None:
        store = experiment_store()
    try:
        task = store.get(task_id)
        return task.task_id == task_id and task.owner_identity == asdict(identity)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    except Exception:
        # Missing and inaccessible experiments have the same public outcome.
        return False


def require_experiment_owner(method, params, store):
    from .session_boundary import current_application_permit
    permit = current_application_permit(method)
    if not experiment_owner_check(params.get('task_id'), permit.identity, store=store):
        raise SessionSharingDenied('Experiment unavailable')
    return permit


def instance_model_snapshot(client, request):
    """Materialize one configured model for the entrusted RSI file consumer.

    RSI consumes private YAML snapshots, not Native's live Model callbacks.
    This is restricted to explicit instance-owner creation; the Worker retains
    its original execution/revocation checks. No ordinary account gets a key.
    """
    from .session_boundary import current_application_permit
    from .model_credentials import (
        ConfiguredModelCredentialResolver, ModelCredentialBinding,
        configured_model_metadata, model_entry_fingerprint,
    )
    permit = current_application_permit('rsi.task.create')
    if not is_instance_owner(permit.identity):
        raise SessionSharingDenied('RSI model unavailable')
    fingerprint = model_entry_fingerprint(client, request)
    matches = [entry for entry in configured_model_metadata()
               if model_entry_fingerprint(entry['model_client_config'],
                                          entry['model_config_obj']) == fingerprint]
    if len(matches) != 1:
        raise SessionSharingDenied('RSI model selection changed')
    binding = ModelCredentialBinding.from_config(client)

    def decode(value):
        from jiuwenswarm.extensions.registry import ExtensionRegistry
        crypto = ExtensionRegistry.get_instance().get_crypto_provider()
        if crypto is None:
            raise SessionSharingDenied('RSI credential decoder unavailable')
        return crypto.decrypt(value)

    secret = ConfiguredModelCredentialResolver(
        binding, credential_decoder=decode).resolve_credential(binding.reference)
    if not permit.revalidate():
        raise SessionSharingDenied('RSI model authority revoked')
    return secret, permit.revalidate


RSI_EVENTS = frozenset({
    'rsi.training.status.changed', 'rsi.training.progress', 'rsi.training.tree.delta',
})


def experiment_event_delivery(frame, identity_resolver):
    """Private experiment pushes use task ownership, never chat subscriptions."""
    from .application_boundary import admit_application_request
    payload = frame.get('payload')
    if frame.get('event') not in RSI_EVENTS or not isinstance(payload, dict):
        raise SessionSharingDenied('Experiment event unavailable')
    permit = admit_application_request('rsi.task.get', {'task_id': payload.get('task_id')},
                                      identity_resolver=identity_resolver)
    # RSI events originate from the original Worker consumer. Its full progress
    # and tree DTO belongs only to the experiment owner, even after buffering.
    return frame, permit.revalidate
