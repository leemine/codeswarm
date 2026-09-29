"""Opt-in original Web startup regression; no model task or real credentials."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system, pytest.mark.skipif(
    os.environ.get('RUN_WEB_STARTUP_LOCAL') != '1', reason='local Web startup opt-in',
)]


@pytest.mark.timeout(240)
async def test_disabled_feishu_does_not_import_sdk_or_block_code_startup(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    from .goal_browser_remote_support import (
        browser_services, observe_browser_channel, wait_code_mode_complete,
    )
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    assert chrome
    output = Path(os.environ.get('WEB_STARTUP_EVIDENCE_DIR', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    scope = Path(tempfile.mkdtemp(prefix='startup-', dir=output))
    for key, value in {
        'JIUWENSWARM_DATA_DIR': str(scope / 'data'),
        'JIUWENSWARM_CONFIG_DIR': str(scope / 'data/config'),
        'HEARTBEAT_REMOTE_API_BASE': 'http://127.0.0.1:9/v1',
        'HEARTBEAT_REMOTE_API_KEY': 'local-startup-test-placeholder',
        'HEARTBEAT_REMOTE_MODEL': 'unused-local-model',
        'PYTHONPROFILEIMPORTTIME': '1',
    }.items():
        monkeypatch.setenv(key, value)
    evidence = {}
    try:
        async with browser_services(scope, 'native') as (url, _data):
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=chrome, headless=True)
                try:
                    page = await browser.new_page()
                    observe_browser_channel(page, evidence)
                    await page.goto(url)
                    skip = page.get_by_test_id('model-setup-guide-skip')
                    await skip.wait_for(state='visible', timeout=15_000)
                    await skip.click()
                    mode = page.get_by_test_id('multi-session-work-mode-label')
                    if await mode.get_attribute('data-variant') != 'code':
                        await page.get_by_test_id('multi-session-work-mode-trigger').click()
                        await page.get_by_test_id('multi-session-work-mode-menu-code').click()
                    await wait_code_mode_complete(evidence, 0)
                    async with asyncio.timeout(30):
                        while 'channel_configuration_applied' not in (scope / 'gateway.log').read_text():
                            await asyncio.sleep(.1)
                    log = (scope / 'gateway.log').read_text()
                    for module in ('lark_oapi', 'feishu_connect'):
                        assert not any(module in line for line in log.splitlines()
                                       if line.startswith('import time:')), module
                    evidence['passed'] = True
                finally:
                    await browser.close()
    finally:
        (scope / 'result.json').write_text(json.dumps(evidence, indent=2))
