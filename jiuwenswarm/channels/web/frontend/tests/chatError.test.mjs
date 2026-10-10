import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/chat-error/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
});
let describeChatError;
try {
  ({ describeChatError } = await vite.ssrLoadModule('/src/features/free-models/chatError.ts'));
} finally {
  await vite.close();
}

const t = (key) => `<${key}>`;
const RAW = "[181001] model call failed: Error code: 401 - {'error_code': 'APIG.0305'}";

test('upstream free-model failures get an actionable hint and keep the raw text', () => {
  for (const [code, key] of [
    ['login_required', 'auth.huawei.modelError.loginRequired'],
    ['quota_exhausted', 'auth.huawei.quota.exhaustedHint'],
    ['rate_limited', 'auth.huawei.modelError.rateLimited'],
    ['free_model_unavailable', 'auth.huawei.modelError.unavailable'],
  ]) {
    assert.equal(describeChatError({ code, upstream: true }, RAW, t), `<${key}>（${RAW}）`);
  }
});

test('precheck login_required already reads well and is not wrapped', () => {
  const precheck = '该模型需要登录华为账号后使用（未登录或登录已过期），请登录后重试';
  assert.equal(describeChatError({ code: 'login_required' }, precheck, t), precheck);
});

test('model_not_configured is replaced by the setup guidance', () => {
  assert.equal(describeChatError({ code: 'model_not_configured' }, 'x', t), '<chat.modelNotConfigured>');
});

test('errors without a known code are shown as-is', () => {
  assert.equal(describeChatError({}, RAW, t), RAW);
  assert.equal(describeChatError({ code: 'something_else', upstream: true }, RAW, t), RAW);
});

// Live events and restored history carry the same stable code. The latter may
// contain only the old generic text; repeated formatting must not stack hints.
test('declined interactions explain the terminal cause for live and old history', () => {
  for (const raw of ['OpenCode execution failed', '错误: OpenCode execution failed', '<chat.executionInteractionDeclined>']) {
    assert.equal(describeChatError({ code: 'interaction_declined' }, raw, t), '<chat.executionInteractionDeclined>');
  }
});

test('event data budget failure is distinct from model output and does not stack hints', () => {
  for (const raw of ['OpenCode execution failed', '<chat.executionEventDataLimit>']) {
    assert.equal(describeChatError({ code: 'turn_event_limit' }, raw, t), '<chat.executionEventDataLimit>');
  }
  assert.equal(describeChatError({ code: 'model_output_limit_exceeded' }, RAW, t), '<chat.executionModelOutputLimit>');
});

test('model output limit is readable for live and restored generic failures', () => {
  for (const raw of ['OpenCode execution failed', '<chat.executionModelOutputLimit>']) {
    assert.equal(describeChatError({ code: 'model_output_limit_exceeded' }, raw, t), '<chat.executionModelOutputLimit>');
  }
});
