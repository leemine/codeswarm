import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Dialog } from '../../components/ui/Dialog/Dialog';
import {
  sessionSharingApi,
  type SharedHistoryPage,
  type SharedSessionTarget,
  type SharedViewGrant,
} from '../../services/sessionSharingApi';
import { onOrganizationCredentialChange } from '../../services/organizationCredentialEvents';
import { webClient } from '../../services/webClient';
import './ShareSessionDialog.css';
import './SharedHistoryDialog.css';

export function SharedHistoryDialog(props: { target: SharedSessionTarget; onClose: () => void }) {
  // A target change destroys both content and in-flight request generations.
  return <SharedHistoryContent key={JSON.stringify(props.target)} {...props} />;
}

function SharedHistoryContent({ target, onClose }: { target: SharedSessionTarget; onClose: () => void }) {
  const { t } = useTranslation();
  const [messages, setMessages] = useState<SharedHistoryPage['messages']>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const generation = useRef(0);
  const busy = useRef(false);
  const mounted = useRef(false);
  const closed = useRef(false);
  const authChanged = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<AbortController | null>(null);
  const displayedGrant = useRef<SharedViewGrant | null>(null);

  function cancel() {
    generation.current += 1;
    busy.current = false;
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    pending.current?.abort();
    pending.current = null;
  }

  function clear() {
    cancel();
    displayedGrant.current = null;
    setMessages([]);
    setCursor(null);
    setLoading(false);
    setError(true);
  }

  function visible() {
    return (
      mounted.current &&
      !closed.current &&
      !authChanged.current &&
      document.visibilityState === 'visible' &&
      webClient.getState() === 'ready'
    );
  }

  function schedule(grant: SharedViewGrant) {
    if (timer.current !== null) clearTimeout(timer.current);
    // No revoke push exists. While visible, cached text is hidden and rechecked
    // at most every 15 seconds, or earlier at the server-reported expiry.
    const delay = Math.min(
      15_000,
      grant.expires_at === null ? 15_000 : Math.max(0, grant.expires_at * 1000 - Date.now()),
    );
    timer.current = setTimeout(() => {
      timer.current = null;
      clear();
      if (visible()) void load();
    }, delay);
  }

  async function load(older = false, manual = false) {
    if (manual) authChanged.current = false;
    if (!visible() || busy.current || (older && !cursor)) return;
    busy.current = true;
    const current = ++generation.current;
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError(false);
    if (!older) {
      setMessages([]);
      setCursor(null);
    }
    const priorGrant = displayedGrant.current;
    const stillCurrent = () => current === generation.current && visible() && !controller.signal.aborted;
    try {
      const grant = await sessionSharingApi.viewGrant(target, controller.signal);
      if (!stillCurrent()) return;
      const append = older && priorGrant?.revision === grant.revision;
      if (older && !append) {
        setMessages([]);
        setCursor(null);
      }
      schedule(grant);
      // The grant DTO never permits cached pagination or substitutes for this
      // server-authorized history request (including on every timer refresh).
      const page = await sessionSharingApi.history(target, append ? cursor! : undefined, controller.signal);
      if (!stillCurrent()) return;
      if (grant.expires_at !== null && grant.expires_at * 1000 <= Date.now()) {
        clear();
        return;
      }
      displayedGrant.current = grant;
      setMessages((previous) => (append ? [...previous, ...page.messages] : page.messages));
      setCursor(page.next_cursor);
    } catch {
      if (current !== generation.current) return;
      // Authorization and transport failures both withdraw cached data.
      clear();
    } finally {
      if (current === generation.current) {
        busy.current = false;
        pending.current = null;
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    mounted.current = true;
    closed.current = false;
    void load();
    const unsubscribeAuth = onOrganizationCredentialChange(() => {
      authChanged.current = true;
      clear();
    });
    const unsubscribeConnection = webClient.onStateChange((state) => {
      if (state !== 'ready') clear();
      else if (visible()) void load();
    });
    const onVisibility = () => {
      if (document.visibilityState !== 'visible') clear();
      else if (visible()) void load();
    };
    const onPageHide = () => {
      closed.current = true;
      clear();
    };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', onPageHide);
    return () => {
      mounted.current = false;
      cancel();
      unsubscribeAuth();
      unsubscribeConnection();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pagehide', onPageHide);
    };
  }, []);

  function close() {
    closed.current = true;
    clear();
    onClose();
  }

  return (
    <Dialog
      open
      titleId="shared-history-title"
      className="session-sharing-dialog"
      onCancel={close}
      onBackdropClick={close}
    >
      <section className="session-sharing-content shared-history-content" data-testid="multi-session-shared-history">
        <div className="session-sharing-heading">
          <h2 id="shared-history-title" data-testid="multi-session-shared-history-title">
            {t('sessionSharing.viewerTitle')}
          </h2>
          <button type="button" onClick={close} data-testid="multi-session-shared-history-close">
            {t('common.close')}
          </button>
        </div>
        <p data-testid="multi-session-shared-history-limits">{t('sessionSharing.viewerLimits')}</p>
        <button
          type="button"
          onClick={() => void load(false, true)}
          disabled={loading}
          data-testid="multi-session-shared-history-refresh"
        >
          {t('sessionSharing.viewerRefresh')}
        </button>
        {loading && (
          <p role="status" data-testid="multi-session-shared-history-loading">
            {t('sessionSharing.loading')}
          </p>
        )}
        {error && (
          <p role="alert" className="session-sharing-error" data-testid="multi-session-shared-history-error">
            {t('sessionSharing.viewerUnavailable')}
          </p>
        )}
        {!loading && !error && !messages.length && (
          <p data-testid="multi-session-shared-history-empty">{t('sessionSharing.viewerEmpty')}</p>
        )}
        <ol hidden={loading} data-testid="multi-session-shared-history-messages">
          {messages.map((message, index) => (
            <li
              key={`${index}:${message.id ?? ''}`}
              data-testid="multi-session-shared-history-message"
              data-variant={message.id ?? index}
            >
              <span data-testid="multi-session-shared-history-role">
                {t(`sessionSharing.viewerRole.${message.role}`)}
              </span>
              <div className="shared-history-text" data-testid="multi-session-shared-history-text">
                {message.content}
              </div>
            </li>
          ))}
        </ol>
        {cursor && (
          <button
            type="button"
            disabled={loading}
            onClick={() => void load(true)}
            data-testid="multi-session-shared-history-older"
          >
            {t('sessionSharing.viewerOlder')}
          </button>
        )}
      </section>
    </Dialog>
  );
}
