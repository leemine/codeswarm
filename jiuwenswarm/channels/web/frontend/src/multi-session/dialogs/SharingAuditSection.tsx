import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { sessionSharingApi, type SharingAuditPage } from '../../services/sessionSharingApi';
import { onOrganizationCredentialChange } from '../../services/organizationCredentialEvents';
import { webClient } from '../../services/webClient';

/** Mounted only by the current-owner area; server authorization is still required per read. */
export function SharingAuditSection({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [page, setPage] = useState<SharingAuditPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const generation = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const mounted = useRef(false);

  function clear() {
    generation.current += 1;
    pending.current?.abort();
    pending.current = null;
    setPage(null);
    setLoading(false);
  }

  async function load(limit = 50) {
    clear();
    setError(false);
    if (webClient.getState() !== 'ready') {
      setError(true);
      return;
    }
    const current = generation.current;
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    try {
      const result = await sessionSharingApi.audit(sessionId, limit, controller.signal);
      if (mounted.current && current === generation.current && !controller.signal.aborted) setPage(result);
    } catch {
      if (mounted.current && current === generation.current) {
        setPage(null);
        setError(true);
      }
    } finally {
      if (mounted.current && current === generation.current) {
        setLoading(false);
        pending.current = null;
      }
    }
  }

  useEffect(() => {
    mounted.current = true;
    const invalidate = () => {
      clear();
      setExpanded(false);
    };
    const auth = onOrganizationCredentialChange(invalidate);
    const connection = webClient.onStateChange((state) => {
      if (state !== 'ready') invalidate();
    });
    return () => {
      mounted.current = false;
      generation.current += 1;
      pending.current?.abort();
      auth();
      connection();
    };
  }, [sessionId]);

  return (
    <section className="session-sharing-audit" data-testid="multi-session-sharing-audit">
      <button
        type="button"
        aria-expanded={expanded}
        data-testid="multi-session-sharing-audit-toggle"
        onClick={() => {
          setExpanded(!expanded);
          if (expanded) clear();
          else void load();
        }}
      >
        {t('sessionSharing.audit.title')}
      </button>
      {expanded && (
        <div data-testid="multi-session-sharing-audit-content">
          <p data-testid="multi-session-sharing-audit-coverage">
            {t(
              page?.coverage === 'confirmed_mutations_publications_and_owner_exit_observations_only'
                ? 'sessionSharing.audit.lifecycleCoverage'
                : 'sessionSharing.audit.coverage',
            )}
          </p>
          <button
            type="button"
            disabled={loading}
            onClick={() => void load()}
            data-testid="multi-session-sharing-audit-refresh"
          >
            {t('sessionSharing.audit.refresh')}
          </button>
          {loading && (
            <p role="status" data-testid="multi-session-sharing-audit-loading">
              {t('sessionSharing.loading')}
            </p>
          )}
          {error && (
            <p role="alert" className="session-sharing-error" data-testid="multi-session-sharing-audit-error">
              {t('sessionSharing.audit.error')}
            </p>
          )}
          {page && (
            <>
              <p data-testid="multi-session-sharing-audit-count">
                {t('sessionSharing.audit.count', { count: page.events.length })}
              </p>
              {page.events.length === 0 && (
                <p data-testid="multi-session-sharing-audit-empty">{t('sessionSharing.audit.empty')}</p>
              )}
              <ul className="session-sharing-audit-list" data-testid="multi-session-sharing-audit-list">
                {page.events.map((event) => (
                  <li key={event.event_id} data-testid="multi-session-sharing-audit-item" data-variant={event.event_id}>
                    <strong data-testid="multi-session-sharing-audit-action">
                      {t(`sessionSharing.audit.action.${event.action}`)}
                    </strong>
                    <time dateTime={new Date(event.recorded_at * 1000).toISOString()}>
                      {new Date(event.recorded_at * 1000).toLocaleString()}
                    </time>
                    <span data-testid="multi-session-sharing-audit-actors">
                      {event.target_actor_id === null
                        ? t('sessionSharing.audit.owner', { actor: event.actor_id })
                        : t('sessionSharing.audit.actors', { actor: event.actor_id, target: event.target_actor_id })}
                    </span>
                    {event.share_id === null ? (
                      <span data-testid="multi-session-sharing-audit-phase" data-variant={event.phase}>
                        {t(`sessionSharing.audit.phase.${event.phase}`)}
                      </span>
                    ) : (
                      <span data-testid="multi-session-sharing-audit-revision">
                        {t('sessionSharing.audit.revision', { share: event.share_id, revision: event.share_revision })}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
              {page.has_more && <p data-testid="multi-session-sharing-audit-more">{t('sessionSharing.audit.more')}</p>}
              {page.has_more && page.events.length < 100 && (
                <button
                  type="button"
                  disabled={loading}
                  onClick={() => void load(100)}
                  data-testid="multi-session-sharing-audit-latest-hundred"
                >
                  {t('sessionSharing.audit.latestHundred')}
                </button>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
