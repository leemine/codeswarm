import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, Cpu, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useSessionStore } from '../../stores';
import { webClient, webRequest } from '../../services/webClient';
import { onOrganizationCredentialChange } from '../../services/organizationCredentialEvents';
import {
  executionProviderLabel,
  getExecutionOptions,
  type ExecutionOption,
  type ExecutionOptions,
} from '../../services/executionOptions';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import './ExecutionPicker.css';

export function ExecutionPicker({
  sessionId,
  mode,
  workMode,
  disabled,
}: {
  sessionId: string;
  mode: string;
  workMode: 'work' | 'code';
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const draft = sessionId === NEW_CONVERSATION_ID;
  const choice = useSessionStore((s) => s.runtimes[sessionId]?.executionChoice);
  const [options, setOptions] = useState<ExecutionOptions | null>(null);
  const binding = useSessionStore((s) => s.runtimes[sessionId]?.executionDisplay ?? null);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<CSSProperties>({ visibility: 'hidden' });
  const anchor = useRef<HTMLDivElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const generation = useRef(0);

  useEffect(() => {
    const invalidate = () => {
      generation.current += 1;
      setOptions(null);
      useSessionStore.getState().setExecutionDisplay(sessionId, null);
      setOpen(false);
    };
    const auth = onOrganizationCredentialChange(() => {
      invalidate();
      useSessionStore.getState().setExecutionChoice(NEW_CONVERSATION_ID, null);
      setReload((n) => n + 1);
    });
    const connection = webClient.onStateChange((state) => {
      if (state !== 'ready') invalidate();
      else setReload((n) => n + 1);
    });
    return () => {
      auth();
      connection();
    };
  }, [sessionId]);

  useEffect(() => {
    const current = ++generation.current;
    setOptions(null);
    useSessionStore.getState().setExecutionDisplay(sessionId, null);
    setError(false);
    setOpen(false);
    const load = draft
      ? getExecutionOptions(webRequest, mode, workMode).then((result) => {
          if (current !== generation.current) return;
          setOptions(result);
          if (!useSessionStore.getState().getRuntime(sessionId)?.executionChoice) {
            const selected = result.options.find((row) => row.execution_profile_id === result.default_profile_id);
            if (selected?.available) useSessionStore.getState().setExecutionChoice(sessionId, selected);
          }
        })
      : webRequest<{ execution_display?: typeof binding }>('session.get_metadata', { session_id: sessionId }).then(
          (metadata) => {
            if (current === generation.current)
              useSessionStore.getState().setExecutionDisplay(sessionId, metadata?.execution_display ?? null);
          },
        );
    void load.catch(() => {
      if (current === generation.current) setError(true);
    });
    return () => {
      generation.current += 1;
    };
  }, [sessionId, mode, workMode, draft, reload]);

  useEffect(() => {
    if (!open) return;
    const dismiss = (e: PointerEvent) => {
      if (!anchor.current?.contains(e.target as Node) && !menu.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', dismiss);
    return () => document.removeEventListener('pointerdown', dismiss);
  }, [open]);
  useLayoutEffect(() => {
    if (!open) return;
    const update = () => {
      if (!anchor.current || !menu.current) return;
      const rect = anchor.current.getBoundingClientRect();
      const above = rect.top - 10,
        below = window.innerHeight - rect.bottom - 10;
      setPosition({
        left: Math.max(10, Math.min(rect.left, window.innerWidth - menu.current.offsetWidth - 10)),
        ...(above > below ? { bottom: window.innerHeight - rect.top + 8 } : { top: rect.bottom + 8 }),
        maxHeight: Math.max(100, Math.min(340, above > below ? above : below)),
      });
    };
    update();
    menu.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open]);

  function choose(option: ExecutionOption) {
    setOpen(false);
    if (draft) useSessionStore.getState().setExecutionChoice(sessionId, option);
  }
  const selectedId = draft ? choice?.execution_profile_id : binding?.execution_profile_id;
  const label = draft
    ? executionProviderLabel(choice?.provider_id)
    : executionProviderLabel(binding?.provider_id) || binding?.execution_profile_id;
  const unavailable =
    draft &&
    choice &&
    !options?.options.some(
      (row) =>
        row.execution_profile_id === choice.execution_profile_id &&
        row.provider_id === choice.provider_id &&
        row.config_fingerprint === choice.config_fingerprint &&
        row.available,
    );
  return (
    <div className="chat-mode-select chat-execution-picker" ref={anchor} data-testid="chat-panel-execution-picker">
      {!draft ? (
        <span
          className="chat-mode-select__trigger"
          title={t('executionPicker.boundHint')}
          data-testid="chat-panel-execution-binding"
        >
          <Cpu size={16} aria-hidden="true" />
          <span data-testid="chat-panel-execution-label">
            {error ? t('executionPicker.loadFailed') : label || t('executionPicker.title')}
          </span>
        </span>
      ) : (
        <button
          type="button"
          className="chat-mode-select__trigger"
          data-testid="chat-panel-execution-trigger"
          aria-label={t('executionPicker.title')}
          aria-expanded={open}
          aria-haspopup="menu"
          disabled={disabled}
          onClick={() => {
            setOpen((value) => !value);
          }}
        >
          <Cpu size={16} aria-hidden="true" />
          <span data-testid="chat-panel-execution-label">
            {error ? t('executionPicker.loadFailed') : label || t('executionPicker.title')}
          </span>
          <ChevronDown size={12} aria-hidden="true" />
        </button>
      )}
      {draft &&
        open &&
        createPortal(
          <div
            ref={menu}
            style={position}
            className="chat-execution-menu"
            role="menu"
            data-testid="chat-panel-execution-menu"
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                setOpen(false);
                anchor.current?.querySelector('button')?.focus();
              } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
                e.preventDefault();
                const items = Array.from(
                  menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
                );
                const index = items.indexOf(document.activeElement as HTMLButtonElement);
                const next =
                  e.key === 'Home'
                    ? 0
                    : e.key === 'End'
                      ? items.length - 1
                      : (index + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
                items[next]?.focus();
              }
            }}
          >
            <div className="chat-execution-menu__title" data-testid="chat-panel-execution-menu-title">
              {t('executionPicker.title')}
            </div>
            {unavailable && (
              <p role="alert" data-testid="chat-panel-execution-unavailable">
                {t('executionPicker.selectionUnavailable')}
              </p>
            )}
            {error ? (
              <button
                type="button"
                className="chat-mode-select__option"
                data-testid="chat-panel-execution-retry"
                onClick={() => setReload((n) => n + 1)}
              >
                <RefreshCw size={14} />
                {t('executionPicker.retry')}
              </button>
            ) : !options ? (
              <p role="status" data-testid="chat-panel-execution-loading">
                {t('executionPicker.loading')}
              </p>
            ) : (
              options.options.map((option) => (
                <button
                  type="button"
                  className="chat-mode-select__option"
                  role="menuitemradio"
                  aria-checked={selectedId === option.execution_profile_id}
                  disabled={!option.available}
                  data-testid="chat-panel-execution-option"
                  data-variant={option.execution_profile_id ?? 'legacy-native'}
                  key={option.execution_profile_id ?? 'legacy-native'}
                  onClick={() => choose(option)}
                >
                  <span className="chat-execution-option__copy">
                    <span>{executionProviderLabel(option.provider_id)}</span>
                    <small>
                      {option.available
                        ? option.execution_profile_id?.startsWith('builtin:')
                          ? t('executionPicker.engineDefaults')
                          : option.execution_profile_id || t('executionPicker.builtin')
                        : t(`executionPicker.reason.${option.reason}`)}
                    </small>
                  </span>
                  {selectedId === option.execution_profile_id && <Check size={14} aria-hidden="true" />}
                </button>
              ))
            )}
            {!error &&
              options?.unconfigured_providers?.map((provider) => (
                <button
                  type="button"
                  className="chat-mode-select__option"
                  role="menuitemradio"
                  aria-checked={false}
                  disabled
                  data-testid="chat-panel-execution-option"
                  data-variant={`provider-${provider.provider_id}`}
                  key={`provider-${provider.provider_id}`}
                >
                  <span className="chat-execution-option__copy">
                    <span>{executionProviderLabel(provider.provider_id)}</span>
                    <small>{t(`executionPicker.reason.${provider.reason}`)}</small>
                  </span>
                </button>
              ))}
          </div>,
          document.body,
        )}
    </div>
  );
}
