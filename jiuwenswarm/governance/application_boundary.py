"""Application-resource authorization, independent of Session sharing.

Only explicitly classified application operations enter this boundary. Session,
project and sharing requests retain their existing owner/resource authorities.
The policy supplier is host-owned; wire parameters never select a principal or
supply grants. Unclassified operations remain unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .contracts import TrustedIdentity
from .instance_access import MCP_OWNER_METHODS
from .rsi_boundary import (
    RSI_READ_METHODS, RSI_OWNER_METHODS, RSI_TASK_OPERATIONS,
    experiment_owner_check, experiment_store, is_instance_owner,
)
from .personal_context import PERSONAL_CONTEXT_READS, PERSONAL_CONTEXT_WRITES, PERSONAL_CONTEXT_METHODS
from .session_sharing import SessionSharingDenied


@dataclass(frozen=True)
class ApplicationRule:
    resource: str
    action: str
    public_projection: bool = False


# These DTOs include instance credentials; only the settings maintainer may
# inspect them. Listing a channel ID does not grant its configuration scope.
CHANNEL_CONFIG_READS = frozenset(
    f'channel.{name}.get_conf' for name in (
        'feishu', 'xiaoyi', 'telegram', 'dingtalk', 'whatsapp', 'discord',
        'slack', 'wecom', 'wechat',
    )
)
INSTANCE_CONFIG_READS = CHANNEL_CONFIG_READS | frozenset({
    'updater.get_status', 'updater.get_conf', 'channel.wechat.get_login_ui',
})
CHANNEL_CONFIG_WRITES = frozenset(method.replace('.get_conf', '.set_conf')
                                 for method in CHANNEL_CONFIG_READS) | frozenset({'channel.wechat.unbind'})
UPDATER_OPERATIONS = frozenset({'updater.check', 'updater.download', 'updater.upgrade',
                                'updater.reset_source', 'updater.set_conf'})


# Shared backend configuration is an operator resource, not every login's data.
# Public projections are explicit DTOs; they never call the unrestricted editor.
RULES = {
    method: ApplicationRule(resource, action, public)
    for resource, action, public, methods in (
        ('catalog', 'read', True, ('plugin_packages.list', 'agent_templates.list', 'agent_groups.list',
                                  'mcp.list', 'skills.list')),
        ('settings', 'read', True, ('config.get', 'models.list', 'path.get', 'locale.get_conf', 'vendors.list')),
        ('settings', 'manage', False, ('config.set', 'config.save_all', 'models.replace_all',
                                     'models.validate', 'config.validate_model', 'path.set', 'locale.set_conf',
                                     'hooks.list', 'agent.reload_config')),
        ('extensions', 'manage', False, ('plugin_packages.install', 'plugin_packages.uninstall',
                                       'agent_templates.install', 'agent_templates.uninstall',
                                       'agent_groups.install', 'agent_groups.uninstall',
                                       'extensions.list', 'extensions.import', 'extensions.delete',
                                       'extensions.toggle', 'skills.toggle',
                                       'plugin_packages.show', 'agent_templates.show',
                                       'agent_templates.file.list', 'agent_templates.file.read',
                                       'agent_groups.show', 'agent_groups.file.list',
                                       'agent_groups.file.read', 'mcp.show')),
        ('instance_mcp', 'manage', False, MCP_OWNER_METHODS),
        ('metadata', 'read', True, ('channel.get',)),
        ('settings', 'manage', False, INSTANCE_CONFIG_READS),
        ('settings', 'manage', False, CHANNEL_CONFIG_WRITES | UPDATER_OPERATIONS),
        ('cron', 'list_owned', False, ('cron.job.list',)),
        ('rsi', 'list_owned', False, ('rsi.task.list',)),
        ('rsi', 'read_owned', False, RSI_READ_METHODS),
        ('rsi', 'instance_execute', False, RSI_OWNER_METHODS),
        ('personal_context', 'read_owned', False, PERSONAL_CONTEXT_READS),
        ('personal_context', 'manage_owned', False, PERSONAL_CONTEXT_WRITES),
        ('hub_catalog', 'read', True, ('skills.swarmskillshub.recommend',)),
        ('project_lifecycle', 'list_owned', False, ('project.lifecycle',)),
    )
    for method in methods
}


def host_application_policy(identity: TrustedIdentity) -> dict:
    """Compatibility composition over the host config, not credential records.

    Permissions belong to durable subjects. Token rotation/logout must never
    assign a new role. A future host can inject another policy supplier without
    changing rules, permits or the Session sharing implementation.
    """
    from .organization_auth import configured_authenticator
    auth = configured_authenticator()
    if auth is None or not auth.known_actor(identity):
        raise PermissionError('application identity unavailable')
    config = auth._config()
    policy = config.get('application_access', {})
    if not isinstance(policy, dict):
        raise PermissionError('invalid application access policy')
    entry = policy.get(identity.actor_id, {})
    if not isinstance(entry, dict):
        raise PermissionError('invalid application access policy')
    if is_instance_owner(identity):
        entry = dict(entry)
        for resource in ('settings', 'extensions'):
            entry[resource] = sorted(set(entry.get(resource, [])) | {'manage'})
    return entry


def admit_application_request(method: str, params: dict, *, identity_resolver: Callable,
                              host=None, envelope_session=None,
                              policy_supplier=host_application_policy, rsi_store=None):
    """Select an application or Session authority and retain delivery checks."""
    from .session_boundary import SessionRequestPermit, admit_session_request
    rule = RULES.get(method)
    if rule is None:
        if host is None:
            raise SessionSharingDenied("Session authority unavailable")
        return admit_session_request(method, params, identity_resolver=identity_resolver,
                                     host=host, envelope_session=envelope_session)
    identity = identity_resolver()
    if not isinstance(identity, TrustedIdentity) or not isinstance(params, dict):
        raise SessionSharingDenied('authenticated application request required')
    if any(key in params for key in ('user_id', 'actor_id', 'authority', 'share_id')):
        raise SessionSharingDenied('application identity cannot be supplied by the caller')
    if method in RSI_OWNER_METHODS:
        if any(k.startswith('_') or k in {'owner_identity', 'subject_id'} for k in params):
            raise SessionSharingDenied('experiment authority cannot be supplied by the caller')
        if not is_instance_owner(identity):
            raise SessionSharingDenied('explicit instance owner required')
        if method in RSI_TASK_OPERATIONS:
            if not experiment_owner_check(params.get('task_id'), identity, store=rsi_store):
                raise SessionSharingDenied('Experiment unavailable')
        if method == 'rsi.task.delete' and set(params) - {'task_id', 'session_id'}:
            raise SessionSharingDenied('exact experiment deletion required')
    if method in PERSONAL_CONTEXT_METHODS:
        if set(params) - (PERSONAL_CONTEXT_METHODS[method] | {'session_id'}):
            raise SessionSharingDenied('unsupported personal context selector')
    rsi_task_id = None
    if method in RSI_READ_METHODS:
        allowed = {'session_id', 'task_id'}
        if method.startswith('rsi.artifact.files.'):
            allowed.add('path')
        if set(params) - allowed or not isinstance(params.get('task_id'), str):
            raise SessionSharingDenied('exact experiment selector required')
        rsi_task_id = params['task_id']
    inventory_revision = None
    if method in INSTANCE_CONFIG_READS and params:
        raise SessionSharingDenied('instance configuration read accepts no selectors')
    if method == 'project.lifecycle':
        if (host is None or set(params) - {'events', 'inventory', 'project_id'}
                or sum(bool(params.get(k)) for k in ('events', 'inventory', 'project_id')) != 1
                or any(k in params and type(params[k]) is not bool for k in ('events', 'inventory'))):
            raise SessionSharingDenied('exact lifecycle read selector required')
        from .session_boundary import _inventory_revision
        inventory_revision = _inventory_revision(host)
    # Configuration reads are bounded DTOs; accepting arbitrary editor selectors
    # would silently change their audience and must not be treated as a read.
    if rule.public_projection and rule.resource in {'settings', 'metadata'} and params:
        raise SessionSharingDenied('configuration projection accepts no parameters')
    if rule.resource == 'catalog':
        if set(params) - {'filter', 'include_team_compatibility', 'cache_mode', 'refresh',
                          'with_installed', 'refresh_marketplaces', 'session_id'}:
            raise SessionSharingDenied('unsupported catalog selector')
        if params.get('filter') not in (None, 'builtin', 'local', 'mine', 'builtin+hub'):
            raise SessionSharingDenied('invalid catalog filter')
    if rule.resource == 'hub_catalog':
        if set(params) - {'session_id', 'top_k', 'limit', 'category_id', 'plugin_type',
                          'skill_type', 'language', 'locale', 'cache_mode', 'refresh'}:
            raise SessionSharingDenied('unsupported public Hub selector')
    management_resource = {'settings': 'settings', 'catalog': 'extensions'}.get(rule.resource)
    def can_manage():
        try:
            grants = policy_supplier(identity).get(management_resource, [])
            return isinstance(grants, list) and 'manage' in grants
        except Exception:
            return False
    initial_manage = can_manage() if management_resource else False
    if method == 'rsi.task.list':
        # RSI's browser session ID is a transport route, not a chat Session
        # whose sharing ACL authorizes the experiment inventory.
        if set(params) - {'session_id', 'scenario', 'artifact_type'}:
            raise SessionSharingDenied('unsupported experiment selector')
        rsi_policy = policy_supplier(identity).get('rsi', [])
        if not isinstance(rsi_policy, list):
            raise SessionSharingDenied('invalid experiment policy')
        rsi_policy = tuple(rsi_policy)

    def check():
        try:
            if identity_resolver() != identity:
                return False
            if management_resource and can_manage() != initial_manage:
                return False
            if method in MCP_OWNER_METHODS:
                return is_instance_owner(identity)
            if method in PERSONAL_CONTEXT_METHODS:
                return True
            if method in RSI_OWNER_METHODS:
                if not is_instance_owner(identity):
                    return False
                if method not in RSI_TASK_OPERATIONS:
                    return True
                if experiment_owner_check(params.get('task_id'), identity, store=rsi_store):
                    return True
                if method == 'rsi.task.delete':
                    # This exact task was owned at admission. Deleting it must
                    # not erase the authority to deliver its own receipt.
                    store = rsi_store if rsi_store is not None else experiment_store()
                    target = store.task_dir(store.tasks_root, params['task_id'])
                    return not target.exists()
                return False
            if method in RSI_READ_METHODS:
                return experiment_owner_check(rsi_task_id, identity, store=rsi_store)
            if method == 'rsi.task.list':
                # The consumer can include legacy unowned experiments only
                # for an explicit operator. Revoke that audience on delivery.
                return tuple(policy_supplier(identity).get('rsi', [])) == rsi_policy
            if rule.public_projection or rule.action == 'list_owned':
                return True
            policy = policy_supplier(identity)
            grants = policy.get(rule.resource, [])
            return (isinstance(grants, list) and rule.action in grants)
        except Exception:
            return False

    if not check():
        raise SessionSharingDenied('application resource permission required')
    # Reuse the established final-delivery permit rather than creating a second
    # outbound queue or bypassing its revoke/identity checks.
    return SessionRequestPermit(identity, identity_resolver, host, method=method,
                                application_check=check, inventory_revision=inventory_revision)


def builtin_catalog_projection(method: str, params: dict) -> dict | None:
    """Public installed-product cards, excluding private packages and paths."""
    rule = RULES.get(method)
    if rule is None or rule.resource != 'catalog':
        return None
    from .organization_auth import current_identity
    if application_can_manage(current_identity(), 'extensions'):
        return None  # Existing instance editor; its admitted permit rechecks this role.
    from jiuwenswarm.server.runtime import extension_package_manager as packages
    if params.get('filter') in {'local', 'mine'}:
        key = {'mcp.list': 'items', 'skills.list': 'skills', 'plugin_packages.list': 'packages',
               'agent_templates.list': 'templates', 'agent_groups.list': 'agentGroups'}[method]
        return {key: [], 'read_only': True, 'scope': 'builtin'}
    if method == 'mcp.list':
        from jiuwenswarm.server.runtime.mcp.registry import list_marketplace_mcps
        fields = {'name', 'display_name', 'description', 'category', 'integration_type',
                  'has_bundled_skills', 'source', 'id', 'package_name'}
        cards = list_marketplace_mcps('builtin')
        # No remote catalog fetches, custom endpoints, tokens or connection
        # state are inferred as this account's configuration.
        return {'type': 'list', 'items': [
            {**{k: v for k, v in card.items() if k in fields}, 'read_only': True,
             'installed': False, 'connection_state': 'disconnected'}
            for card in cards if card.get('source') == 'built_in'
        ], 'read_only': True, 'scope': 'builtin'}
    if method == 'skills.list':
        from jiuwenswarm.common.utils import get_builtin_skills_dir
        from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
        root = get_builtin_skills_dir()
        cards = []
        for directory in sorted(root.iterdir()) if root.is_dir() else ():
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith('_'):
                continue
            path = SkillManager._try_find_skill_file(directory)
            if path is None or path.is_symlink():
                continue
            meta = SkillManager._parse_skill_md(path)
            if meta:
                cards.append({**{k: meta[k] for k in ('name', 'description') if k in meta},
                              'name': directory.name, 'source': 'builtin', 'is_builtin': True,
                              'installed': False, 'read_only': True})
        return {'skills': cards, 'plugins': [], 'read_only': True, 'scope': 'builtin'}
    readers = {
        'plugin_packages.list': ('packages', packages.list_plugin_packages),
        'agent_templates.list': ('templates', packages.list_agent_templates),
        'agent_groups.list': ('agentGroups', packages.list_agent_groups),
    }
    key, reader = readers[method]
    cards = reader({'filter': 'builtin', 'include_team_compatibility': params.get('include_team_compatibility') is True})
    allowed = {'id', 'name', 'displayName', 'displayDescription', 'category', 'tags',
               'source', 'installed', 'connection_state', 'teamCompatible'}
    result = []
    for card in cards:
        if card.get('source') != 'builtin':
            continue
        if params.get('filter') in {'local', 'mine'} and not card.get('installed'):
            continue
        result.append({**{k: v for k, v in card.items() if k in allowed}, 'read_only': True,
                       'installed': False, 'connection_state': 'disconnected'})
    return {key: result, 'read_only': True, 'scope': 'builtin'}


def application_can_manage(identity, resource: str) -> bool:
    """Explicit durable host grant; a login or sharing relationship is not a role."""
    if not isinstance(identity, TrustedIdentity):
        return False
    try:
        grants = host_application_policy(identity).get(resource, [])
        return isinstance(grants, list) and 'manage' in grants
    except Exception:
        return False


def require_application_consumer(method: str):
    """Recheck the original permit at a shared configuration consumer."""
    from .organization_auth import configured_authenticator
    if configured_authenticator() is not None:
        from .session_boundary import current_application_permit
        return current_application_permit(method)
    return None
