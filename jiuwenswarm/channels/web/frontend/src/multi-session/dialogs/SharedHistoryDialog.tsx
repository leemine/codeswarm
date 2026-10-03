import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Dialog } from '../../components/ui/Dialog/Dialog';
import { sessionSharingApi, type SharedHistoryPage, type SharedSessionTarget } from '../../services/sessionSharingApi';
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

  function clear() {
    generation.current += 1;
    busy.current = false;
    setMessages([]);
    setCursor(null);
    setLoading(false);
    setError(true);
  }

  async function load(older = false) {
    if (busy.current || (older && !cursor)) return;
    busy.current = true;
    const current = ++generation.current;
    setLoading(true);
    setError(false);
    if (!older) {
      setMessages([]);
      setCursor(null);
    }
    try {
      const page = await sessionSharingApi.history(target, older ? cursor! : undefined);
      if (current !== generation.current) return;
      setMessages((previous) => (older ? [...previous, ...page.messages] : page.messages));
      setCursor(page.next_cursor);
    } catch {
      if (current !== generation.current) return;
      // Authorization failures and transport failures both withdraw cached data.
      setMessages([]);
      setCursor(null);
      setError(true);
    } finally {
      if (current === generation.current) {
        busy.current = false;
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    void load();
    const unsubscribeAuth = onOrganizationCredentialChange(clear);
    const unsubscribeConnection = webClient.onStateChange((state) => {
      if (state !== 'ready') clear();
    });
    const onVisibility = () => {
      if (document.visibilityState !== 'visible') clear();
    };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', clear);
    return () => {
      generation.current += 1;
      busy.current = false;
      unsubscribeAuth();
      unsubscribeConnection();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pagehide', clear);
    };
  }, []);

  function close() {
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
          onClick={() => void load()}
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
        <ol data-testid="multi-session-shared-history-messages">
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
