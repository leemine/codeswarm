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
export const sessionSharingApi = {
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
