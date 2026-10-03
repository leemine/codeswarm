import { webRequest } from './webClient';

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
export interface SharedHistoryPage extends SharedSessionTarget {
  messages: { role: 'user' | 'assistant'; content: string; id?: string }[];
  next_cursor: string | null;
  read_only: true;
}
export const sessionSharingApi = {
  history: async (target: SharedSessionTarget, cursor?: string): Promise<SharedHistoryPage> => {
    const page = await webRequest<SharedHistoryPage>('session.share.history.get', {
      session_id: target.session_id,
      share_id: target.share_id,
      ...(cursor === undefined ? {} : { cursor }),
      limit: 50,
    });
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
    webRequest<{ share: SessionShare }>('session.share.create', {
      session_id: sessionId,
      target_actor: targetActor,
      history_scope: 'current_snapshot',
      actions: [...bounds.actions],
      expires_at: bounds.expires_at,
    }),
  update: (share: SessionShare, bounds: SharingBounds) =>
    webRequest<{ share: SessionShare }>('session.share.update', {
      session_id: share.session_id,
      share_id: share.share_id,
      expected_revision: share.revision,
      actions: [...bounds.actions],
      expires_at: bounds.expires_at,
    }),
  revoke: (share: SessionShare) =>
    webRequest<{ share_id: string; revision: number }>('session.share.revoke', {
      session_id: share.session_id,
      share_id: share.share_id,
      expected_revision: share.revision,
    }),
};
