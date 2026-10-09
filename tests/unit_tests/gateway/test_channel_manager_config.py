"""Regression coverage for atomic channel configuration updates."""

from __future__ import annotations

import pytest
import asyncio
from types import SimpleNamespace

from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager


class _MessageHandler:
    pass


async def test_set_conf_restores_last_working_config_when_callback_fails() -> None:
    async def reject_bad_config(config: dict) -> None:
        if config.get("bad"):
            raise RuntimeError("optional channel unavailable")

    manager = ChannelManager(
        _MessageHandler(),
        config={"working": {"enabled": True}},
        on_config_updated=reject_bad_config,
    )

    with pytest.raises(RuntimeError, match="optional channel unavailable"):
        await manager.set_conf("bad", {"enabled": True})

    assert manager.get_conf("working") == {"enabled": True}
    assert manager.get_conf("bad") == {}
    # The failed write is still observable to a background retry.  Otherwise a
    # user disabling the channel while that retry sleeps is indistinguishable
    # from the retry's own rollback-to-empty state.
    assert manager.get_conf_revision("bad") == 1


async def test_set_config_restores_last_working_snapshot_when_callback_fails() -> None:
    async def reject_new_config(config: dict) -> None:
        if "bad" in config:
            raise RuntimeError("optional channel unavailable")

    manager = ChannelManager(
        _MessageHandler(),
        config={"working": {"enabled": True}},
        on_config_updated=reject_new_config,
    )

    with pytest.raises(RuntimeError, match="optional channel unavailable"):
        await manager.set_config({"bad": {"enabled": True}})

    assert manager.get_conf("working") == {"enabled": True}
    assert manager.get_conf("bad") == {}
    assert manager.get_conf_revision("bad") == 1


async def test_config_revision_changes_when_a_user_disables_a_channel() -> None:
    manager = ChannelManager(_MessageHandler(), config={"telegram": {"enabled": True}})

    initial_revision = manager.get_conf_revision("telegram")
    await manager.set_conf("telegram", {})

    assert manager.get_conf("telegram") == {}
    assert manager.get_conf_revision("telegram") == initial_revision + 1


async def test_revoked_channel_update_cancels_consumer_before_restoring_previous():
    allowed = [True]
    started = asyncio.Event()
    events = []
    async def apply(config):
        if config['telegram']['enabled']:
            events.append('start-new')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                events.append('new-exited')
        else:
            events.append('restore-old')
    manager = ChannelManager(_MessageHandler(), config={'telegram': {'enabled': False}},
                             on_config_updated=apply)
    task = asyncio.create_task(manager.set_conf('telegram', {'enabled': True},
                                                authority_check=lambda: allowed[0]))
    await started.wait()
    allowed[0] = False
    with pytest.raises(PermissionError):
        await asyncio.wait_for(task, 2)
    assert events == ['start-new', 'new-exited', 'restore-old']
    assert manager.get_conf('telegram') == {'enabled': False}


async def test_revocation_after_stop_prevents_replacement_registration():
    allowed = [True]
    manager = None
    async def apply(config):
        if config['telegram']['enabled']:
            await asyncio.sleep(0)
            allowed[0] = False
            manager.register_channel(SimpleNamespace(channel_id='telegram'))
    manager = ChannelManager(_MessageHandler(), config={'telegram': {'enabled': False}},
                             on_config_updated=apply)
    with pytest.raises(PermissionError):
        await manager.set_conf('telegram', {'enabled': True}, authority_check=lambda: allowed[0])
    assert manager.enabled_channels == []
    assert manager.get_conf('telegram') == {'enabled': False}


async def test_channel_request_proof_is_released_after_configuration_transaction():
    from jiuwenswarm.gateway.channel_manager.channel_manager import _check_configuration_authority
    allowed = [True]
    release = asyncio.Event()
    async def lifetime():
        await release.wait()
        _check_configuration_authority()
    workers = []
    async def apply(_):
        workers.append(asyncio.create_task(lifetime()))
    manager = ChannelManager(_MessageHandler(), on_config_updated=apply)
    await manager.set_conf('telegram', {'enabled': True}, authority_check=lambda: allowed[0])
    allowed[0] = False
    release.set()
    await asyncio.gather(*workers)
