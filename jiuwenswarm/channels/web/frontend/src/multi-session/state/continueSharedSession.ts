/** In-memory immutable attempt only. Server publication owns idempotency/authority. */
import {
  continuationRequest,
  validateContinuedSession,
  type ContinuationInput,
  type ContinuedSession,
} from '../../services/sessionSharingApi';
import { isRequestTimeoutError, type SessionCreateRequestFn } from './createConversationSession';
import type { Session } from '../../types';

export interface ContinuationAttempt {
  input: Readonly<ContinuationInput>;
  outcome: 'unknown' | 'failed';
}
export type ContinuationAttempts = Map<string, ContinuationAttempt>;

export function newContinuationAttempt(input: Omit<ContinuationInput, 'create_token'>): Readonly<ContinuationInput> {
  return continuationRequest({ ...input, create_token: crypto.randomUUID() });
}

export function isContinuationOutcomeUnknown(error: unknown): boolean {
  const code = error && typeof error === 'object' ? (error as { code?: string }).code : undefined;
  return (
    isRequestTimeoutError(error) ||
    ['AGENT_SERVER_TIMEOUT', 'WS_CLOSED', 'WS_DISCONNECTED', 'WS_NOT_READY', 'WS_ERROR'].includes(code ?? '')
  );
}

/** Verify current owned metadata and switch before any local registry/navigation mutation. */
export async function prepareContinuedConversation(
  request: SessionCreateRequestFn,
  result: ContinuedSession,
  input: ContinuationInput,
  isCurrent: () => boolean,
  previous: { session_id: string; mode: string; view_id: string },
): Promise<Session> {
  validateContinuedSession(result, input);
  const check = () => {
    if (!isCurrent()) throw new Error('Continuation navigation is no longer current');
  };
  check();
  const metadata = await request<Session & { execution_profile_id?: string }>('session.get_metadata', {
    session_id: result.session_id,
  });
  check();
  if (
    metadata.session_id !== result.session_id ||
    metadata.project_id !== result.project_id ||
    metadata.project_dir !== result.project_dir ||
    metadata.work_mode !== result.work_mode ||
    metadata.mode !== result.mode ||
    metadata.model !== result.model_name ||
    metadata.execution_profile_id !== result.execution_profile_id ||
    metadata.persist_session !== true ||
    metadata.title !== result.title ||
    metadata.is_processing === true ||
    metadata.ephemeral ||
    metadata.side_parent_session_id ||
    metadata.forked_from
  ) {
    throw new Error('Continuation owned metadata changed');
  }
  await request('session.switch', {
    session_id: result.session_id,
    previous_session_id: previous.session_id,
    previous_mode: previous.mode,
    mode: result.mode,
    view_id: previous.view_id,
  });
  check();
  // Deliberately select only the new recipient's fields, never source equipment,
  // memory, pending approval, tools, runtime state or active processing flags.
  return {
    session_id: result.session_id,
    title: result.title,
    project_id: result.project_id,
    project_dir: result.project_dir,
    work_mode: result.work_mode,
    mode: result.mode,
    model: result.model_name,
    persist_session: true,
    status: 'active',
    message_count: 0,
    created_at: typeof metadata.created_at === 'string' ? metadata.created_at : new Date().toISOString(),
    updated_at: typeof metadata.updated_at === 'string' ? metadata.updated_at : new Date().toISOString(),
    is_processing: false,
  };
}
