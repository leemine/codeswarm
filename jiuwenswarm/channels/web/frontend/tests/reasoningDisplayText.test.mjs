import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';
import { mkdir } from 'node:fs/promises';
let useReasoningDisplayText;
before(async () => {
  await mkdir('node_modules/.cache/reasoning-display', { recursive: true });
  await build({
    entryPoints: ['src/components/ChatPanel/useReasoningDisplayText.ts'],
    bundle: true,
    packages: 'external',
    platform: 'node',
    format: 'esm',
    outfile: 'node_modules/.cache/reasoning-display/hook.mjs',
  });
  ({ useReasoningDisplayText } = await import('../node_modules/.cache/reasoning-display/hook.mjs'));
});
async function fixture(t, run) {
  const dom = new JSDOM('<div id="root"></div>');
  const previous = new Map();
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const root = createRoot(document.getElementById('root'));
  function Probe({ id, text, closed }) {
    return createElement('output', null, useReasoningDisplayText(id, text, closed));
  }
  const render = (text, closed = false, id = 'first') =>
    act(() => root.render(createElement(Probe, { id, text, closed })));
  try {
    await run({
      render,
      read: () => document.querySelector('output').textContent,
      tick: (ms) => act(() => t.mock.timers.tick(ms)),
    });
  } finally {
    act(() => root.unmount());
    t.mock.timers.reset();
    dom.window.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}
test('continuous deltas coalesce without starving updates or losing the full text', async (t) =>
  fixture(t, ({ render, read, tick }) => {
    const prefix = 'reasoning '.repeat(20000);
    render(prefix);
    for (let i = 1; i <= 9; i++) {
      render(prefix + 'x'.repeat(i));
      tick(10);
      assert.equal(read(), prefix);
    }
    render(prefix + 'x'.repeat(10));
    tick(10);
    assert.equal(read(), prefix + 'x'.repeat(10));
    render(prefix + 'x'.repeat(11));
    tick(100);
    assert.equal(read(), prefix + 'x'.repeat(11));
  }));
test('terminal and restored content appears immediately and a pending timer cannot overwrite it', async (t) =>
  fixture(t, ({ render, read, tick }) => {
    render('start');
    render('pending');
    render('full final tail', true);
    assert.equal(read(), 'full final tail');
    tick(500);
    assert.equal(read(), 'full final tail');
    render('restored history', true);
    assert.equal(read(), 'restored history');
  }));
test('switching segments cannot show or flush the previous segment into the new one', async (t) =>
  fixture(t, ({ render, read, tick }) => {
    render('old');
    render('pending old');
    render('new initial', false, 'second');
    assert.equal(read(), 'new initial');
    render('new tail', false, 'second');
    tick(100);
    assert.equal(read(), 'new tail');
  }));
