"""Updater authority stays attached to the original user operation."""
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common import updater, upgrade_executor


def test_revoked_manual_actions_do_not_reach_configuration_or_executor(monkeypatch):
    service = updater.UpdaterService()
    load = Mock(side_effect=AssertionError('unauthorized configuration read'))
    monkeypatch.setattr(service, '_load_config', load)
    for action in (service.check, service.start_download, service.start_upgrade):
        with pytest.raises(PermissionError):
            action(authorize=lambda: False)
    load.assert_not_called()


def test_desktop_revoke_removes_partial_without_publishing(monkeypatch, tmp_path):
    statuses = []
    live = True
    executor = upgrade_executor.DesktopExecutor(
        {'timeout_seconds': 1, 'download_url': 'https://example.invalid', 'asset_name': 'test.bin'}, statuses.append)
    executor.set_authority(lambda: live)
    monkeypatch.setattr(upgrade_executor, '_updates_dir', lambda: tmp_path)
    monkeypatch.setattr(executor, '_download_headers', lambda: {})
    def download(url, destination, headers, timeout):
        nonlocal live
        destination.write_bytes(b'synthetic-update')
        live = False
    monkeypatch.setattr(executor, '_download_file', download)
    executor.install()
    assert not list(tmp_path.iterdir())
    assert statuses[-1]['state'] == 'error'


def test_pip_revoke_stops_actual_owned_process(monkeypatch, tmp_path):
    # The real subprocess only sleeps. No package install or host restart occurs.
    statuses = []
    live = threading.Event()
    live.set()
    started = tmp_path / 'started'
    executor = upgrade_executor.PipExecutor({'timeout_seconds': 1}, statuses.append)
    executor.set_authority(live.is_set)
    monkeypatch.setattr(executor, '_check_editable_install', lambda _: None)
    monkeypatch.setattr(executor, '_build_install_args', lambda *a: [sys.executable, '-I', '-S', '-c',
        'import os,time,pathlib; pathlib.Path(' + repr(str(started)) + ').write_text(str(os.getpid())); time.sleep(30)'])
    task = threading.Thread(target=executor.install)
    task.start()
    try:
        import time
        for _ in range(200):
            if started.exists():
                break
            time.sleep(.01)
        assert started.exists()
        pid = int(started.read_text())
        live.clear()
        task.join(timeout=5)
        assert not task.is_alive()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert statuses[-1]['state'] == 'update_available'
        assert 'authorized' in statuses[-1]['error']
    finally:
        live.clear()
        task.join(timeout=5)


def test_restart_timer_rechecks_before_signalling_host(monkeypatch):
    service = updater.UpdaterService()
    service._status.install_mode = 'pip'
    live = True
    monkeypatch.setattr(service, '_load_config', lambda: {})
    executor = Mock()
    monkeypatch.setattr(updater, 'create_executor', lambda *a: executor)
    timers = []
    monkeypatch.setattr(updater.threading, 'Timer', lambda seconds, callback: SimpleNamespace(start=lambda: timers.append(callback)))
    kill = Mock(side_effect=AssertionError('host must not be killed'))
    monkeypatch.setattr(updater.os, 'kill', kill)
    service.start_upgrade(authorize=lambda: live)
    live = False
    timers[0]()
    kill.assert_not_called()
    executor.cancel_restart.assert_called_once()
    assert service.get_status()['state'] == 'error'


def test_restart_helper_does_not_restart_if_original_parent_lives(monkeypatch):
    from jiuwenswarm.common import updater_restart_helper as helper
    monkeypatch.setattr(sys, 'argv', ['helper', '--restart-json', '/missing/owned.json', '--parent-pid', '123'])
    monkeypatch.setattr(helper, 'wait_for_pid_exit', lambda *a, **k: False)
    read = Mock(side_effect=AssertionError('restart data must not be consumed'))
    monkeypatch.setattr(Path, 'is_file', read)
    helper.main()
    read.assert_not_called()
