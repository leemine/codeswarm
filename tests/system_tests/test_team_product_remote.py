"""Opt-in full App / independent AgentServer / Gateway Team remote acceptance.

Only isolated product configuration is supplied. No Runtime, admission, model,
Team factory, reviewer, or frontend store is replaced.
"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system, pytest.mark.skipif(
    os.environ.get('RUN_TEAM_PRODUCT_REMOTE') != '1', reason='real Team product acceptance is opt-in')]

def configure_team_product(monkeypatch, chrome, *, approvals=False):
    import yaml
    from . import test_heartbeat_channels_remote as support
    from .test_browser_product_remote import _configure
    _configure(monkeypatch, chrome)
    original = support.configure_workspace
    def configure(data, selected):
        root, profile = original(data, selected)
        path=data/'config/config.yaml'; config=yaml.safe_load(path.read_text())
        if selected == 'opencode':
            settings = config['execution']['profiles'][profile]['provider_config']
            settings['turn_timeout_s'] = 600
            settings['model'].update(context_window=131072, max_output_tokens=16384)
        config['modes']['team']={'acceptance': {
            'team_name':'acceptance', 'lifecycle':'persistent', 'spawn_mode':'inprocess',
            'dispatch_mode':'scheduled', 'enable_task_verification':True,
            'enable_swarmflow':False, 'enable_hitt':False, 'evolution_enabled':False,
            'worktree':{'enabled':False}, 'workspace':{'enabled':False},
            'transport':{'type':'inprocess'}, 'storage':{'type':'sqlite'},
            'leader':{'member_name':'team-leader','display_name':'验收组长'},
            'agents':{'leader':{},'teammate':{}},
        }}
        if approvals and profile:
            config.setdefault("permissions", {})["enabled"] = True
            selected_profile = config['execution']['profiles'][profile]
            selected_profile['authorization'] = {'full_access': False}
            if selected == 'codex':
                import openai_codex as sdk
                settings = selected_profile['provider_config']
                settings['mcp_default_tools_approval_mode'] = 'prompt'
                codex_home = Path(settings['env']['CODEX_HOME'])
                binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
                readable = {':minimal': 'read', str(root): 'write', str(codex_home / 'tmp'): 'read', str(Path(binary).parent): 'read'}
                (codex_home / 'config.toml').write_text(
                    'default_permissions = "team-acceptance"\n[permissions.team-acceptance.filesystem]\n'
                    + '\n'.join(f'{json.dumps(path)} = {json.dumps(access)}' for path, access in readable.items())
                    + '\n[permissions.team-acceptance.network]\nenabled=false\n')
        path.write_text(yaml.safe_dump(config,allow_unicode=True,sort_keys=False));path.chmod(0o600)
        return root,profile
    monkeypatch.setattr(support,'configure_workspace',configure)


@pytest.mark.asyncio
@pytest.mark.timeout(900)
@pytest.mark.parametrize('provider', ['native', 'codex', 'opencode'])
@pytest.mark.parametrize('work_mode', ['work', 'code'])
async def test_team_task_review_full_product(tmp_path, monkeypatch, provider, work_mode):
    from playwright.async_api import async_playwright
    from .goal_browser_remote_support import browser_services, preflight, observe_browser_channel, wait_code_mode_complete
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    assert chrome
    output = Path(os.environ.get('TEAM_PRODUCT_EVIDENCE_DIR', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    scope = Path(tempfile.mkdtemp(prefix=f'{provider}-{work_mode}-', dir=output))
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(scope/'data'))
    monkeypatch.setenv('JIUWENSWARM_CONFIG_DIR', str(scope/'data/config'))
    preflight(provider)
    configure_team_product(monkeypatch, chrome)
    evidence={'provider':provider,'work_mode':work_mode,'model':os.environ['HEARTBEAT_REMOTE_MODEL'],
              'events':[],'page_errors':[],'checks':[]}
    state={}
    def observe(socket):
        def receive(raw):
            try:frame=json.loads(raw)
            except (ValueError,TypeError):return
            payload=frame.get('payload') or {}
            if not isinstance(payload,dict):return
            if isinstance(payload.get('project'),dict) and payload['project'].get('project_dir'):
                state['project'] = payload['project']
            event=frame.get('event','') or payload.get('event_type','')
            if event:
                evidence['events'].append({'event':event,'payload':payload})
                if payload.get('session_id'):state['session_id']=payload['session_id']
                if event=='chat.error':state['error']=payload
                if event=='chat.final':state['final']=True
        socket.on('framereceived',receive)
    try:
        async with browser_services(scope,provider) as (url,data):
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path=chrome,headless=True)
                page=await browser.new_page(viewport={'width':1440,'height':1000})
                page.set_default_timeout(30000)
                page.on('pageerror',lambda error:evidence['page_errors'].append(str(error)))
                page.on('websocket',observe);observe_browser_channel(page,evidence)
                try:
                    await page.goto(url,wait_until='domcontentloaded')
                    await page.get_by_test_id('model-setup-guide-skip').click()
                    if await page.get_by_test_id('multi-session-work-mode-label').get_attribute('data-variant')!=work_mode:
                        await page.get_by_test_id('multi-session-work-mode-trigger').click()
                        await page.get_by_test_id(f'multi-session-work-mode-menu-{work_mode}').click()
                        if work_mode=='code':await wait_code_mode_complete(evidence,0)
                    await page.get_by_test_id('multi-session-new-project-button').click()
                    await page.get_by_test_id('multi-session-project-create-menu-blank').click()
                    await page.get_by_test_id('multi-session-project-create-dialog-name').fill(f'R1-11F {provider} {work_mode}')
                    await page.get_by_test_id('multi-session-project-create-dialog-confirm').click()
                    async with asyncio.timeout(30):
                        while 'project' not in state:
                            await asyncio.sleep(.05)
                    artifact = Path(state['project']['project_dir']) / 'team-result.txt'
                    await page.get_by_test_id('chat-panel-mode-select-trigger').click()
                    await page.locator('[data-testid="chat-panel-mode-select-option"][data-variant="team"]').click()
                    evidence['checks'].append('full App selected Team mode')
                    query=('完成一个最小团队验收任务。使用 build_team 创建且仅创建一个成员 worker，任务由 worker 执行。'
                           '使用 create_task 创建且仅创建一个任务：计算 17+25 并报告 42，明确指派给 worker，'
                           '指定 reviewer 名为 reviewer 检查结果。使用 scheduled 调度，不要自行执行成员工作，'
                           f'worker 必须使用文件工具把计算结果写入 {artifact}，文件内容恰好为 17+25=42，'
                           '并用 send_file_to_user 将该文件作为产物交付；reviewer 读取该实际文件并核验内容后投票。'
                           '等待任务审查通过后再向用户回复 R1-11F-TEAM-VERIFIED-42。不要询问用户，不要联网或安装软件。')
                    await page.get_by_test_id('chat-panel-input').fill(query)
                    await page.get_by_test_id('chat-panel-input-send').click()
                    leader_result = page.locator(
                        '[data-testid="chat-panel-team-leader-message-plain"], '
                        '[data-testid="chat-panel-message-bubble"][data-variant="assistant"]'
                    ).filter(has_text='R1-11F-TEAM-VERIFIED-42')
                    async with asyncio.timeout(650):
                        while True:
                            assert not state.get('error'), 'Product chat.error: '+str(state.get('error'))
                            rows=evidence['events']
                            assert not any(r['payload'].get('terminal_status') == 'failed' for r in rows), 'Team member failed; inspect product evidence'
                            db = data / '.agent_teams/team.db'
                            complete = False
                            if db.exists():
                                with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
                                    names = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
                                    task_tables = [name for name in names if name.startswith('team_task_') and not name.startswith('team_task_dependency_')]
                                    statuses = [r[0] for name in task_tables for r in conn.execute(f'SELECT status FROM "{name}"')]
                                    complete = statuses == ['completed']
                            leader_output = ''.join(str(row['payload'].get('content', '')) for row in rows
                                if row['event'] in {'chat.delta', 'chat.final'} and row['payload'].get('role') == 'leader')
                            if (complete and 'R1-11F-TEAM-VERIFIED-42' in leader_output
                                    and await leader_result.count()):
                                break
                            await asyncio.sleep(.5)
                    async def inspect_reviewer_ui():
                        expand = page.get_by_test_id('tool-panel-team-members-expand-button')
                        if await expand.count() and await expand.is_visible():
                            await expand.click()
                        await page.locator('[data-testid="team-area-member-item"][data-variant="worker"]').click()
                        if provider != 'native':
                            review_rows = page.get_by_test_id('team-area-process-item').filter(
                                has=page.get_by_test_id('team-area-process-item-title').filter(has_text='审查 ·'))
                            await review_rows.first.wait_for(state='visible')
                            await review_rows.first.get_by_test_id('team-area-process-item-toggle').click()
                            detail = page.get_by_test_id('team-area-process-detail')
                            await detail.wait_for(state='visible')
                            assert provider in await detail.inner_text()
                            evidence['checks'].append('original ToolPanel reviewer detail visible')
                    await inspect_reviewer_ui()
                    await page.screenshot(path=str(scope/'live.png'),full_page=True)
                    assert artifact.read_text().strip() == '17+25=42'
                    assert any(row['event'] == 'chat.file' for row in evidence['events']), 'No product artifact delivery'
                    evidence['artifact'] = str(artifact)
                    evidence['checks'].append('leader final marker and delivered artifact')
                    db = data / '.agent_teams/team.db'
                    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
                        tables = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
                        task_tables = [name for name in tables if name.startswith('team_task_') and not name.startswith('team_task_dependency_')]
                        vote_tables = [name for name in tables if name.startswith('team_review_vote_')]
                        tasks = [row for name in task_tables for row in conn.execute(f'SELECT task_id,status,assignee,review_round FROM "{name}"')]
                        votes = sum(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in vote_tables)
                        assert len(tasks) == 1 and tasks[0][1] == 'completed' and tasks[0][2] == 'worker', tasks
                        assert votes == 1, votes
                        evidence['tasks'] = tasks; evidence['vote_count'] = votes
                    if provider != 'native':
                        history_path = next((data/'agent/sessions').glob('*/history.jsonl'))
                        history = [json.loads(line) for line in history_path.read_text().splitlines()]
                        reviews = [row for row in history if row.get('execution_kind') == 'scheduled_review']
                        assert reviews and {row.get('provider_id') for row in reviews} == {provider}
                        assert any(row.get('event_type') == 'chat.tool_result' and 'Vote recorded' in str(row) for row in reviews)
                        assert not any('Product tool execution failed' in str(row) for row in history)
                        assert {row.get('provider_id') for row in history if row.get('member_session_id')} == {provider}
                        evidence['review_events'] = len(reviews)
                    evidence['checks'].append('one completed worker task, one review vote, selected Provider histories')
                    await page.reload(wait_until='domcontentloaded')
                    await asyncio.sleep(2)
                    await inspect_reviewer_ui()
                    async with asyncio.timeout(45):
                        while not await leader_result.count():
                            await asyncio.sleep(.25)
                    evidence['checks'].append('leader result restored after full App reload')
                    await page.screenshot(path=str(scope/'reloaded.png'),full_page=True)
                    assert not evidence['page_errors']
                    start = len(evidence['events'])
                    await page.get_by_test_id('chat-panel-input').fill(
                        '开始新一轮：分步分析从 1 到 10000 的平方和，不调用工具，先说明计算计划。')
                    await page.get_by_test_id('chat-panel-input-send').click()
                    async with asyncio.timeout(90):
                        while not any(row['event'] in {'chat.reasoning', 'chat.delta'} for row in evidence['events'][start:]):
                            await asyncio.sleep(.1)
                    stop = page.locator('[data-testid="chat-panel-input-send"][data-variant="stop"]')
                    await stop.click()
                    async with asyncio.timeout(60):
                        while not any(row['event'] == 'chat.interrupt_result'
                                      and row['payload'].get('intent') == ('pause' if provider == 'native' else 'cancel')
                                      and row['payload'].get('success') is True for row in evidence['events'][start:]):
                            await asyncio.sleep(.1)
                    evidence['checks'].append('active subsequent Team round controlled from original UI')
                finally:
                    await page.screenshot(path=str(scope/'last.png'),full_page=True)
                    (scope/'last-page.txt').write_text(await page.locator('body').inner_text())
                    await browser.close()
    finally:
        (scope/'product.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
