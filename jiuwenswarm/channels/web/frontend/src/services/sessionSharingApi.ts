import { webRequest } from './webClient';
import { SESSION_CREATE_TIMEOUT_MS } from '../multi-session/state/createConversationSession';

export interface ContinuationOptionsInput extends SharedSessionTarget {
  expected_revision: number;
  target_project_id: string;
}
export interface ContinuationOption {
  execution_profile_id: string;
  provider_id: 'native';
  mode: 'agent.work.normal' | 'agent.code.normal';
  model_name: string;
  label: string;
}
export interface ContinuationInput extends ContinuationOptionsInput {
  create_token: string;
  execution_profile_id: string;
  model_name: string;
  mode: ContinuationOption['mode'];
  title: string;
}
export interface ContinuedSession {
  session_id: string;
  project_id: string;
  project_dir: string;
  work_mode: 'work' | 'code';
  mode: ContinuationOption['mode'];
  execution_profile_id: string;
  model_name: string;
  title: string;
  persist_session: true;
  continued_from: { session_id: string; share_id: string; revision: number };
}
const validText = (value: unknown, maximum = 200, empty = false): value is string =>
  typeof value === 'string' &&
  value.length <= maximum &&
  (empty || value.length > 0) &&
  value === value.trim() &&
  !/[\x00-\x1f]/.test(value);
const onlyKeys = (value: unknown, keys: string[]): boolean =>
  Boolean(
    value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.keys(value).every((key) => keys.includes(key)),
  );
const validMode = (mode: unknown) => mode === 'agent.work.normal' || mode === 'agent.code.normal';

export function continuationRequest(input: ContinuationInput): Readonly<ContinuationInput> {
  if (
    !validText(input.session_id) ||
    !validText(input.share_id) ||
    !validText(input.target_project_id) ||
    !Number.isSafeInteger(input.expected_revision) ||
    input.expected_revision < 1 ||
    !validText(input.create_token) ||
    !validText(input.execution_profile_id) ||
    !validText(input.model_name) ||
    !/^.+#\d+$/.test(input.model_name) ||
    !validMode(input.mode) ||
    !validText(input.title, 100, true)
  )
    throw new Error('Invalid continuation input');
  return Object.freeze({
    session_id: input.session_id,
    share_id: input.share_id,
    expected_revision: input.expected_revision,
    target_project_id: input.target_project_id,
    create_token: input.create_token,
    execution_profile_id: input.execution_profile_id,
    model_name: input.model_name,
    mode: input.mode,
    title: input.title,
  });
}

export function validateContinuedSession(value: ContinuedSession, input: ContinuationInput): ContinuedSession {
  if (
    !onlyKeys(value, [
      'session_id',
      'project_id',
      'project_dir',
      'work_mode',
      'mode',
      'execution_profile_id',
      'model_name',
      'title',
      'persist_session',
      'continued_from',
    ]) ||
    !validText(value.session_id) ||
    value.session_id === input.session_id ||
    value.project_id !== input.target_project_id ||
    !validText(value.project_dir, 4096) ||
    value.mode !== input.mode ||
    value.work_mode !== (input.mode === 'agent.code.normal' ? 'code' : 'work') ||
    value.execution_profile_id !== input.execution_profile_id ||
    value.model_name !== input.model_name ||
    !validText(value.title, 100, true) ||
    (input.title !== '' && value.title !== input.title) ||
    value.persist_session !== true ||
    !onlyKeys(value.continued_from, ['session_id', 'share_id', 'revision']) ||
    value.continued_from?.session_id !== input.session_id ||
    value.continued_from.share_id !== input.share_id ||
    value.continued_from.revision !== input.expected_revision
  )
    throw new Error('Invalid continuation response');
  return value;
}

export const sharingActions = ['view', 'discuss', 'execute', 'approve', 'download', 'manage'] as const;
export type SharingAction = (typeof sharingActions)[number];
export interface SessionShare {
  share_id: string;
  session_id: string;
  revision: number;
  state: 'active' | 'unavailable';
  target_actor?: string;
  grantor_actor?: string;
  actions?: SharingAction[];
  expires_at?: number | null;
  history_scope?: 'fixed_snapshot';
  can_update: boolean;
  can_revoke: boolean;
}
export interface SharingBounds {
  actions: SharingAction[];
  expires_at: number | null;
}
export interface SharedSessionTarget {
  session_id: string;
  share_id: string;
}
/** Display freshness only; history.get remains the actual read authority. */
export interface SharedViewGrant {
  revision: number;
  expires_at: number | null;
}
export interface SharedHistoryPage extends SharedSessionTarget {
  messages: { role: 'user' | 'assistant'; content: string; id?: string }[];
  next_cursor: string | null;
  read_only: true;
}

