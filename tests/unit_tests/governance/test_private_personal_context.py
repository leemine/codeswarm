from dataclasses import replace
from pathlib import Path

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.personal_context import personal_context_home
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI

ALICE = TrustedIdentity('alice', 'alice', 'test:private-context')
BOB = TrustedIdentity('bob', 'bob', 'test:private-context')


@pytest.fixture
def private(monkeypatch, tmp_path):
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: object())
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    return tmp_path


def test_identity_names_cannot_alias_or_escape_homes(private):
    identities = [ALICE, BOB, replace(ALICE, authority='other'),
                  replace(ALICE, subject_id='other'), replace(ALICE, actor_id='../../bob')]
    homes = [personal_context_home(i) for i in identities]
    assert len(set(homes)) == len(identities)
    assert all(p.is_relative_to(private / '.jiuwenswarm' / '.personal_context' / 'subjects') for p in homes)
    assert personal_context_home(ALICE) == homes[0]


def test_authenticated_missing_identity_never_uses_legacy_home(private, monkeypatch):
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.current_identity', lambda: None)
    with pytest.raises(PermissionError):
        personal_context_home()


def test_private_home_symlink_rejected(private):
    path = personal_context_home(ALICE)
    path.parent.mkdir(parents=True)
    path.symlink_to(personal_context_home(BOB))
    with pytest.raises(PermissionError):
        personal_context_home(ALICE)


@pytest.mark.asyncio
async def test_two_hosts_read_only_their_published_graph(private):
    hosts = []
    for identity in (ALICE, BOB):
        home = personal_context_home(identity)
        context = home / 'workspace' / 'context'
        context.mkdir(parents=True)
        (context / 'description.md').write_text('# ' + identity.actor_id)
        hosts.append(PersonalContextHostAPI(home=home))
    for host, identity, other in [(hosts[0], ALICE, BOB), (hosts[1], BOB, ALICE)]:
        await host.start(activate_collection=False)
        page = await host.get_graph_page('page:description.md')
        assert identity.actor_id in str(page)
        assert other.actor_id not in str(page)


def test_noauth_home_is_unchanged(private, monkeypatch):
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: None)
    assert personal_context_home() == private / '.jiuwenswarm' / '.personal_context'


@pytest.mark.asyncio
async def test_native_mounts_current_private_home_and_detaches_without_identity(private, monkeypatch):
    from tests.unit_tests.agentserver.test_personal_context_rail_registration import _adapter, _FakeAgent
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    current = [ALICE]
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.current_identity', lambda: current[0])
    class Rail:
        def __init__(self, home):
            self.home = home
    monkeypatch.setattr(interface_deep, 'PersonalContextRail', Rail)
    agent = _FakeAgent()
    adapter = _adapter(agent, runtime_enabled=False)
    await adapter._sync_personal_context_rail('agent.work.normal')
    first = adapter._personal_context_rail
    assert first.home == personal_context_home(ALICE)
    current[0] = BOB
    await adapter._sync_personal_context_rail('agent.work.normal')
    assert adapter._personal_context_rail.home == personal_context_home(BOB)
    assert first in agent.unregister_attempts
    current[0] = None
    await adapter._sync_personal_context_rail('agent.work.normal')
    assert adapter._personal_context_rail is None


def test_personal_context_rejects_scope_injection_and_identity_revocation(private):
    from jiuwenswarm.governance.application_boundary import admit_application_request
    current = [ALICE]
    for method in ['personal_context.runtime.status', 'personal_context.runtime.get_config',
                   'personal_context.context.stream_graph']:
        permit = admit_application_request(method, {}, identity_resolver=lambda: current[0])
        assert permit.revalidate()
        with pytest.raises(PermissionError):
            admit_application_request(method, {'home': '/other'}, identity_resolver=lambda: current[0])
        current[0] = None
        assert not permit.revalidate()
        current[0] = ALICE
