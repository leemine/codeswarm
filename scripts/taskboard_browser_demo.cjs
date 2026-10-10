/* Real Chrome + the live Web/Gateway/AgentServer; no intercepted network responses. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const base = process.env.DEMO_URL || 'http://127.0.0.1:19240';
const out = path.resolve(process.env.EVIDENCE_DIR || 'docs/taskboard/evidence');
(async () => {
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME_PATH || '/opt/google/chrome/chrome',
    args: ['--no-sandbox'],
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const checks = [];
  const passed = (s) => {
    checks.push(s);
    console.log('PASS', s);
  };
  try {
    await page.addInitScript(() => {
      localStorage.setItem('jiuwenswarm_work_mode', 'code');
      localStorage.setItem('i18nextLng', 'zh');
    });
    await page.goto(base + '/taskboard');
    await page.getByTestId('taskboard-create').waitFor({ timeout: 60000 });
    await page.waitForFunction(() => document.querySelector('[aria-busy="false"]'));
    const refs = await page.evaluate(async () => {
      const ws = new WebSocket(`ws://${location.host}/ws`);
      await new Promise((r, j) => {
        ws.onopen = r;
        ws.onerror = j;
      });
      let seq = 0;
      async function rpc(method, params) {
        return new Promise((resolve, reject) => {
          const id = 'taskboard-e2e-' + ++seq;
          const timer = setTimeout(() => reject(new Error('RPC timeout: ' + method)), 45000);
          const onmessage = (e) => {
            const m = JSON.parse(e.data);
            if (m.id === id) {
              clearTimeout(timer);
              ws.removeEventListener('message', onmessage);
              m.ok ? resolve(m.payload) : reject(new Error(JSON.stringify(m)));
            }
          };
          ws.addEventListener('message', onmessage);
          ws.send(JSON.stringify({ type: 'req', id, method, params }));
        });
      }
      try {
        const projects = await rpc('project.list', { work_mode: 'code' });
        const project = projects.projects.find((p) => p.name === 'Taskboard Demo');
        if (!project)
          throw new Error('Create a real Code project named Taskboard Demo before running browser acceptance');
        let sessions = await rpc('project.get_sessions', { project_id: project.project_id });
        const session = sessions.sessions[0];
        if (!session) throw new Error('Create an existing Web Code session first');
        await rpc('session.rename', { session_id: session.session_id, title: 'Taskboard Demo 联调会话' });
        return { project: project.project_id, session: session.session_id };
      } finally {
        ws.close();
      }
    });
    async function create(title, description, project = '') {
      await page.getByTestId('taskboard-create').click();
      await page.getByTestId('taskboard-title-input').fill(title);
      await page.getByTestId('taskboard-description-input').fill(description);
      await page.getByTestId('taskboard-project-input').selectOption(project);
      await page.getByTestId('taskboard-submit').click();
      await page.getByTestId('taskboard-status').waitFor();
      const id = new URL(page.url()).pathname.split('/').pop();
      return id;
    }
    const first = await create(
      '完成 Taskboard 最小版联调',
      '通过真实链路创建任务，关联已有会话并记录结果。',
      refs.project,
    );
    passed('create persisted task via browser');
    await page.getByTestId('taskboard-edit').click();
    await page.getByTestId('taskboard-title-input').fill('完成 Taskboard 最小版 Demo 联调');
    await page.getByTestId('taskboard-submit').click();
    await page
      .getByTestId('taskboard-detail')
      .getByRole('heading', { name: '完成 Taskboard 最小版 Demo 联调' })
      .waitFor();
    passed('edit task');
    await page.getByTestId('taskboard-status').selectOption('doing');
    await page.waitForFunction(() => document.querySelector('[data-testid="taskboard-status"]')?.value === 'doing');
    await page.getByTestId('taskboard-link').click();
    await page.getByTestId('taskboard-session-option').first().waitFor();
    await page.getByTestId('taskboard-session-option').first().click();
    await page.getByTestId('taskboard-confirm-link').click();
    await page.getByTestId('taskboard-open-session').waitFor();
    passed('link existing real Web Code session');
    await page.getByTestId('taskboard-open-session').click();
    await page.waitForURL('**/chat/' + refs.session);
    await page.getByTestId('app-return-taskboard').click();
    await page.waitForURL('**/taskboard/' + first);
    await page.getByTestId('taskboard-status').waitFor();
    passed('open actual session and return to task');
    await page.getByTestId('taskboard-result').fill('已完成新建、编辑、关联会话、返回、结果记录与人工完成验证。');
    await page.getByTestId('taskboard-save-result').click();
    await page.waitForTimeout(400);
    await page.reload();
    await page.getByTestId('taskboard-result').waitFor();
    assert.equal(
      await page.getByTestId('taskboard-result').inputValue(),
      '已完成新建、编辑、关联会话、返回、结果记录与人工完成验证。',
    );
    passed('save result and deep-link reload');
    await page.getByTestId('taskboard-complete').click();
    await page.waitForFunction(() => document.querySelector('[data-testid="taskboard-status"]')?.value === 'done');
    await page.getByTestId('taskboard-complete').waitFor({ state: 'visible' });
    await page.waitForFunction(() => !document.querySelector('[data-testid="taskboard-complete"]')?.disabled);
    await page.screenshot({ path: path.join(out, '02-task-detail.png') });
    passed('manual completion');
    await page.getByTestId('taskboard-complete').click();
    await page.waitForFunction(() => document.querySelector('[data-testid="taskboard-status"]')?.value === 'todo');
    await page.getByTestId('taskboard-complete').click();
    await page.waitForFunction(() => document.querySelector('[data-testid="taskboard-status"]')?.value === 'done');
    passed('reopen and manually complete again');
    await page.getByTestId('taskboard-close').click();
    await page.getByTestId('taskboard-search').fill('不存在的任务');
    await page.waitForTimeout(650);
    assert.equal(await page.getByTestId('taskboard-card').count(), 0);
    await page.getByTestId('taskboard-search').fill('Demo 联调');
    await page.waitForTimeout(650);
    assert.equal(await page.getByTestId('taskboard-card').count(), 1);
    await page.getByTestId('taskboard-search').fill('');
    await page.getByTestId('taskboard-project-filter').selectOption(refs.project);
    await page.waitForTimeout(650);
    assert.equal(await page.getByTestId('taskboard-card').count(), 1);
    await page.getByTestId('taskboard-project-filter').selectOption('');
    passed('search and project filter');
    const second = await create('检查 Gateway 与实例隔离', '沿用统一入口，业务持久化位于 AgentServer 实例。');
    await page.getByTestId('taskboard-close').click();
    await page
      .locator(`[data-testid="taskboard-card"][data-variant="${second}"]`)
      .dragTo(page.locator('[data-testid="taskboard-column"][data-variant="doing"]'));
    await page.waitForTimeout(600);
    assert.equal(
      await page.locator(`[data-testid="taskboard-column"][data-variant="doing"] [data-variant="${second}"]`).count(),
      1,
    );
    passed('drag status update');
    const third = await create('整理 Demo 使用说明', '保留启动、停止、验证记录和界面截图。');
    await page.getByTestId('taskboard-close').click();
    await page.screenshot({ path: path.join(out, '01-board.png') });
    await page.goto(base + '/taskboard/' + second);
    await page.getByTestId('taskboard-result').waitFor();
    await page.getByTestId('taskboard-result').fill('未提交草稿');
    await page.goBack();
    await page.goto(base + '/taskboard/' + second);
    await page.getByTestId('taskboard-result').waitFor();
    assert.equal(await page.getByTestId('taskboard-result').inputValue(), '未提交草稿');
    page.once('dialog', (d) => d.accept());
    await page.getByTestId('taskboard-close').click();
    passed('draft survives browser navigation');
    await page.setViewportSize({ width: 640, height: 900 });
    await page.waitForFunction(() =>
      document.querySelector('[data-testid="multi-session-sidebar"]')?.classList.contains('is-collapsed'),
    );
    await page.waitForTimeout(350);
    const box = await page.getByTestId('taskboard-title').boundingBox();
    assert(box.x >= 0 && box.x + box.width <= 640);
    await page.screenshot({ path: path.join(out, '03-narrow.png') });
    passed('640px narrow layout');
    assert.deepEqual(errors, []);
    passed('no uncaught browser errors');
    fs.writeFileSync(
      path.join(out, 'browser-result.json'),
      JSON.stringify({ base, checks, tasks: [first, second, third], refs, errors }, null, 2),
    );
  } catch (e) {
    await page.screenshot({ path: path.join(out, 'failure.png') });
    throw e;
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
