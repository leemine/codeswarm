import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Dialog } from '../../components/ui/Dialog/Dialog';
import {
  sessionSharingApi,
  SharingMutationCommittedError,
  type SessionShare,
  type SharingAction,
  type SharedSessionTarget,
  type SharingAuditStatus,
} from '../../services/sessionSharingApi';
import './ShareSessionDialog.css';
import { ContinuationPane, type OnContinued } from './ContinuationPane';
import { SharingAuditSection } from './SharingAuditSection';
import { onOrganizationCredentialChange } from '../../services/organizationCredentialEvents';
import { webClient } from '../../services/webClient';
import type { ContinuationAttempts } from '../state/continueSharedSession';

function localDateTime(time: number): string {
  const date = new Date(time);
  return new Date(time - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export function ShareSessionDialog(props: {
  sessionId?: string;
  onClose: () => void;
  onOpenSharedSession?: (target: SharedSessionTarget) => void;
  onContinued?: OnContinued;
  continuationAttempts?: ContinuationAttempts;
}) {
  return <ShareSessionContent key={props.sessionId ?? ''} {...props} />;
}

function ShareSessionContent({
  sessionId,
  onClose,
  onOpenSharedSession,
  onContinued,
  continuationAttempts,
}: {
  sessionId?: string;
  onClose: () => void;
  onOpenSharedSession?: (target: SharedSessionTarget) => void;
  onContinued?: OnContinued;
  continuationAttempts?: ContinuationAttempts;
}) {
  const { t } = useTranslation();
  const [managed, setManaged] = useState<SessionShare[]>([]);
  const [received, setReceived] = useState<SessionShare[]>([]);
  const [canCreate, setCanCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [feedback, setFeedback] = useState<{ auditDegraded: boolean; exitUnconfirmed: boolean } | null>(null);
  const [revokedHere, setRevokedHere] = useState<Set<string>>(new Set());
  const [target, setTarget] = useState('');
  const [actions, setActions] = useState<SharingAction[]>(['view']);
  const [expires, setExpires] = useState(() => localDateTime(Date.now() + 86400000));
  const [editing, setEditing] = useState<SessionShare | null>(null);
  const generation = useRef(0);
  const operationGeneration = useRef(0);
  const localAttempts = useRef<ContinuationAttempts>(new Map());
  const [continuing, setContinuing] = useState<SessionShare | null>(null);

  const pendingRefresh = useRef<AbortController | null>(null);
  function invalidate(preserveContinuation = false) {
    generation.current += 1;
    operationGeneration.current += 1;
    pendingRefresh.current?.abort();
    pendingRefresh.current = null;
    setManaged([]);
    setReceived([]);
    setCanCreate(false);
    setBusy(false);
    setEditing(null);
    setTarget('');
    setFeedback(null);
    // The existing continuation pane retains its immutable retry token across disconnects.
    if (!preserveContinuation) setContinuing(null);
    setLoading(false);
  }
  function close() {
    invalidate();
    onClose();
  }

  async function refresh() {
    pendingRefresh.current?.abort();
    const controller = new AbortController();
    pendingRefresh.current = controller;
    const current = ++generation.current;
    setLoading(true);
    setError(false);
    setManaged([]);
    setReceived([]);
    setCanCreate(false);
    setContinuing(null);
    const [owned, all] = await Promise.allSettled([
      sessionId ? sessionSharingApi.list(sessionId, controller.signal) : Promise.resolve(null),
      sessionSharingApi.list(undefined, controller.signal),
    ]);
    if (current !== generation.current || controller.signal.aborted) return;
    if (owned.status === 'fulfilled' && owned.value !== null) {
      setManaged(owned.value.shares);
      setCanCreate(true);
    }
    if (all.status === 'fulfilled') {
      setReceived(all.value.shares.filter((share) => !share.can_revoke));
    }
    setError(all.status === 'rejected' || owned.status === 'rejected');
    setLoading(false);
  }
  useEffect(() => {
    setBusy(false);
    setFeedback(null);
    setRevokedHere(new Set());
    void refresh();
    const auth = onOrganizationCredentialChange(() => invalidate());
    const connection = webClient.onStateChange((state) => {
      if (state !== 'ready') invalidate(true);
    });
    return () => {
      auth();
      connection();
      pendingRefresh.current?.abort();
      generation.current += 1;
      operationGeneration.current += 1;
    };
  }, [sessionId]);

  async function perform(operation: () => Promise<{ audit?: SharingAuditStatus }>, revokedShare?: SessionShare) {
    const current = generation.current;
    const operationId = ++operationGeneration.current;
    setBusy(true);
    setError(false);
    setFeedback(null);
    const committed = async (audit: SharingAuditStatus | undefined, exitUnconfirmed: boolean) => {
      if (revokedShare) setRevokedHere((previous) => new Set([...previous, revokedShare.share_id]));
      setFeedback({ auditDegraded: audit?.degraded === true, exitUnconfirmed });
      setEditing(null);
      setTarget('');
      setActions(['view']);
      await refresh(); // Read current state only; never repeat a committed change.
    };
    try {
      const result = await operation();
      if (current !== generation.current) return;
      await committed(result.audit, false);
    } catch (failure) {
      if (current === generation.current) {
        if (failure instanceof SharingMutationCommittedError) {
          await committed(failure.audit, true);
          return;
        }
        setError(true);
        // Withdraw cached grants after any failed current authorization.
        setManaged([]);
        setReceived([]);
        setCanCreate(false);
      }
    } finally {
      if (operationId === operationGeneration.current) setBusy(false);
    }
  }
  function edit(share: SessionShare) {
    setEditing(share);
    setTarget(share.target_actor ?? '');
    setActions(share.actions ?? []);
    setExpires(share.expires_at ? localDateTime(share.expires_at * 1000) : '');
  }
  const expiry = expires ? new Date(expires).getTime() / 1000 : null;
  const validExpiry = expiry === null ? Boolean(editing) : Number.isFinite(expiry) && expiry > Date.now() / 1000;
  return (
    <Dialog
      open
      titleId="session-sharing-title"
      className="session-sharing-dialog"
      closeDisabled={busy}
      onCancel={close}
      onBackdropClick={close}
    >
      <div data-testid="multi-session-sharing-dialog" className="session-sharing-content">
        <div className="session-sharing-heading">
          <h2 id="session-sharing-title" data-testid="multi-session-sharing-title">
            {t('sessionSharing.title')}
          </h2>
          <button type="button" onClick={close} disabled={busy} data-testid="multi-session-sharing-close">
            {t('common.close')}
          </button>
        </div>
        <p data-testid="multi-session-sharing-scope">{t('sessionSharing.scope')}</p>
        <button
          type="button"
          onClick={() => {
            void refresh();
          }}
          disabled={busy || loading}
          data-testid="multi-session-sharing-refresh"
        >
          {t('sessionSharing.refresh')}
        </button>
        {loading && (
          <p role="status" data-testid="multi-session-sharing-loading">
            {t('sessionSharing.loading')}
          </p>
        )}
        {error && (
          <p role="alert" className="session-sharing-error" data-testid="multi-session-sharing-error">
            {t(feedback ? 'sessionSharing.refreshError' : 'sessionSharing.error')}
          </p>
        )}
        {feedback?.exitUnconfirmed && (
          <p role="alert" className="session-sharing-error" data-testid="multi-session-sharing-exit-unconfirmed">
            {t('sessionSharing.exitUnconfirmed')}
          </p>
        )}
        {feedback?.auditDegraded && (
          <p role="alert" className="session-sharing-error" data-testid="multi-session-sharing-audit-degraded">
            {t('sessionSharing.auditDegraded')}
          </p>
        )}
        {canCreate && !loading && (
          <form
            data-testid="multi-session-sharing-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!sessionId || !validExpiry || !actions.length || !target.trim()) return;
              const bounds = { actions, expires_at: expiry };
              void perform(() =>
                editing
                  ? sessionSharingApi.update(editing, bounds)
                  : sessionSharingApi.create(sessionId, target.trim(), bounds),
              );
            }}
          >
            <label data-testid="multi-session-sharing-target-label">
              {t('sessionSharing.target')}
              <input
                value={target}
                onChange={(event) => setTarget(event.target.value)}
                disabled={busy || Boolean(editing)}
                required
                data-testid="multi-session-sharing-target"
              />
            </label>
            <fieldset disabled={busy} data-testid="multi-session-sharing-actions">
              <legend data-testid="multi-session-sharing-actions-label">{t('sessionSharing.actions')}</legend>
              <label data-testid="multi-session-sharing-action-label" data-variant="view">
                <input
                  type="checkbox"
                  checked={actions.includes('view')}
                  onChange={(event) =>
                    setActions((current) =>
                      event.target.checked ? [...current, 'view'] : current.filter((value) => value !== 'view'),
                    )
                  }
                  data-testid="multi-session-sharing-action"
                  data-variant="view"
                />
                {t('sessionSharing.action.view')}
              </label>
              <details data-testid="multi-session-sharing-extra-permissions">
                <summary data-testid="multi-session-sharing-extra-permissions-toggle">
                  {t('sessionSharing.extraPermissions')}
                </summary>
                {(['discuss', 'approve', 'download', 'manage'] as const).map((action) => (
                  <label key={action} data-testid="multi-session-sharing-action-label" data-variant={action}>
                    <input
                      type="checkbox"
                      checked={actions.includes(action)}
                      disabled={action !== 'manage'}
                      onChange={(event) =>
                        setActions((current) =>
                          event.target.checked ? [...current, action] : current.filter((value) => value !== action),
                        )
                      }
                      data-testid="multi-session-sharing-action"
                      data-variant={action}
                    />
                    <span data-testid="multi-session-sharing-permission-description" data-variant={action}>
                      {t(`sessionSharing.action.${action}`)}
                      <small className="session-sharing-permission-status">
                        {t(`sessionSharing.permissionStatus.${action}`)}
                      </small>
                      <small className="session-sharing-permission-description">
                        {t(`sessionSharing.permissionDescription.${action}`)}
                      </small>
                    </span>
                  </label>
                ))}
              </details>
            </fieldset>
            <label data-testid="multi-session-sharing-expiry-label">
              {t('sessionSharing.expiry')}
              <input
                type="datetime-local"
                value={expires}
                onChange={(event) => setExpires(event.target.value)}
                disabled={busy}
                required={!editing}
                data-testid="multi-session-sharing-expiry"
              />
            </label>
            <button
              type="submit"
              disabled={busy || !target.trim() || !actions.length || !validExpiry}
              data-testid="multi-session-sharing-save"
              data-variant={editing ? 'update' : 'create'}
            >
              {t(editing ? 'sessionSharing.update' : 'sessionSharing.create')}
            </button>
            {editing && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setEditing(null);
                  setTarget('');
                  setActions(['view']);
                  setExpires(localDateTime(Date.now() + 86400000));
                }}
                data-testid="multi-session-sharing-cancel-edit"
              >
                {t('common.cancel')}
              </button>
            )}
          </form>
        )}
        {canCreate && !loading && sessionId && <SharingAuditSection key={sessionId} sessionId={sessionId} />}
        {sessionId && (
          <>
            <h3 data-testid="multi-session-sharing-managed-title">{t('sessionSharing.managed')}</h3>
            <ul data-testid="multi-session-sharing-managed-list">
              {managed.map((share) => (
                <li key={share.share_id} data-testid="multi-session-sharing-managed-item" data-variant={share.share_id}>
                  <div>{share.target_actor ?? share.share_id}</div>
                  <span data-testid="multi-session-sharing-status">
                    {share.state === 'active'
                      ? share.actions?.map((action) => t(`sessionSharing.action.${action}`)).join(' · ')
                      : t('sessionSharing.unavailable')}
                  </span>
                  {share.state === 'active' && (
                    <span data-testid="multi-session-sharing-expiry-value">
                      {share.expires_at
                        ? new Date(share.expires_at * 1000).toLocaleString()
                        : t('sessionSharing.noExpiry')}
                    </span>
                  )}
                  <div className="session-sharing-row-actions">
                    {share.can_update && (
                      <button
                        type="button"
                        onClick={() => edit(share)}
                        disabled={busy}
                        data-testid="multi-session-sharing-edit"
                      >
                        {t('sessionSharing.edit')}
                      </button>
                    )}
                    {share.can_revoke && (
                      <button
                        type="button"
                        onClick={() => {
                          void perform(() => sessionSharingApi.revoke(share), share);
                        }}
                        disabled={busy || revokedHere.has(share.share_id)}
                        data-testid="multi-session-sharing-revoke"
                      >
                        {t('sessionSharing.revoke')}
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
        <h3 data-testid="multi-session-sharing-received-title">{t('sessionSharing.received')}</h3>
        {!loading && !error && !received.length && (
          <p data-testid="multi-session-sharing-received-empty">{t('sessionSharing.receivedEmpty')}</p>
        )}
        {continuing && onContinued && (
          <ContinuationPane
            key={`${continuing.share_id}:${continuing.revision}`}
            share={continuing}
            onContinued={onContinued}
            attempts={continuationAttempts ?? localAttempts.current}
            onCancel={() => setContinuing(null)}
          />
        )}
        <ul data-testid="multi-session-sharing-received-list">
          {received.map((share) => (
            <li key={share.share_id} data-testid="multi-session-sharing-received-item" data-variant={share.share_id}>
              <div>
                {share.session_id} · {share.grantor_actor}
              </div>
              <span data-testid="multi-session-sharing-received-expiry">
                {t('sessionSharing.expiry')}:{' '}
                {share.expires_at ? new Date(share.expires_at * 1000).toLocaleString() : t('sessionSharing.noExpiry')}
              </span>
              {onContinued &&
                share.state === 'active' &&
                share.actions?.includes('view') &&
                share.actions.includes('execute') &&
                (!share.expires_at || share.expires_at > Date.now() / 1000) && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setContinuing(share)}
                    data-testid="multi-session-sharing-continue"
                  >
                    {t('sessionSharing.continuation.open')}
                  </button>
                )}
              {onOpenSharedSession && share.state === 'active' && share.actions?.includes('view') && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onOpenSharedSession({ session_id: share.session_id, share_id: share.share_id })}
                  data-testid="multi-session-sharing-open"
                >
                  {t('sessionSharing.open')}
                </button>
              )}
            </li>
          ))}
        </ul>
      </div>
    </Dialog>
  );
}
