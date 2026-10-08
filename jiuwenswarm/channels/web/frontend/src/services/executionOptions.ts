import type { SessionCreateRequestFn } from '../multi-session/state/createConversationSession';

export interface ExecutionOption {
  execution_profile_id: string | null;
  provider_id: string;
  config_fingerprint: string;
  available: boolean;
  reason: string | null;
  model_selection_keys: string[] | null;
}
export interface ExecutionOptions {
  options: ExecutionOption[];
  default_profile_id: string | null;
}
export type ExecutionChoice = Pick<
  ExecutionOption,
  'execution_profile_id' | 'provider_id' | 'config_fingerprint' | 'model_selection_keys'
>;

export function parseExecutionOptions(value: unknown): ExecutionOptions {
  const payload = value as ExecutionOptions;
  if (
    !payload ||
    !Array.isArray(payload.options) ||
    !(payload.default_profile_id === null || typeof payload.default_profile_id === 'string') ||
    payload.options.some(
      (row) =>
        !row ||
        !(row.execution_profile_id === null || typeof row.execution_profile_id === 'string') ||
        typeof row.provider_id !== 'string' ||
        typeof row.config_fingerprint !== 'string' ||
        typeof row.available !== 'boolean' ||
        !(row.reason === null || typeof row.reason === 'string') ||
        !(
          row.model_selection_keys === null ||
          (Array.isArray(row.model_selection_keys) && row.model_selection_keys.every((key) => typeof key === 'string'))
        ),
    ) ||
    new Set(payload.options.map((row) => row.execution_profile_id)).size !== payload.options.length
  ) {
    throw new Error('Invalid execution options response');
  }
  return payload;
}

export async function getExecutionOptions(request: SessionCreateRequestFn, mode: string, workMode: string) {
  return parseExecutionOptions(await request('session.execution.options', { mode, work_mode: workMode }));
}

export function selectCreationExecution(options: ExecutionOptions, choice?: ExecutionChoice | null): ExecutionOption {
  const id = choice ? choice.execution_profile_id : options.default_profile_id;
  const selected = options.options.find((row) => row.execution_profile_id === id);
  if (
    !selected?.available ||
    (choice &&
      (choice.provider_id !== selected.provider_id || choice.config_fingerprint !== selected.config_fingerprint))
  ) {
    throw new Error('executionPicker.selectionUnavailable');
  }
  return selected;
}

/** Freeze the explicit choice into the original create-token request before retrying. */
export async function prepareExecutionCreate(
  request: SessionCreateRequestFn,
  params: Record<string, unknown>,
  choice?: ExecutionChoice | null,
): Promise<void> {
  if (typeof params.mode !== 'string' || !params.mode.startsWith('agent')) return;
  const options = await getExecutionOptions(request, params.mode, String(params.work_mode || 'work'));
  const selected = selectCreationExecution(options, choice);
  if (selected.model_selection_keys) {
    const requested = String(params.model_name || '');
    const matches = selected.model_selection_keys.filter(
      (key) => key === requested || key.replace(/#\d+$/, '') === requested,
    );
    if (matches.length !== 1) throw new Error('executionPicker.chooseModel');
    params.model_name = matches[0];
  }
  params.execution_expected_fingerprint = selected.config_fingerprint;
  if (selected.execution_profile_id !== null) params.execution_profile_id = selected.execution_profile_id;
}

export function executionProviderLabel(provider: string | null | undefined): string {
  return (
    { native: 'Native', opencode: 'OpenCode', codex: 'Codex', claude: 'Claude Code', dsh: 'DSH' }[provider || ''] ||
    provider ||
    ''
  );
}
