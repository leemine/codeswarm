import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
const target = new URL('../node_modules/.cache/cli-auth-modal.mjs', import.meta.url);
await build({
  entryPoints: ['src/components/ConnectorMarket/CliAuthModal.tsx'],
  bundle: true, packages: 'external', platform: 'node', format: 'esm', outfile: target.pathname,
  plugins: [{ name: 'local-store', setup(b) {
    b.onResolve({ filter: /stores\/connectorStore$/ }, () => ({ path: 'store', namespace: 'test' }));
    b.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents:
      'export const useConnectorStore = select => select(globalThis.cliTestStore);' }));
    b.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'i18n', namespace: 'test-i18n' }));
    b.onLoad({ filter: /.*/, namespace: 'test-i18n' }, () => ({ contents:
      'export const useTranslation = () => ({t: key => key});' }));
  } }],
});
const { CliAuthModal } = await import(target.href);
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const initial = { type: 'auth_required', stepIndex: 0, stepsTotal: 2, authUrl: 'https://open.feishu.cn/test' };
async function fixture(run) {
  const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost/'});
  Object.assign(globalThis, {window:dom.window, document:dom.window.document, IS_REACT_ACT_ENVIRONMENT:true});
  const root = createRoot(document.getElementById('root'));
  const opened = [], waits = [], first = deferred(), next = deferred();
  const calls = { connected:0, cancelled:0, disconnected:0, connect:0 };
  window.open = url => opened.push(url);
  globalThis.cliTestStore = {
    waitAuth: (_name, index) => { waits.push(index); return index === 0 ? first.promise : next.promise; },
    disconnect: async () => { calls.disconnected++; },
    connect: async () => { calls.connect++; return {...initial, stepIndex:1, authUrl:'https://accounts.feishu.cn/retry'}; },
  };
  const props = {name:'feishu', initial, onConnected:()=>calls.connected++, onCancel:()=>calls.cancelled++};
  const button = suffix => document.querySelector(`[data-testid="connector-market-cli-auth-modal-${suffix}"]`);
  try {
    await act(async()=>root.render(React.createElement(React.StrictMode, null, React.createElement(CliAuthModal, props))));
    await run({opened,waits,first,next,calls,button});
  } finally { await act(async()=>root.unmount()); dom.window.close(); delete globalThis.cliTestStore; }
}
test('one wait and one browser open per step, including StrictMode effect replay',()=>fixture(async f=>{
  assert.deepEqual(f.waits,[0]); assert.deepEqual(f.opened,[initial.authUrl]);
  await act(async()=>f.first.resolve({...initial, stepIndex:1, authUrl:'https://accounts.feishu.cn/login'}));
  assert.deepEqual(f.waits,[0,1]); assert.equal(f.opened[1],'https://accounts.feishu.cn/login');
  assert.equal(f.calls.connected,0);
  await act(async()=>f.next.resolve({type:'connected'})); assert.equal(f.calls.connected,1);
}));
test('cancel disconnects and ignores a late success',()=>fixture(async f=>{
  await act(async()=>f.button('close').click());
  await act(async()=>f.first.resolve({type:'connected'}));
  assert.equal(f.calls.disconnected,1); assert.equal(f.calls.cancelled,1); assert.equal(f.calls.connected,0);
}));
test('retry starts a fresh connect and prevents duplicate clicks',()=>fixture(async f=>{
  await act(async()=>f.first.resolve(null));
  const retry = f.button('retry'); assert.ok(retry);
  await act(async()=>{ retry.click(); retry.click(); });
  assert.equal(f.calls.connect,1); assert.deepEqual(f.waits,[0,1]);
}));
