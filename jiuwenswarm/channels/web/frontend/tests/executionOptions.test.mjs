import test from 'node:test';
import assert from 'node:assert/strict';
import {
  prepareExecutionCreate,
  parseExecutionOptions,
  selectCreationExecution,
  executionProviderLabel,
} from '../node_modules/.cache/execution-options/executionOptions.js';
const native = {
  execution_profile_id: 'n',
  provider_id: 'native',
  config_fingerprint: 'a'.repeat(64),
  available: true,
  reason: null,
  model_selection_keys: null,
};
const oc = { ...native, execution_profile_id: 'o', provider_id: 'opencode', model_selection_keys: ['example#0'] };
const catalog = { options: [native, oc], default_profile_id: 'n' };
test('explicit Provider survives default change and freezes exact model/key before create', async () => {
  const calls = [];
  const params = { mode: 'agent.code.normal', work_mode: 'code', model_name: 'example' };
  await prepareExecutionCreate(
    async (...args) => {
      calls.push(args);
      return catalog;
    },
    params,
    oc,
  );
  assert.deepEqual(calls, [['session.execution.options', { mode: 'agent.code.normal', work_mode: 'code' }]]);
  assert.equal(params.execution_profile_id, 'o');
  assert.equal(params.execution_expected_fingerprint, oc.config_fingerprint);
  assert.equal(params.model_name, 'example#0');
});
test('changed or missing choice never falls back to default', () => {
  for (const choice of [
    { ...oc, config_fingerprint: 'b'.repeat(64) },
    { ...oc, execution_profile_id: 'missing' },
    { ...oc, provider_id: 'native' },
  ])
    assert.throws(() => selectCreationExecution(catalog, choice), /selectionUnavailable/);
  assert.throws(
    () => selectCreationExecution({ ...catalog, options: [native, { ...oc, available: false }] }, oc),
    /selectionUnavailable/,
  );
});
test('ambiguous and incompatible models cannot create a session', async () => {
  for (const [model_name, keys] of [
    ['other', ['example#0']],
    ['example', ['example#0', 'example#1']],
  ]) {
    const params = { mode: 'agent', work_mode: 'work', model_name };
    await assert.rejects(
      prepareExecutionCreate(
        async () => ({ ...catalog, options: [native, { ...oc, model_selection_keys: keys }] }),
        params,
        oc,
      ),
      /chooseModel/,
    );
    assert.equal(params.execution_profile_id, undefined);
  }
});
test('original Native model and non-Agent create flow remain intact', async () => {
  const params = { mode: 'agent', model_name: 'legacy' };
  await prepareExecutionCreate(async () => catalog, params, native);
  assert.equal(params.model_name, 'legacy');
  await prepareExecutionCreate(async () => assert.fail('must not query'), { mode: 'team.work.normal' });
});
test('malformed catalog and duplicated identifiers fail closed', () => {
  for (const value of [
    null,
    {},
    { ...catalog, options: [native, native] },
    { ...catalog, options: [{ ...oc, model_selection_keys: [{}] }] },
  ])
    assert.throws(() => parseExecutionOptions(value));
});

test('unconfigured engines remain discoverable without becoming creation choices', async () => {
  const response = parseExecutionOptions({
    options: [native],
    default_profile_id: 'n',
    unconfigured_providers: [{ provider_id: 'opencode', reason: 'configuration_required' }],
  });
  assert.equal(response.unconfigured_providers[0].provider_id, 'opencode');
  assert.throws(() => selectCreationExecution(response, oc), /selectionUnavailable/);
  const params = { mode: 'agent.code.normal', work_mode: 'code' };
  await prepareExecutionCreate(async () => response, params, native);
  assert.equal(params.execution_profile_id, 'n');
  assert.equal(executionProviderLabel(native.provider_id), 'Deepagent');
  assert.equal(native.provider_id, 'native');
});

test('malformed or duplicate discovery entries cannot mask configured choices', () => {
  for (const unconfigured_providers of [
    {},
    [null],
    [{ provider_id: 'opencode' }],
    [{ provider_id: 'native', reason: 'configuration_required' }],
    [
      { provider_id: 'dsh', reason: 'provider_unavailable' },
      { provider_id: 'dsh', reason: 'provider_unavailable' },
    ],
  ])
    assert.throws(() => parseExecutionOptions({ ...catalog, unconfigured_providers }));
  assert.equal(parseExecutionOptions(catalog), catalog); // earlier server response remains valid
  assert.equal(executionProviderLabel('claudecode'), 'Claude Code');
});


test('installed engine defaults use the original creation token and cannot switch silently', async () => {
  for (const provider_id of ['opencode', 'codex']) {
    const choice = { ...native, execution_profile_id: `builtin:${provider_id}`, provider_id };
    const response = parseExecutionOptions({ options: [native, choice], default_profile_id: 'n' });
    const params = { mode: 'agent.code.normal', work_mode: 'code' };
    await prepareExecutionCreate(async () => response, params, choice);
    assert.equal(params.execution_profile_id, choice.execution_profile_id);
    assert.equal(params.execution_expected_fingerprint, choice.config_fingerprint);
    assert.throws(() => selectCreationExecution(catalog, choice), /selectionUnavailable/);
  }
});
