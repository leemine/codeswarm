"""Opt-in Browser product qualification with a real, explicitly supplied model.

RUN_BROWSER_PRODUCT_REMOTE=1 and HEARTBEAT_REMOTE_API_BASE/API_KEY/MODEL
are required. Only synthetic local site content is used. No user configuration
is discovered. Original Web UI, AgentServer, Gateway, Provider and Browser tools
perform the work; only the isolated test workspace configuration is customized.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system, pytest.mark.skipif(
    os.environ.get('RUN_BROWSER_PRODUCT_REMOTE') != '1', reason='remote Browser product qualification is opt-in',
)]


class _Page(BaseHTTPRequestHandler):
    visits: list[str] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).visits.append(self.path)
        download = self.path == '/result.txt'
        body = (b'R1-10F REAL BROWSER DOWNLOAD' if download else
                b'<!doctype html><title>Browser acceptance</title><h1>R1-10F LOCAL SITE</h1>'
                b'<a href="/result.txt" download>Download acceptance result</a>')
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream' if download else 'text/html')
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="result.txt"')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def synthetic_site():
    _Page.visits = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/', _Page.visits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _configure(monkeypatch, chrome: str):
    import yaml
    from . import test_heartbeat_channels_remote as support

    original = support.configure_workspace

    def configure(data: Path, provider: str):
        root, profile = original(data, provider if provider != 'opencode' else 'native')
        path = data / 'config/config.yaml'
        config = yaml.safe_load(path.read_text())
        config['browser'] = {'headless': True, 'chrome_path': chrome}
        if provider == 'opencode':
            (data / 'opencode-runtime').mkdir(mode=0o700)
            profile = 'r1-10f-opencode-remote'
            cli = os.environ.get('OPENCODE_OC1_CLI', os.path.expanduser('~/.opencode/bin/opencode'))
            assert Path(cli).is_file(), 'Explicit OpenCode opt-in requires its executable'
            config['execution'] = {'default_profile_id': profile, 'profiles': {profile: {
                'provider_id': 'opencode', 'config_revision': 'r1-10f-remote',
                'authorization': {'full_access': True}, 'provider_config': {
                    'cli_path': cli, 'runtime_root': str(data / 'opencode-runtime'),
                    'turn_timeout_s': 240,
                    'model': {'model': os.environ['HEARTBEAT_REMOTE_MODEL'], 'provider': 'r1_10f_remote',
                              'api_base': os.environ['HEARTBEAT_REMOTE_API_BASE'],
                              'api_key': os.environ['HEARTBEAT_REMOTE_API_KEY']},
                },
            }}}
        path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        path.chmod(0o600)
        return root, profile

    monkeypatch.setattr(support, 'configure_workspace', configure)


@pytest.mark.asyncio
@pytest.mark.timeout(600)
@pytest.mark.parametrize('provider', ['native', 'codex', 'opencode'])
async def test_remote_browser_download_original_ui(tmp_path, monkeypatch, synthetic_site, provider):
    from playwright.async_api import async_playwright, expect
    from .goal_browser_remote_support import browser_services, digest, preflight

    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    assert chrome, 'Explicit Browser opt-in requires Chrome'
    output = Path(os.environ.get('BROWSER_PRODUCT_EVIDENCE_DIR', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    scope = Path(tempfile.mkdtemp(prefix=f'browser-{provider}-', dir=output))
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(scope / 'data'))
    monkeypatch.setenv('JIUWENSWARM_CONFIG_DIR', str(scope / 'data/config'))
    monkeypatch.setenv('BROWSER_RUNTIME_MCP_ENABLED', '1')
    monkeypatch.setenv('BROWSER_DRIVER', 'managed')
    monkeypatch.setenv('BROWSER_MANAGED_BINARY', chrome)
    monkeypatch.setenv('BROWSER_MANAGED_ARGS', '--headless=new')
    for name in ('PLAYWRIGHT_MCP_COMMAND', 'PLAYWRIGHT_MCP_ARGS', 'PLAYWRIGHT_MCP_CDP_ENDPOINT',
                 'PLAYWRIGHT_CDP_URL', 'PLAYWRIGHT_MCP_TARGET_ID', 'PLAYWRIGHT_MCP_TARGET_RESOLVER'):
        monkeypatch.delenv(name, raising=False)
    dist = preflight(provider)
    _configure(monkeypatch, chrome)
    site, visits = synthetic_site
    evidence = {'provider': provider, 'model': os.environ['HEARTBEAT_REMOTE_MODEL'],
                'frontend_index_sha256': digest(dist / 'index.html'), 'checks': [], 'events': [], 'approvals': []}
    state = {'final': False}

    def observe(socket):
        def receive(raw):
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                return
            event = frame.get('event', '')
            payload = frame.get('payload') or {}
            if not isinstance(payload, dict):
                return
            if event.startswith('chat.'):
                evidence['events'].append({'event': event, 'session_id': payload.get('session_id'),
                                           'delivery_id': payload.get('delivery_id')})
            if event == 'chat.final':
                state['final'] = True
            if event == 'chat.error':
                state['error'] = True
            if event == 'chat.ask_user_question':
                state['question'] = payload
        socket.on('framereceived', receive)

    try:
        async with asyncio.timeout(540):
            async with browser_services(scope, provider) as (url, data):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(executable_path=chrome, headless=True)
                    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.set_default_timeout(30_000)
                    page.on('websocket', observe)
                    from .goal_browser_remote_support import observe_browser_channel, wait_code_mode_complete
                    observe_browser_channel(page, evidence)
                    try:
                        await page.goto(url, wait_until='domcontentloaded')
                        await page.get_by_test_id('model-setup-guide-skip').click()
                        mode = page.get_by_test_id('multi-session-work-mode-label')
                        if await mode.get_attribute('data-variant') != 'code':
                            await page.get_by_test_id('multi-session-work-mode-trigger').click()
                            await page.get_by_test_id('multi-session-work-mode-menu-code').click()
                            await wait_code_mode_complete(evidence, 0)
                        delegate = ('Use task_tool with subagent_type=browser_agent.' if provider == 'native' else
                                    'Use subagent_spawn with subagent_type=browser_agent. Wait for its result '
                                    'with subagent_wait, then close it with subagent_close after completion. '
                                    'Omit the optional browser_capabilities field; the core tool set suffices.')
                        query = (
                            f'{delegate} Delegate this exact task: use only Browser tools to visit {site}, '
                            'read its heading and click Download acceptance result. Return the downloaded file '
                            'to the user as an Artifact. Do not use shell, curl, fetch, Python or file-writing '
                            'tools to manufacture or download this file. Do not visit any other website. '
                            'Browser permission prompts will be answered by the user. '
                            'After completion report the heading and the download result. If the file was not '
                            'already delivered by the Browser gateway, use send_file_to_user with its actual path.'
                        )
                        await page.get_by_test_id('chat-panel-input').fill(query)
                        await page.get_by_test_id('chat-panel-input-send').click()
                        approved = set()
                        async with asyncio.timeout(400):
                            while not state['final']:
                                assert not state.get('error'), 'Product emitted chat.error; inspect isolated service logs'
                                prompt = page.get_by_test_id('interaction-slot-auth-prompt')
                                question = state.get('question', {})
                                request = question.get('request_id')
                                if await prompt.count() and request and request not in approved:
                                    names = [q.get('tool_name', '') for q in question.get('questions', [])]
                                    assert question.get('source') == 'browser_permission', 'Unexpected non-Browser approval'
                                    assert names and all(n in {'browser_profile_use', 'browser_navigate', 'browser_click',
                                        'browser_take_screenshot', 'browser_wait_for', 'browser_tabs', 'browser_close'} for n in names), names
                                    assert len(approved) < 12, 'Unexpected Browser action loop'
                                    await prompt.locator('[data-testid="interaction-slot-auth-action-button"][data-variant="allow-once"]').click()
                                    approved.add(request)
                                    evidence['approvals'].append({'request_id': request, 'tools': names})
                                await asyncio.sleep(.2)
                        session = await page.get_by_test_id('app-shell').get_attribute('data-session-id')
                        assert session and session != 'new'
                        evidence['session_id'] = session
                        if provider != 'native':
                            archives = []
                            for archive in (data / 'agent/sessions').rglob('*recovery*.json'):
                                record = json.loads(archive.read_text())
                                binding = record.get('binding', {})
                                if binding.get('host_session_id') == session or record.get('parent_session_id') == session:
                                    assert binding.get('provider_id') == provider
                                    archives.append({'session_id': binding.get('host_session_id'),
                                                     'parent_session_id': record.get('parent_session_id'),
                                                     'provider': binding.get('provider_id')})
                            assert any(a['parent_session_id'] == session for a in archives), 'No same-Provider Browser child archive'
                            evidence['bindings'] = archives
                        assert '/' in visits and '/result.txt' in visits, 'Real Browser did not visit and download'
                        if provider != 'native':
                            assert any('browser_profile_use' in a['tools'] for a in evidence['approvals'])
                        card = page.get_by_test_id('chat-panel-file-download-item')
                        await expect(card).to_have_count(1, timeout=30_000)
                        await page.reload(wait_until='domcontentloaded')
                        await expect(card).to_have_count(1, timeout=30_000)
                        async with page.expect_download() as result:
                            await card.get_by_test_id('chat-panel-file-download-btn').click()
                        download = await result.value
                        assert await download.failure() is None
                        assert Path(await download.path()).read_bytes() == b'R1-10F REAL BROWSER DOWNLOAD'
                        evidence['checks'].extend(['real_model_browser_navigation_download', 'unique_artifact_after_reload',
                                                   'original_ui_download_exact_bytes'])
                        await page.screenshot(path=str(scope / 'success.png'))
                    finally:
                        if not evidence['checks']:
                            await page.screenshot(path=str(scope / 'failure.png'))
                        await browser.close()
                        # Generated OpenCode directories can be sealed read-only;
                        # allow the existing cleanup helper to scrub only this scope.
                        for directory in (data / 'opencode-runtime').rglob('*'):
                            if directory.is_dir() and not directory.is_symlink():
                                directory.chmod(0o700)
        evidence['passed'] = True
    finally:
        evidence['site_visits'] = list(visits)
        (scope / 'result.json').write_text(json.dumps(evidence, indent=2))