export interface SharingAuditStatus {
  persisted: boolean;
  degraded: boolean;
  reason: 'audit_persisted' | 'audit_storage_invalid';
  sequence: number | null;
  event_id: string | null;
}
type SharingMutationMethod = 'session.share.create' | 'session.share.update' | 'session.share.revoke';
export interface SharingCommittedMutation extends SharedSessionTarget {
  committed: true;
  method: SharingMutationMethod;
  revision: number;
}
export class SharingMutationCommittedError extends Error {
  constructor(
    readonly mutation: SharingCommittedMutation,
    readonly audit: SharingAuditStatus,
  ) {
    super('Sharing changed; execution exit remains unconfirmed');
  }
}

function sharingAudit(value: unknown): SharingAuditStatus | undefined {
  if (value === undefined) return undefined; // Older successful RPC: audit status is unknown.
  if (!onlyKeys(value, ['persisted', 'degraded', 'reason', 'sequence', 'event_id']))
    throw new Error('Invalid sharing audit response');
  const audit = value as SharingAuditStatus;
  const persisted =
    audit.persisted === true &&
    audit.degraded === false &&
    audit.reason === 'audit_persisted' &&
    Number.isSafeInteger(audit.sequence) &&
    audit.sequence! > 0 &&
    typeof audit.event_id === 'string' &&
    /^[a-f0-9]{32}$/.test(audit.event_id);
  const degraded =
    audit.persisted === false &&
    audit.degraded === true &&
    audit.reason === 'audit_storage_invalid' &&
    audit.sequence === null &&
    audit.event_id === null;
  if (!persisted && !degraded) throw new Error('Invalid sharing audit response');
  return Object.freeze({ ...audit });
}

async function sharingMutation<T>(
  method: SharingMutationMethod,
  params: Record<string, unknown>,
): Promise<T & { audit?: SharingAuditStatus }> {
  let requestId: string | undefined;
  let value: Record<string, unknown>;
  const matches = (mutation: SharingCommittedMutation) =>
    onlyKeys(mutation, ['committed', 'method', 'session_id', 'share_id', 'revision']) &&
    mutation.committed === true &&
    mutation.method === method &&
    mutation.session_id === params.session_id &&
    validText(mutation.share_id) &&
    Number.isSafeInteger(mutation.revision) &&
    (method === 'session.share.create'
      ? mutation.revision === 1
      : mutation.share_id === params.share_id && mutation.revision === (params.expected_revision as number) + 1);
  try {
    value = await webRequest<Record<string, unknown>>(method, params, {
      onRequestId: (id) => {
        requestId = id;
      },
    });
  } catch (error) {
    const failure = error as { code?: string; requestId?: string; payload?: Record<string, unknown> };
    const payload = failure?.payload;
    if (
      requestId &&
      failure?.requestId === requestId &&
      failure.code === 'EXIT_UNCONFIRMED' &&
      payload?.code === 'EXIT_UNCONFIRMED' &&
      payload.exit_confirmed === false &&
      onlyKeys(payload, ['code', 'error', 'exit_confirmed', 'mutation', 'audit']) &&
      matches(payload.mutation as SharingCommittedMutation)
    ) {
      const audit = sharingAudit(payload.audit);
      if (audit)
        throw new SharingMutationCommittedError(
          Object.freeze({ ...(payload.mutation as SharingCommittedMutation) }),
          audit,
        );
    }
    throw error;
  }
  const record = method === 'session.share.revoke' ? value : value?.share;
  const item = record as { session_id?: string; share_id?: string; revision?: number };
  if (
    !onlyKeys(value, method === 'session.share.revoke' ? ['share_id', 'revision', 'audit'] : ['share', 'audit']) ||
    !item ||
    !validText(item.share_id) ||
    (method !== 'session.share.revoke' && item.session_id !== params.session_id) ||
    !matches({
      committed: true,
      method,
      session_id: params.session_id as string,
      share_id: item.share_id,
      revision: item.revision!,
    })
  )
    throw new Error('Invalid sharing mutation response');
  return { ...value, audit: sharingAudit(value.audit) } as T & { audit?: SharingAuditStatus };
}

