/* Existing persisted fixtures + real UI conflict/recovery and optional Native Provider. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const result = JSON.parse(fs.readFileSync('docs/taskboard/evidence/browser-result.json'));
  const b = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME_PATH || '/opt/google/chrome/chrome',
    args: ['--no-sandbox'],
  });
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  const checks = [];
  const errors = [];
  const finals = [];
  p.on('pageerror', (e) => errors.push(e.message));
  p.on('websocket', (ws) =>
    ws.on('framereceived', (e) => {
      try {
        const m = JSON.parse(e.payload);
        if (['chat.final', 'chat.error', 'execution.error'].includes(m.event)) finals.push(m);
      } catch {}
    }),
  );
  try {
    await p.addInitScript(() => localStorage.setItem('jiuwenswarm_work_mode', 'code'));
    await p.goto(result.base + '/taskboard/' + result.tasks[0]);
    await p.getByTestId('taskboard-result').waitFor({ timeout: 60000 });
    assert.equal(await p.getByTestId('taskboard-status').inputValue(), 'done');
    assert((await p.getByTestId('taskboard-result').inputValue()).includes('人工完成验证'));
    await p.waitForFunction(() => !document.querySelector('[data-testid="taskboard-complete"]')?.disabled);
    await p.screenshot({ path: 'docs/taskboard/evidence/02-task-detail.png' });
    console.log('PASS persisted local restart');
    checks.push('full local process restart preserves task, status, result and existing session link');
    await p.evaluate(async (taskId) => {
      const w = new WebSocket(`ws://${location.host}/ws`);
      await new Promise((r, j) => {
        w.onopen = r;
        w.onerror = j;
      });
      let seq = 0;
      window.acceptanceRpc = (method, params) =>
        new Promise((r, j) => {
          let id = 'followup-' + ++seq;
          let timer = setTimeout(() => j(new Error('RPC timeout ' + method)), 30000);
          let f = (e) => {
            let m = JSON.parse(e.data);
            if (m.id === id) {
              clearTimeout(timer);
              w.removeEventListener('message', f);
              m.ok ? r(m.payload) : j(new Error(JSON.stringify(m)));
            }
          };
          w.addEventListener('message', f);
          w.send(JSON.stringify({ type: 'req', id, method, params }));
        });
      const v = await window.acceptanceRpc('taskboard.get', { task_id: taskId });
      await window.acceptanceRpc('taskboard.update', {
        task_id: taskId,
        expected_version: v.task.version,
        patch: { priority: 'high' },
      });
    }, result.tasks[0]);
    await p.getByTestId('taskboard-result').fill('冲突时保留的结果草稿');
    await p.getByTestId('taskboard-save-result').click();
    await p.getByTestId('taskboard-detail-error').waitFor();
    assert.equal(await p.getByTestId('taskboard-result').inputValue(), '冲突时保留的结果草稿');
    await p.screenshot({ path: 'docs/taskboard/evidence/04-version-conflict.png' });
    p.once('dialog', (d) => d.accept());
    await p.getByTestId('taskboard-detail-retry').click();
    await p.waitForFunction(
      () =>
        document.querySelector('[data-testid="taskboard-priority"]')?.value === 'high' &&
        document.querySelector('[data-testid="taskboard-result"]')?.value.includes('人工完成验证'),
    );
    assert((await p.getByTestId('taskboard-result').inputValue()).includes('人工完成验证'));
    console.log('PASS concurrent writer conflict recovery');
    checks.push('second real RPC writer triggers conflict, UI preserves draft and reload recovers');
    await p.getByTestId('taskboard-open-session').click();
    await p.waitForURL('**/chat/' + result.refs.session);
    await p.getByTestId('app-return-taskboard').waitFor();
    await p.screenshot({ path: 'docs/taskboard/evidence/05-session-return.png' });
    if (process.env.REAL_PROVIDER === '1') {
      await p.getByTestId('chat-panel-input').fill('请仅回复：TASKBOARD_PROVIDER_OK。不要调用工具，不要修改文件。');
      await p.getByTestId('chat-panel-input-send').click();
      const deadline = Date.now() + 120000;
      while (
        !finals.some((m) => m.event === 'chat.final' && JSON.stringify(m.payload).includes('TASKBOARD_PROVIDER_OK'))
      ) {
        if (finals.some((m) => m.event === 'execution.error' || m.event === 'chat.error'))
          throw new Error(JSON.stringify(finals));
        if (Date.now() > deadline) throw new Error('No real Provider completion within 120 seconds');
        await p.waitForTimeout(250);
      }
      await p.waitForTimeout(1500);
      assert(
        finals.some((m) => m.event === 'chat.final' && JSON.stringify(m.payload).includes('TASKBOARD_PROVIDER_OK')),
        JSON.stringify(finals),
      );
      assert(!finals.some((m) => m.event === 'execution.error' || m.event === 'chat.error'), JSON.stringify(finals));
      await p.screenshot({ path: 'docs/taskboard/evidence/06-real-provider.png' });
      checks.push('real Native Provider reply observed in original Web session');
      await p.reload();
      await p.getByTestId('chat-panel-input').waitFor();
      await p.waitForFunction(
        () => document.body.innerText.split('TASKBOARD_PROVIDER_OK').length >= 3,
        {},
        { timeout: 30000 },
      );
      checks.push('real Provider history survives browser refresh');
    }
    await p.getByTestId('app-return-taskboard').click();
    await p.getByTestId('taskboard-status').waitFor();
    assert.equal(await p.getByTestId('taskboard-status').inputValue(), 'done');
    await p.getByTestId('taskboard-close').click();
    await p.waitForFunction(() => document.querySelectorAll('[data-testid="taskboard-card"]').length === 3);
    await p.screenshot({ path: 'docs/taskboard/evidence/01-board.png' });
    await p.setViewportSize({ width: 640, height: 900 });
    await p.waitForFunction(() =>
      document.querySelector('[data-testid="multi-session-sidebar"]')?.classList.contains('is-collapsed'),
    );
    await p.waitForTimeout(400);
    const box = await p.getByTestId('taskboard-create').boundingBox();
    assert(box.x + box.width <= 640);
    assert.equal(await p.evaluate(() => document.documentElement.scrollWidth), 640);
    await p.screenshot({ path: 'docs/taskboard/evidence/03-narrow.png' });
    checks.push('640px narrow screen fits existing shell without horizontal overflow');
    await p.setViewportSize({ width: 1600, height: 1000 });
    await p.evaluate(async () => {
      await window.acceptanceRpc('locale.set_conf', { preferred_language: 'en' });
      localStorage.setItem('i18nextLng', 'en');
    });
    await p.reload();
    await p.waitForFunction(
      () => document.querySelector('[data-testid="taskboard-title"]')?.textContent === 'Taskboard',
    );
    assert.equal(await p.getByTestId('taskboard-title').innerText(), 'Taskboard');
    await p.locator(`[data-testid="taskboard-card"][data-variant="${result.tasks[0]}"]`).waitFor();
    await p.screenshot({ path: 'docs/taskboard/evidence/07-english.png' });
    checks.push('English locale');
    await p.evaluate(async () => {
      const w = new WebSocket(`ws://${location.host}/ws`);
      await new Promise((r, j) => {
        w.onopen = r;
        w.onerror = j;
      });
      await new Promise((r, j) => {
        const timer = setTimeout(() => j(new Error('locale restore timeout')), 15000);
        w.onmessage = (e) => {
          const m = JSON.parse(e.data);
          if (m.id === 'locale-restore') {
            clearTimeout(timer);
            m.ok ? r() : j(new Error(JSON.stringify(m)));
          }
        };
        w.send(
          JSON.stringify({
            type: 'req',
            id: 'locale-restore',
            method: 'locale.set_conf',
            params: { preferred_language: 'zh' },
          }),
        );
      });
      w.close();
      localStorage.setItem('i18nextLng', 'zh');
    });

    assert.deepEqual(errors, []);
    fs.writeFileSync(
      'docs/taskboard/evidence/followup-result.json',
      JSON.stringify(
        {
          checks,
          errors,
          browser: await b.version(),
          provider: process.env.REAL_PROVIDER === '1' ? 'Native real model' : 'not run',
          finals,
        },
        null,
        2,
      ),
    );
    console.log(JSON.stringify({ checks, errors }));
  } catch (e) {
    await p.screenshot({ path: 'docs/taskboard/evidence/followup-failure.png' });
    throw e;
  } finally {
    await b.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