export const sessionSharingApi = {
  continuationOptions: async (input: ContinuationOptionsInput): Promise<ContinuationOption[]> => {
    const params = {
      session_id: input.session_id,
      share_id: input.share_id,
      expected_revision: input.expected_revision,
      target_project_id: input.target_project_id,
    };
    const value = await webRequest<ContinuationOptionsInput & { options: ContinuationOption[] }>(
      'session.share.continuation.options',
      params,
    );
    if (
      !onlyKeys(value, [...Object.keys(params), 'options']) ||
      Object.entries(params).some(([key, expected]) => value[key as keyof ContinuationOptionsInput] !== expected) ||
      !Array.isArray(value.options) ||
      value.options.length > 1000 ||
      value.options.some(
        (option) =>
          !onlyKeys(option, ['execution_profile_id', 'provider_id', 'mode', 'model_name', 'label']) ||
          !validText(option.execution_profile_id) ||
          option.provider_id !== 'native' ||
          !validMode(option.mode) ||
          !validText(option.model_name) ||
          !/^.+#\d+$/.test(option.model_name) ||
          !validText(option.label, 500),
      ) ||
      new Set(value.options.map((row) => JSON.stringify([row.execution_profile_id, row.mode, row.model_name]))).size !==
        value.options.length
    ) {
      throw new Error('Invalid continuation options response');
    }
    return value.options;
  },
  continueSession: async (input: ContinuationInput): Promise<ContinuedSession> => {
    const request = continuationRequest(input);
    return validateContinuedSession(
      await webRequest<ContinuedSession>('session.share.continue', request, { timeoutMs: SESSION_CREATE_TIMEOUT_MS }),
      request,
    );
  },
  viewGrant: async (target: SharedSessionTarget, signal?: AbortSignal): Promise<SharedViewGrant> => {
    const result = await webRequest<{ shares: SessionShare[] }>('session.share.list', {}, { signal });
    if (!Array.isArray(result?.shares)) throw new Error('Shared history unavailable');
    const matches = result.shares.filter(
      (share) => share?.session_id === target.session_id && share.share_id === target.share_id,
    );
    const grant = matches[0];
    if (
      matches.length !== 1 ||
      grant.state !== 'active' ||
      !Array.isArray(grant.actions) ||
      !grant.actions.includes('view') ||
      !Number.isSafeInteger(grant.revision) ||
      grant.revision < 1 ||
      !(
        grant.expires_at === null ||
        (typeof grant.expires_at === 'number' &&
          Number.isFinite(grant.expires_at) &&
          grant.expires_at > Date.now() / 1000)
      )
    ) {
      throw new Error('Shared history unavailable');
    }
    return Object.freeze({ revision: grant.revision, expires_at: grant.expires_at });
  },
  history: async (target: SharedSessionTarget, cursor?: string, signal?: AbortSignal): Promise<SharedHistoryPage> => {
    const page = await webRequest<SharedHistoryPage>(
      'session.share.history.get',
      {
        session_id: target.session_id,
        share_id: target.share_id,
        ...(cursor === undefined ? {} : { cursor }),
        limit: 50,
      },
      { signal },
    );
    if (
      page?.session_id !== target.session_id ||
      page.share_id !== target.share_id ||
      page.read_only !== true ||
      !Array.isArray(page.messages) ||
      page.messages.length > 100 ||
      !(page.next_cursor === null || (typeof page.next_cursor === 'string' && page.next_cursor.length > 0)) ||
      page.messages.some(
        (message) =>
          !message ||
          !['user', 'assistant'].includes(message.role) ||
          typeof message.content !== 'string' ||
          (message.id !== undefined && typeof message.id !== 'string'),
      )
    ) {
      throw new Error('Invalid shared history response');
    }
    return page;
  },
  list: (sessionId?: string) =>
    webRequest<{ shares: SessionShare[] }>('session.share.list', sessionId ? { session_id: sessionId } : {}),
  create: (sessionId: string, targetActor: string, bounds: SharingBounds) =>
    sharingMutation<{ share: SessionShare }>('session.share.create', {
      session_id: sessionId,
      target_actor: targetActor,
      history_scope: 'current_snapshot',
      actions: [...bounds.actions],
      expires_at: bounds.expires_at,
    }),
  update: (share: SessionShare, bounds: SharingBounds) =>
    sharingMutation<{ share: SessionShare }>('session.share.update', {
      session_id: share.session_id,
      share_id: share.share_id,
      expected_revision: share.revision,
      actions: [...bounds.actions],
      expires_at: bounds.expires_at,
    }),
  revoke: (share: SessionShare) =>
    sharingMutation<{ share_id: string; revision: number }>('session.share.revoke', {
      session_id: share.session_id,
      share_id: share.share_id,
      expected_revision: share.revision,
    }),
};
